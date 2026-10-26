"""Tahap 8.2 — full-duration preview built from per-shot segments.

Why segments
------------
The old preview rendered a single 20 s window in one FFmpeg pass, so every edit
(even moving one shot's crop by a pixel) threw the whole file away. A full clip
is now split along shot boundaries and each piece is cached on its own key:
editing shot 3 re-renders shot 3 and re-uses everything else.

How the pieces are joined without drift
---------------------------------------
Concatenating per-segment AAC is the classic source of audio drift (every
segment carries its own encoder priming). So the video halves are joined with
`-c copy` and the audio is built **once** for the whole clip by
`video_engine._audio_graph`, then muxed in. One continuous audio track means the
A/V offset cannot accumulate, and `-shortest` trims any one-frame rounding at
the tail.

Frame accuracy
--------------
Shot boundaries are already frame-snapped by the shot plan, `render_clip` forces
`-r fps` on every segment, so the sum of segment frames equals the clip length
in frames. Hard cuts stay clean because each segment is a separate encode and
the join is a stream copy — no frame blending across the seam.

Framing parity with the final render
------------------------------------
Segments do not re-derive the crop path. They slice the *same* `plan["points"]`
that the final render uses, shifted to the segment's own zero, with `cut=True` on
the first point so smoothing never crosses a shot boundary.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from backend import config
from backend.schemas.render import RenderSettingsModel
from backend.services import edit_service, media_service, project_store, video_engine

logger = logging.getLogger(__name__)

SEGMENT_VERSION = 3
MANIFEST_VERSION = 1
#: Segments shorter than this are folded into the previous one; a 4-frame sliver
#: is not worth its own encode and would make the concat list noisy.
MIN_SEGMENT_SECONDS = 0.35

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()

#: Settings that only touch audio — excluded from the video key so a voice-boost
#: change does not re-encode every segment.
_AUDIO_ONLY_KEYS = ("voiceBoost", "bgmFile", "bgmVolume", "bgmOffset", "bgmLoop")


class PreviewCacheError(RuntimeError):
    """Raised when a segment or the assembly fails."""


# --- small helpers -----------------------------------------------------------


def _lock_for(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def _digest(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def proxy_size(settings: RenderSettingsModel, duration: float) -> tuple[int, int]:
    """Proxy width follows the clip length: 540 for ≤60 s, 360 above."""
    from backend.services.preview_service import TARGET_SIZES

    width, height = TARGET_SIZES.get(settings.aspectRatio, TARGET_SIZES["9:16"])
    out_w = int(
        config.PREVIEW_FULL_WIDTH_SHORT
        if duration <= config.PREVIEW_FULL_LONG_THRESHOLD
        else config.PREVIEW_FULL_WIDTH_LONG
    )
    out_h = max(2, int(round(out_w * height / width)))
    if out_h % 2:
        out_h += 1
    return out_w, out_h


def _src_stamp(source: Path) -> dict[str, Any]:
    try:
        stat = source.stat()
        return {"name": source.name, "size": stat.st_size, "mtime": int(stat.st_mtime)}
    except OSError:
        return {"name": source.name}


# --- segment planning --------------------------------------------------------


def plan_segments(
    shots: Sequence[dict[str, Any]] | None,
    start: float,
    end: float,
    *,
    fps: float | None = None,
) -> list[dict[str, Any]]:
    """Split `[start, end)` along the shot list; one segment when there is none.

    Every returned segment covers the clip exactly once: gaps between shots are
    absorbed by the neighbour so the concat list has no holes. Boundaries are
    snapped to the frame grid relative to `start` — otherwise a shot cut at
    22.95 s (688.5 frames) rounds down in its own segment and the assembled
    video loses a frame at every join.
    """
    start = float(start)
    end = float(end)
    total = max(0.0, end - start)
    if total <= 0:
        return []

    rate = float(fps or config.RENDER_FPS) or 30.0

    def snap(value: float) -> float:
        return start + round((value - start) * rate) / rate

    bounds: list[tuple[float, float]] = []
    for shot in shots or []:
        try:
            lo = float(shot.get("start"))
            hi = float(shot.get("end"))
        except (TypeError, ValueError):
            continue
        lo = min(max(lo, start), end)
        hi = min(max(hi, start), end)
        if hi - lo > 0.05:
            bounds.append((snap(lo), snap(hi)))
    bounds.sort()

    if not bounds:
        return [_with_frames({"index": 0, "start": start, "end": end}, rate)]

    segments: list[dict[str, Any]] = []
    cursor = start
    for lo, hi in bounds:
        lo = min(lo, cursor)  # absorb a gap into this segment
        if hi <= cursor + 1e-6:
            continue
        if segments and hi - lo < MIN_SEGMENT_SECONDS:
            segments[-1]["end"] = hi  # fold the sliver into its neighbour
            cursor = hi
            continue
        segments.append({"index": len(segments), "start": cursor, "end": hi})
        cursor = hi
    if cursor < end - 1e-3:
        if segments:
            segments[-1]["end"] = end
        else:
            segments.append({"index": 0, "start": start, "end": end})
    return [_with_frames(segment, rate) for segment in segments]


def _with_frames(segment: dict[str, Any], rate: float) -> dict[str, Any]:
    """Attach the exact frame count the segment must contain.

    The renderer is told how many frames to emit, so a cut that lands between
    two frames can never shorten the piece and shift everything after it.
    """
    span = max(0.0, float(segment["end"]) - float(segment["start"]))
    segment["frames"] = max(1, int(round(span * rate)))
    return segment


# --- key building ------------------------------------------------------------


def video_key(
    *,
    project_id: str,
    clip_index: int,
    source: Path,
    segment: dict[str, Any],
    width: int,
    height: int,
    settings: RenderSettingsModel,
    points: list[dict[str, Any]] | None,
    spans: list[tuple[float, float]],
    ass_text: str | None,
    hook: str | None,
    shot: list[dict[str, Any]] | None = None,
) -> str:
    payload = settings.model_dump()
    for key in _AUDIO_ONLY_KEYS:
        payload.pop(key, None)
    return _digest(
        {
            "v": SEGMENT_VERSION,
            "project": project_id,
            "clip": clip_index,
            "segment": segment,
            "size": [width, height],
            "fps": config.RENDER_FPS,
            "settings": payload,
            "src": _src_stamp(source),
            "points": points,
            "spans": spans,
            # The stored shot entries that overlap this segment. They are the
            # authority on kind/zoom/pan, so editing one shot re-renders exactly
            # that segment even when the derived crop path barely moves.
            "shot": shot,
            "ass": _digest(ass_text) if ass_text else None,
            "hook": hook,
        }
    )


def audio_key(
    *,
    project_id: str,
    clip_index: int,
    source: Path,
    start: float,
    end: float,
    settings: RenderSettingsModel,
    has_audio: bool,
) -> str:
    return _digest(
        {
            "v": SEGMENT_VERSION,
            "project": project_id,
            "clip": clip_index,
            "window": [round(start, 3), round(end, 3)],
            "src": _src_stamp(source),
            "has_audio": has_audio,
            "boost": settings.voiceBoost,
            "bgm": [settings.bgmFile, settings.bgmVolume, settings.bgmOffset, settings.bgmLoop],
        }
    )


# --- path helpers ------------------------------------------------------------


def segment_path(key: str) -> Path:
    return config.PREVIEW_SEG_DIR / f"{key}.mp4"


def audio_path(key: str) -> Path:
    return config.PREVIEW_SEG_DIR / f"{key}.m4a"


def preview_path(project_id: str, clip_index: int, signature: str) -> Path:
    return config.PREVIEWS_DIR / f"full-{project_id}-{clip_index:02d}-{signature}.mp4"


def manifest_path(project_id: str, clip_index: int) -> Path:
    return config.PREVIEW_SEG_DIR / f"{project_id}-{clip_index:02d}.json"


def _work_dir(key: str) -> Path:
    # render_clip writes `subs.ass`/`watermark-*.txt` into out_path.parent under
    # fixed names and deletes them afterwards, so every segment needs its own.
    path = config.PREVIEW_SEG_DIR / f"w-{key}"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- crop path slicing -------------------------------------------------------


def slice_points(
    points: Sequence[dict[str, Any]] | None,
    lo: float,
    hi: float,
) -> list[dict[str, Any]] | None:
    """Shift the clip-wide crop path into a segment's own zero.

    A point just outside the window on each side is kept so the first and last
    frames land on the interpolated position instead of a stale one.
    """
    if not points:
        return None
    pad = 0.25
    out: list[dict[str, Any]] = []
    for raw in points:
        try:
            t = float(raw.get("t") or 0.0)
        except (TypeError, ValueError):
            continue
        if t < lo - pad or t > hi + pad:
            continue
        local = min(max(t - lo, 0.0), max(0.0, hi - lo))
        point = {key: value for key, value in raw.items() if key != "t"}
        point["t"] = round(local, 4)
        out.append(point)
    if not out:
        return None
    out.sort(key=lambda item: item["t"])
    out[0]["t"] = 0.0
    out[0]["cut"] = True  # never smooth across a shot boundary
    if out[-1]["t"] < hi - lo:
        tail = dict(out[-1])
        tail["t"] = round(hi - lo, 4)
        tail["cut"] = False
        out.append(tail)
    return out


# --- ffmpeg plumbing ---------------------------------------------------------


def _run(command: list[str], *, cwd: Path | None = None) -> None:
    logger.debug("ffmpeg %s", " ".join(command[:8]))
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=1800,
        )
    except OSError as exc:
        raise PreviewCacheError(f"ffmpeg could not start: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PreviewCacheError("ffmpeg timed out while assembling the preview.") from exc
    if result.returncode != 0:
        tail = (result.stdout or "").strip().splitlines()[-4:]
        raise PreviewCacheError("ffmpeg failed: " + " | ".join(tail)[:400])


def render_segment(
    source: Path,
    *,
    key: str,
    segment: dict[str, Any],
    width: int,
    height: int,
    settings: RenderSettingsModel,
    src_w: int,
    src_h: int,
    has_audio: bool,
    points: list[dict[str, Any]] | None,
    spans: list[tuple[float, float]],
    ass_text: str | None,
    hook: str | None,
    logo: Path | None,
    bgm: Path | None,
) -> tuple[Path, int]:
    """Render one shot into its own file. Returns `(path, elapsed_ms)`."""
    out = segment_path(key)
    work = _work_dir(key)
    started = time.time()
    video_engine.render_clip(
        source,
        work / f"{key}.mp4",
        start=float(segment["start"]),
        end=float(segment["end"]),
        width=width,
        height=height,
        background=settings.background,
        encoder=settings.encoder,
        ass_text=ass_text,
        has_audio=has_audio,
        watermark_text=settings.watermark,
        watermark_image=str(logo) if logo else None,
        watermark_position=settings.watermarkPosition,
        watermark_opacity=settings.watermarkOpacity,
        watermark_size=settings.watermarkSize,
        # Audio is rebuilt once for the whole clip; the per-segment track is
        # discarded at concat time, so no BGM work is needed here.
        bgm_path=None,
        bgm_volume=settings.bgmVolume,
        voice_boost=settings.voiceBoost,
        src_size=(src_w, src_h),
        face_keyframes=points,
        whole_spans=spans,
        facecam_face=None,
        streamer_preset=edit_service.effective_streamer(settings),
        facecam_position=settings.facecamPosition,
        hook_text=hook,
        hook_position=settings.hookPosition,
        hook_duration=settings.hookDuration,
        hook_size=settings.hookSize,
        hook_case=settings.hookCase,
        preview=True,
        frames=int(segment.get("frames") or 0) or None,
    )
    (work / f"{key}.mp4").replace(out)
    shutil.rmtree(work, ignore_errors=True)
    return out, int((time.time() - started) * 1000)


def render_audio(
    source: Path,
    *,
    key: str,
    start: float,
    end: float,
    has_audio: bool,
    settings: RenderSettingsModel,
    bgm: Path | None,
) -> tuple[Path | None, int]:
    """One continuous audio track for the whole clip (no per-segment drift)."""
    duration = max(0.2, float(end) - float(start))
    fragment, label = video_engine._audio_graph(  # noqa: SLF001 - shared builder
        duration=duration,
        has_audio=has_audio,
        voice_boost=settings.voiceBoost,
        bgm_label="1:a" if bgm else None,
        bgm_volume=settings.bgmVolume,
    )
    if not label:
        return None, 0

    out = audio_path(key)
    started = time.time()
    command = [
        video_engine.capabilities()["path"],
        "-y",
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, float(start)):.3f}",
        "-i",
        str(source),
    ]
    if bgm:
        if settings.bgmLoop:
            command += ["-stream_loop", "-1"]
        if settings.bgmOffset > 0:
            command += ["-ss", f"{float(settings.bgmOffset):.3f}"]
        command += ["-i", str(bgm)]
    command += [
        "-t",
        f"{duration:.3f}",
        "-filter_complex",
        fragment,
        "-map",
        f"[{label}]",
        "-c:a",
        "aac",
        "-b:a",
        config.RENDER_AUDIO_BITRATE,
        "-ac",
        "2",
        str(out),
    ]
    _run(command, cwd=config.PREVIEW_SEG_DIR)
    return out, int((time.time() - started) * 1000)


def _concat_list(paths: Sequence[Path], target: Path) -> Path:
    lines = []
    for path in paths:
        safe = str(path.resolve()).replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def assemble(
    segments: Sequence[Path],
    *,
    key: str,
    out_path: Path,
    audio: Path | None,
    encoder: str | None,
) -> int:
    """Join video segments with a stream copy, then mux the single audio track."""
    if not segments:
        raise PreviewCacheError("No segments to assemble.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = _work_dir(f"asm-{key}")
    listing = _concat_list(segments, work / "list.txt")
    joined = work / "joined.mp4"
    binary = video_engine.capabilities()["path"]

    try:
        _run(
            [
                binary,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-c",
                "copy",
                "-an",
                str(joined),
            ]
        )
    except PreviewCacheError as exc:  # pragma: no cover - codec mismatch safety net
        logger.warning("stream-copy concat failed (%s) — re-encoding the join", exc)
        codec = video_engine.pick_encoder(encoder)
        _run(
            [
                binary,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-an",
                *video_engine._encoder_args(codec, preview=True),  # noqa: SLF001
                "-r",
                str(config.RENDER_FPS),
                str(joined),
            ]
        )

    command = [binary, "-y", "-hide_banner", "-loglevel", "error", "-i", str(joined)]
    if audio is not None:
        command += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:a", "copy", "-shortest"]
    else:
        command += ["-map", "0:v:0"]
    command += ["-c:v", "copy", "-movflags", "+faststart", str(out_path)]
    _run(command, cwd=work)
    if not out_path.is_file() or out_path.stat().st_size < 1024:
        raise PreviewCacheError("Assembled preview is empty.")
    shutil.rmtree(work, ignore_errors=True)
    return out_path.stat().st_size


# --- manifest ----------------------------------------------------------------


def read_manifest(project_id: str, clip_index: int) -> dict[str, Any] | None:
    path = manifest_path(project_id, clip_index)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_manifest(project_id: str, clip_index: int, payload: dict[str, Any]) -> None:
    path = manifest_path(project_id, clip_index)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def purge(project_id: str, clip_index: int) -> int:
    """Drop the segments of one clip (used by DELETE / reset and by cleanup)."""
    manifest = read_manifest(project_id, clip_index) or {}
    removed = 0
    for entry in manifest.get("segments") or []:
        path = segment_path(str(entry.get("key")))
        if path.is_file():
            path.unlink(missing_ok=True)
            removed += 1
    audio = manifest.get("audio_key")
    if audio:
        audio_file = audio_path(str(audio))
        if audio_file.is_file():
            audio_file.unlink(missing_ok=True)
            removed += 1
    if manifest.get("file"):
        (config.PREVIEWS_DIR / str(manifest["file"])).unlink(missing_ok=True)
    manifest_path(project_id, clip_index).unlink(missing_ok=True)
    return removed


def prune_orphans(grace_seconds: float = 0.0) -> int:
    """Delete segment files that no manifest points at any more.

    Every edit produces a new content key, so the old file of that shot becomes
    dead weight the moment the new one lands. Keys are content hashes, so the
    only way to know whether a file is still wanted is to ask every manifest.
    """
    if not config.PREVIEW_SEG_DIR.is_dir():
        return 0
    keep: set[str] = set()
    for path in config.PREVIEW_SEG_DIR.glob("*.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entry in manifest.get("segments") or []:
            if entry.get("key"):
                keep.add(str(entry["key"]))
        if manifest.get("audio_key"):
            keep.add(str(manifest["audio_key"]))
    removed = 0
    now = time.time()
    for path in list(config.PREVIEW_SEG_DIR.glob("*.mp4")) + list(config.PREVIEW_SEG_DIR.glob("*.m4a")):
        if path.stem in keep:
            continue
        try:
            if grace_seconds and now - path.stat().st_mtime < grace_seconds:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    # Leftover scratch folders from an interrupted segment render.
    for path in config.PREVIEW_SEG_DIR.glob("w-*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    # Stale assembled previews: every settings change mints a new content key and
    # writes a new `full-*` file next to the old one, which nothing ever removed.
    live: set[str] = set()
    for path in config.PREVIEW_SEG_DIR.glob("*.json"):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if manifest.get("file"):
            live.add(str(manifest["file"]))
    for path in config.PREVIEWS_DIR.glob("full-*.mp4"):
        if path.name in live:
            continue
        try:
            if grace_seconds and now - path.stat().st_mtime < grace_seconds:
                continue
            path.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return removed


def cache_size(project_id: str, clip_index: int | None = None) -> int:
    total = 0
    if clip_index is not None:
        manifest = read_manifest(project_id, clip_index) or {}
        keys = [str(entry.get("key")) for entry in manifest.get("segments") or []]
        if manifest.get("audio_key"):
            keys.append(str(manifest["audio_key"]))
        for key in keys:
            for path in (segment_path(key), audio_path(key)):
                if path.is_file():
                    total += path.stat().st_size
        if manifest.get("file"):
            path = config.PREVIEWS_DIR / str(manifest["file"])
            if path.is_file():
                total += path.stat().st_size
        return total
    if config.PREVIEW_SEG_DIR.is_dir():
        for path in config.PREVIEW_SEG_DIR.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    return total


# --- orchestration -----------------------------------------------------------


def _resolve_assets(settings: RenderSettingsModel) -> tuple[Path | None, Path | None]:
    logo = None
    if settings.watermarkImage:
        try:
            from backend.services import asset_service

            logo = asset_service.resolve("watermarks", settings.watermarkImage)
        except Exception:  # noqa: BLE001
            logo = None
    bgm = None
    if settings.bgmFile:
        try:
            from backend.services import asset_service

            bgm = asset_service.resolve("bgm", settings.bgmFile)
        except Exception:  # noqa: BLE001
            bgm = None
    return logo, bgm


def build_full(
    project_id: str,
    clip_index: int,
    settings: RenderSettingsModel | None = None,
    *,
    on_progress: Any = None,
) -> dict[str, Any]:
    """Render the full clip preview from per-shot segments.

    Returns the same shape as ``preview_service.render_preview`` plus
    ``segments``/``audio`` diagnostics; re-renders only the segments whose key
    changed. Never raises for routine ffmpeg failures — they bubble up so the
    router can map them, but the cache is left intact for a retry.
    """
    from backend.services import preview_service

    project = project_store.load(project_id)
    if clip_index < 0 or clip_index >= len(project.clips):
        raise PreviewCacheError(f"Clip {clip_index} does not exist in this project.")
    resolved = (settings or RenderSettingsModel()).model_copy(deep=True)
    clip = project.clips[clip_index]
    source = media_service.ensure_source(project.model_dump())
    info = video_engine.probe(source)
    src_w = int(info.get("width") or 0)
    src_h = int(info.get("height") or 0)
    has_audio = bool(info.get("has_audio"))

    start, end = edit_service.clip_window(clip)
    cap = float(config.PREVIEW_FULL_MAX_SECONDS)
    capped_end = min(end, start + cap) if cap > 0 else end
    if capped_end - start < edit_service.MIN_CLIP_SECONDS:
        capped_end = start + edit_service.MIN_CLIP_SECONDS
    truncated = capped_end < end - 1e-3
    duration = capped_end - start

    width, height = proxy_size(resolved, duration)
    shots = edit_service.stored_shots(project.id, clip_index)
    plan = edit_service.face_plan(faces := edit_service.face_path(
        source, start, capped_end, resolved, project_id=project.id, clip_index=clip_index
    ), width=width, height=height, shots=shots)
    points = (plan or {}).get("points") or edit_service.face_keyframes(
        faces, width=width, height=height
    )
    hook_line = edit_service.hook_line(resolved)
    logo, bgm = _resolve_assets(resolved)
    transcript = [line.model_dump() for line in project.transcript]

    logger.info(
        "preview.full %s[%s]: shots=%d plan=%s points=%d proxy=%dx%d",
        project.id, clip_index, len(shots or []), plan is not None,
        len(points or []), width, height,
    )

    segments = plan_segments(shots, start, capped_end, fps=config.RENDER_FPS)
    if not segments:
        raise PreviewCacheError("Clip has no duration.")

    config.PREVIEW_SEG_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    seg_meta: list[dict[str, Any]] = []
    rendered = 0
    for seg in segments:
        lo = seg["start"]
        hi = seg["end"]
        # `plan["points"]` are clip-relative (shot_plan converts to
        # `shot.start - clip_start`), while segment bounds are absolute source
        # seconds. Slicing with the absolute window matched nothing, so every
        # segment rendered through the letterbox fallback and the whole shot
        # list — manual framing included — was silently ignored.
        seg_points = slice_points(points, lo - start, hi - start) if points else None
        spans = edit_service.whole_spans(
            {"shots": shots} if shots else (plan or {}), lo, hi
        )
        # The stored shots overlapping this segment — carry their kind/zoom so
        # a manual whole_frame (or later pan_x) change invalidates just this one
        # segment regardless of how the draft plan derives kinds.
        seg_shot = [
            s for s in (shots or [])
            if min(float(s.get("end") or 0), hi) - max(float(s.get("start") or 0), lo) > 0.02
        ]
        ass_text = None
        if resolved.subtitleStyle != "none" and transcript:
            ass_text = video_engine.build_ass(
                transcript,
                clip_start=lo,
                clip_end=hi,
                width=width,
                height=height,
                style=resolved.subtitleStyle,
            )
        hook = hook_line if seg["index"] == 0 and abs(lo - start) < 0.05 else None
        key = video_key(
            project_id=project.id,
            clip_index=clip_index,
            source=source,
            segment=seg,
            width=width,
            height=height,
            settings=resolved,
            points=seg_points,
            spans=spans,
            shot=seg_shot,
            ass_text=ass_text,
            hook=hook,
        )
        out = segment_path(key)
        was_cached = out.is_file() and out.stat().st_size >= 1024
        with _lock_for(key):
            if not was_cached:
                render_segment(
                    source,
                    key=key,
                    segment=seg,
                    width=width,
                    height=height,
                    settings=resolved,
                    src_w=src_w,
                    src_h=src_h,
                    has_audio=has_audio,
                    points=seg_points,
                    spans=spans,
                    ass_text=ass_text,
                    hook=hook,
                    logo=logo,
                    bgm=None,  # audio is built once, per-segment BGM is wasted
                )
                rendered += 1
        seg_meta.append(
            {"index": seg["index"], "start": seg["start"], "end": seg["end"],
             "key": key, "cached": was_cached, "frames": int(seg.get("frames") or 0)}
        )
        seg_meta[-1]["file"] = out.name
        if on_progress:
            on_progress(len(seg_meta) / (len(segments) + 1))
        if out.is_file():
            seg_meta[-1]["size"] = out.stat().st_size

    akey = audio_key(
        project_id=project.id,
        clip_index=clip_index,
        source=source,
        start=start,
        end=capped_end,
        settings=resolved,
        has_audio=has_audio,
    )
    aout = audio_path(akey)
    audio_elapsed = 0
    if has_audio or bgm:
        with _lock_for(f"audio-{akey}"):
            if not aout.is_file() or aout.stat().st_size < 512:
                aout, audio_elapsed = render_audio(
                    source,
                    key=akey,
                    start=start,
                    end=capped_end,
                    has_audio=has_audio,
                    settings=resolved,
                    bgm=bgm,
                )
            else:
                aout = aout
    else:
        aout = None

    signature = _digest(
        {"v": MANIFEST_VERSION, "segs": [m["key"] for m in seg_meta], "audio": akey,
         "size": [width, height]}
    )
    final = preview_path(project.id, clip_index, signature)
    with _lock_for(f"asm-{project.id}-{clip_index}"):
        if not final.is_file() or final.stat().st_size < 1024:
            assemble(
                [segment_path(m["key"]) for m in seg_meta],
                key=f"{project.id}-{clip_index:02d}",
                out_path=final,
                audio=aout,
                encoder=resolved.encoder,
            )
    if on_progress:
        on_progress(1.0)
    elapsed = int((time.time() - started) * 1000)

    manifest = {
        "version": MANIFEST_VERSION,
        "project": project.id,
        "clip_index": clip_index,
        "start": round(start, 3),
        "end": round(capped_end, 3),
        "duration": round(duration, 3),
        "truncated": truncated,
        "full_duration": round(end - start, 3),
        "width": width,
        "height": height,
        "segments": seg_meta,
        "audio_key": akey if aout else None,
        "audio_file": aout.name if aout else None,
        "file": final.name,
        "rendered_segments": rendered,
        "cached_segments": len(seg_meta) - rendered,
        "audio_elapsed_ms": audio_elapsed,
        "elapsed_ms": elapsed,
        "cache_bytes": sum(
            int(m.get("size") or 0) for m in seg_meta
        ) + (aout.stat().st_size if aout and aout.is_file() else 0)
        + (final.stat().st_size if final.is_file() else 0),
    }
    _write_manifest(project.id, clip_index, manifest)
    # Sweep dead segment files left by edits. Best effort: never fail a build
    # because the GC pass threw.
    try:
        prune_orphans()
    except Exception:  # noqa: BLE001
        logger.debug("prune_orphans failed", exc_info=True)
    return {
        "file": final.name,
        "url": preview_service.url_for(final),
        "cached": rendered == 0,
        "elapsed_ms": elapsed,
        "width": width,
        "height": height,
        "duration": round(duration, 3),
        "truncated": truncated,
        "full_duration": round(end - start, 3),
        "segments": seg_meta,
        "rendered_segments": rendered,
        "cached_segments": len(seg_meta) - rendered,
        "audio_elapsed_ms": audio_elapsed,
        "cache_bytes": manifest["cache_bytes"],
        "settings_hash": signature,
        "faces": preview_service._faces_meta(faces, plan),  # noqa: SLF001
    }
