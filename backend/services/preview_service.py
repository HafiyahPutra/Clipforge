"""Fast low-resolution preview renders for the editor (Phase 5).

A preview is a real FFmpeg render of one clip with the current editor
settings, only smaller (``PREVIEW_WIDTH``), faster (``PREVIEW_PRESET`` /
``PREVIEW_CRF``) and capped at ``PREVIEW_MAX_SECONDS`` so the UI can show
"this is what my edit looks like" in a couple of seconds.

Results are cached in ``temp/previews`` keyed by a hash of the clip window +
settings + source mtime, so repeated slider moves on the same settings are free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from backend import config
from backend.schemas.render import TARGET_SIZES, RenderSettingsModel
from backend.services import edit_service, media_service, project_store, video_engine

logger = logging.getLogger(__name__)


class PreviewInvalid(RuntimeError):
    """Raised when a preview cannot be produced."""


def _whole_spans(resolved: dict[str, Any], plan: dict[str, Any] | None, start: float, end: float):
    """Clip-relative whole_frame spans, or none when a streamer layout owns the frame."""
    spans = edit_service.whole_spans(plan or {}, start, end)
    if spans and edit_service.effective_streamer(resolved) in {"split_top_cam", "pip_corner"}:
        (plan or {}).setdefault("metrics", {}).setdefault("warnings", []).append(
            "whole_frame shots are ignored while a streamer layout is active"
        )
        return []
    return spans


def _faces_meta(faces: dict[str, Any] | None, plan: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Small, JSON-safe summary of a face path for the editor badge.

    The shot-plan metrics (shot count, cuts, zoom range) come from the plan, not
    from the cached tracking payload: the plan is rebuilt per render because it
    depends on the output size and on the (editable) shot list.
    """
    if not faces:
        return None
    metrics = faces.get("metrics") or {}
    shot_metrics = (plan or {}).get("metrics") or {}
    return {
        "detector": faces.get("detector", "none"),
        "coverage": float(faces.get("coverage", 0.0) or 0.0),
        "keyframes": len(faces.get("keyframes") or []),
        "clip_type": faces.get("clip_type", "auto"),
        "faces": int(faces.get("faces") or 0),
        "points": len(faces.get("trajectory") or []),
        "jitter_raw_pct": metrics.get("jitter_raw_pct"),
        "jitter_smoothed_pct": metrics.get("jitter_smoothed_pct"),
        "jitter_cut_pct": metrics.get("jitter_cut_pct"),
        "jumps_raw": metrics.get("jumps_raw"),
        "jumps_smoothed": metrics.get("jumps_smoothed"),
        "shots": len((plan or {}).get("shots") or []),
        "cuts": len((plan or {}).get("cuts") or []),
        "cuts_per_minute": shot_metrics.get("cuts_per_minute"),
        "avg_shot_seconds": shot_metrics.get("avg_shot_seconds"),
        "zoom_min": shot_metrics.get("zoom_min"),
        "zoom_max": shot_metrics.get("zoom_max"),
        "zoom_turns": shot_metrics.get("zoom_turns"),
        "cut_jumps_pct": shot_metrics.get("cut_jumps_pct"),
        "whole_frame_shots": shot_metrics.get("whole_frame_shots"),
        "whole_frame_seconds": shot_metrics.get("whole_frame_seconds"),
        "persons": shot_metrics.get("persons"),
        "speech_gate": shot_metrics.get("speech_gate"),
        "vad": (faces.get("vad") or {}).get("backend"),
        "speech_ratio": (faces.get("vad") or {}).get("speech_ratio"),
        "warnings": shot_metrics.get("warnings") or [],
    }


def _sidecar_path(out_path: Path) -> Path:
    return out_path.with_suffix(".faces.json")


def _write_sidecar(out_path: Path, faces: dict[str, Any] | None, plan: dict[str, Any] | None = None) -> None:
    """Remember the face summary next to the preview so cache hits stay instant."""
    meta = _faces_meta(faces, plan)
    try:
        if meta is None:
            _sidecar_path(out_path).unlink(missing_ok=True)
        else:
            _sidecar_path(out_path).write_text(json.dumps(meta), encoding="utf-8")
    except OSError:  # noqa: BLE001 - a missing sidecar only costs the badge
        logger.debug("Could not write preview sidecar for %s", out_path.name)


def _read_sidecar(out_path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(_sidecar_path(out_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _preview_size(settings: RenderSettingsModel) -> tuple[int, int]:
    width, height = TARGET_SIZES.get(settings.aspectRatio, TARGET_SIZES["9:16"])
    out_w = int(config.PREVIEW_WIDTH)
    out_h = max(2, int(round(out_w * height / width)))
    if out_h % 2:
        out_h += 1
    return out_w, out_h


def _signature(
    *,
    project_id: str,
    clip_index: int,
    start: float,
    end: float,
    settings: RenderSettingsModel,
    source: Path,
    shots_rev: str = "none",
) -> str:
    payload = {
        "v": 9,
        "project": project_id,
        "clip": clip_index,
        "start": round(start, 3),
        "end": round(end, 3),
        "settings": settings.model_dump(mode="json"),
        "src": source.name,
        "mtime": int(source.stat().st_mtime),
        # The edited shot list lives outside the project JSON, so it needs its
        # own slot in the key or a boundary move would serve the old preview.
        "shots": shots_rev,
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha1(blob).hexdigest()[:16]


def preview_path(project_id: str, clip_index: int, signature: str) -> Path:
    return config.PREVIEWS_DIR / f"{project_id}-{clip_index:02d}-{signature}.mp4"


def url_for(path: Path) -> str:
    return f"/api/preview/file/{path.name}"


def render_preview(
    project_id: str,
    clip_index: int,
    settings: RenderSettingsModel | None = None,
) -> dict[str, Any]:
    project = project_store.load(project_id)
    if not project.clips:
        raise PreviewInvalid("This project has no clips to preview.")
    if clip_index < 0 or clip_index >= len(project.clips):
        raise PreviewInvalid(f"Clip {clip_index} does not exist in this project.")

    clip = project.clips[clip_index]
    resolved = (settings or RenderSettingsModel()).model_copy(deep=True)

    source = media_service.ensure_source(project.model_dump())
    info = video_engine.probe(source)
    src_w = int(info.get("width") or 0)
    src_h = int(info.get("height") or 0)

    start, end = edit_service.clip_window(clip)
    capped = min(end, start + float(config.PREVIEW_MAX_SECONDS))
    if capped - start < edit_service.MIN_CLIP_SECONDS:
        capped = start + edit_service.MIN_CLIP_SECONDS
    duration = capped - start

    width, height = _preview_size(resolved)
    signature = _signature(
        project_id=project.id,
        clip_index=clip_index,
        start=start,
        end=capped,
        settings=resolved,
        source=source,
        shots_rev=edit_service.shots_rev(project.id, clip_index),
    )
    out_path = preview_path(project.id, clip_index, signature)
    if out_path.is_file() and out_path.stat().st_size > 1024:
        return {
            "file": out_path.name,
            "url": url_for(out_path),
            "cached": True,
            "elapsed_ms": 0,
            "width": width,
            "height": height,
            "duration": round(duration, 3),
            "faces": _read_sidecar(out_path),
            "settings_hash": signature,
        }

    faces = edit_service.face_path(
        source, start, capped, resolved, project_id=project.id, clip_index=clip_index
    )
    # The stored shot list wins over the draft: that is the whole point of the
    # editor — what you arranged is what gets previewed and exported.
    plan = edit_service.face_plan(
        faces,
        width=width,
        height=height,
        shots=edit_service.stored_shots(project.id, clip_index),
    )
    hook = edit_service.hook_line(resolved)

    transcript = [line.model_dump() for line in project.transcript]
    ass_text = None
    if resolved.subtitleStyle != "none" and transcript:
        ass_text = video_engine.build_ass(
            transcript,
            clip_start=start,
            clip_end=capped,
            width=width,
            height=height,
            style=resolved.subtitleStyle,
        )

    logo_path = None
    if resolved.watermarkImage:
        try:
            from backend.services import asset_service

            logo_path = asset_service.resolve("watermarks", resolved.watermarkImage)
        except Exception:  # noqa: BLE001 - missing asset is not fatal
            logo_path = None
    bgm_path = None
    if resolved.bgmFile:
        try:
            from backend.services import asset_service

            bgm_path = asset_service.resolve("bgm", resolved.bgmFile)
        except Exception:  # noqa: BLE001
            bgm_path = None

    config.PREVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    started = time.time()
    try:
        video_engine.render_clip(
            source,
            out_path,
            start=start,
            end=capped,
            width=width,
            height=height,
            background=resolved.background,
            encoder=resolved.encoder,
            ass_text=ass_text,
            has_audio=bool(info.get("has_audio")),
            watermark_text=resolved.watermark,
            watermark_image=str(logo_path) if logo_path else None,
            watermark_position=resolved.watermarkPosition,
            watermark_opacity=resolved.watermarkOpacity,
            watermark_size=resolved.watermarkSize,
            bgm_path=bgm_path,
            bgm_volume=resolved.bgmVolume,
            bgm_offset=resolved.bgmOffset,
            bgm_loop=resolved.bgmLoop,
            voice_boost=resolved.voiceBoost,
            src_size=(src_w, src_h),
            face_keyframes=(plan or {}).get("points")
            or edit_service.face_keyframes(faces, width=width, height=height),
            whole_spans=_whole_spans(resolved, plan, start, capped),
            facecam_face=edit_service.facecam_face(faces),
            streamer_preset=edit_service.effective_streamer(resolved),
            facecam_position=resolved.facecamPosition,
            hook_text=hook,
            hook_position=resolved.hookPosition,
            hook_duration=resolved.hookDuration,
            hook_size=resolved.hookSize,
            hook_case=resolved.hookCase,
            preview=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise PreviewInvalid(str(exc)[:400]) from exc

    elapsed = int((time.time() - started) * 1000)
    _write_sidecar(out_path, faces, plan)
    return {
        "file": out_path.name,
        "url": url_for(out_path),
        "cached": False,
        "elapsed_ms": elapsed,
        "width": width,
        "height": height,
        "duration": round(duration, 3),
        "faces": _faces_meta(faces, plan),
        "settings_hash": signature,
    }


def cleanup(max_age_seconds: int = 3600) -> int:
    """Drop stale preview files; returns how many were removed."""
    if not config.PREVIEWS_DIR.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for path in config.PREVIEWS_DIR.iterdir():
        if path.suffix not in (".mp4", ".json"):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                if path.suffix == ".mp4":
                    removed += 1
        except OSError:
            continue
    return removed
