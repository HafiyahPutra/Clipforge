"""FFmpeg video engine: probe, subtitle (.ass) generation, vertical encode.

Cut a segment out of the source, reframe it to the target aspect ratio
(letterboxed on black or a blurred copy of itself), burn a karaoke `.ass`
subtitle track, overlay a watermark and mix background music (Phase 4), then
encode with `h264_videotoolbox` on Apple Silicon (falling back to `libx264`).

Everything here is synchronous; `render_service` runs it in a worker thread.
"""

from __future__ import annotations

import json
import logging
import math
import platform
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from backend import config

logger = logging.getLogger("clipforge.render")

ProgressFn = Callable[[float], None]


class RenderError(RuntimeError):
    """Raised when ffmpeg cannot render a clip."""


# --- ffmpeg capability detection ---------------------------------------------

_CAPS_LOCK = threading.Lock()
_CAPS: dict[str, Any] | None = None


def _which(candidate: str) -> str | None:
    if not candidate:
        return None
    found = shutil.which(candidate)
    if found:
        return found
    path = Path(candidate).expanduser()
    return str(path) if path.is_file() else None


def _ffmpeg_banner(path: str) -> str:
    try:
        completed = subprocess.run(
            [path, "-hide_banner", "-version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    first = (completed.stdout or "").splitlines()
    return first[0].strip() if first else ""


_ENCODER_RE = re.compile(r"^\s*[VAS][A-Z.]{5}\s+(\S+)")
_FILTER_RE = re.compile(r"^\s*[TSC.]{2,3}\s+(\S+)\s")


def _ffmpeg_listing(path: str, flag: str) -> set[str]:
    try:
        completed = subprocess.run(
            [path, "-hide_banner", flag], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    pattern = _ENCODER_RE if flag == "-encoders" else _FILTER_RE
    names: set[str] = set()
    for line in (completed.stdout or "").splitlines():
        match = pattern.match(line)
        if match:
            names.add(match.group(1))
    return names


def _describe(path: str) -> dict[str, Any]:
    filters = _ffmpeg_listing(path, "-filters")
    encoders = _ffmpeg_listing(path, "-encoders")
    return {
        "path": path,
        "version": _ffmpeg_banner(path),
        "libass": "ass" in filters or "subtitles" in filters,
        "drawtext": "drawtext" in filters,
        "audio_filters": {"amix", "volume", "afade", "alimiter"} <= filters,
        "videotoolbox": "h264_videotoolbox" in encoders,
        "libx264": "libx264" in encoders,
    }


def capabilities() -> dict[str, Any]:
    """Detect ffmpeg once: prefer a build that can burn subtitles (libass)."""
    global _CAPS
    with _CAPS_LOCK:
        if _CAPS is not None:
            return _CAPS

        candidates = [config.FFMPEG_BIN, *config.FFMPEG_FALLBACK_BINS]
        best: dict[str, Any] | None = None
        for candidate in candidates:
            resolved = _which(candidate)
            if not resolved:
                continue
            info = _describe(resolved)
            if best is None:
                best = info
            if info["libass"]:
                best = info
                break

        if best is None:
            _CAPS = {
                "path": config.FFMPEG_BIN,
                "version": "",
                "libass": False,
                "drawtext": False,
                "audio_filters": False,
                "videotoolbox": False,
                "libx264": False,
            }
        else:
            _CAPS = best
        logger.info(
            "ffmpeg: %s (libass=%s, videotoolbox=%s, libx264=%s)",
            _CAPS["path"],
            _CAPS["libass"],
            _CAPS["videotoolbox"],
            _CAPS["libx264"],
        )
        return _CAPS


def pick_encoder(choice: str | None) -> str:
    """Resolve `auto|videotoolbox|cpu` to an actual ffmpeg encoder name."""
    caps = capabilities()
    wants_hw = (choice or "auto") in {"auto", "videotoolbox"}
    if wants_hw and caps["videotoolbox"] and platform.system() == "Darwin":
        return "h264_videotoolbox"
    if caps["libx264"]:
        return "libx264"
    return "h264_videotoolbox" if caps["videotoolbox"] else "libx264"


def hardware_info() -> dict[str, Any]:
    caps = capabilities()
    encoder = pick_encoder(config.FFMPEG_ENCODER)
    try:
        from backend.services import face_service

        faces = face_service.available()
    except Exception:  # pragma: no cover - optional dependency
        faces = False
    note = None
    if not caps["libass"]:
        note = (
            "This ffmpeg build has no libass, so subtitles are skipped. "
            "Install a build with libass (macOS: `brew install ffmpeg-full`, "
            "Debian: `apt-get install ffmpeg`) and set FFMPEG_BIN."
        )
    return {
        "encoder": encoder,
        "platform": f"{platform.system()} {platform.machine()}",
        "ffmpeg": caps["version"],
        "ffmpeg_path": caps["path"],
        "videotoolbox": caps["videotoolbox"],
        "libx264": caps["libx264"],
        "subtitles": caps["libass"],
        "overlays": caps["drawtext"],
        "audio_filters": caps["audio_filters"],
        "face_tracking": faces,
        "note": note,
    }


# --- ASS subtitles ------------------------------------------------------------

# Colours use ASS' &HBBGGRR layout. With karaoke (`\k`) the *secondary* colour is
# what a word shows before it is sung and the *primary* colour is the highlight.
_STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "viral_pop": {
        "font": "Arial Black",
        "scale": 1.0,
        "primary": "&H0000E5FF",  # amber highlight
        "secondary": "&H00FFFFFF",
        "outline": "&H00000000",
        "outline_w": 6,
        "shadow": 2,
    },
    "beast_punch": {
        "font": "Arial Black",
        "scale": 1.12,
        "primary": "&H0000FFFF",  # yellow
        "secondary": "&H00FFFFFF",
        "outline": "&H00000000",
        "outline_w": 9,
        "shadow": 3,
    },
    "cyber_violet": {
        "font": "Arial Black",
        "scale": 1.0,
        "primary": "&H00FF64C8",  # violet
        "secondary": "&H00FFFFFF",
        "outline": "&H00201008",
        "outline_w": 6,
        "shadow": 2,
    },
    "fire_red": {
        "font": "Arial Black",
        "scale": 1.05,
        "primary": "&H000040FF",  # red-orange
        "secondary": "&H00FFFFFF",
        "outline": "&H00000000",
        "outline_w": 7,
        "shadow": 3,
    },
    "electric_cyan": {
        "font": "Arial Black",
        "scale": 1.0,
        "primary": "&H00FFFF00",  # cyan
        "secondary": "&H00FFFFFF",
        "outline": "&H00300000",
        "outline_w": 6,
        "shadow": 2,
    },
    "golden_aura": {
        "font": "Arial Black",
        "scale": 1.0,
        "primary": "&H0000D7FF",  # gold
        "secondary": "&H00FFFFFF",
        "outline": "&H00102040",
        "outline_w": 7,
        "shadow": 3,
    },
    "clean_minimal": {
        "font": "Helvetica",
        "scale": 0.86,
        "primary": "&H00FFFFFF",
        "secondary": "&H00E6E6E6",
        "outline": "&H00202020",
        "outline_w": 2,
        "shadow": 0,
    },
}


def _ass_time(seconds: float) -> str:
    total = max(0.0, seconds)
    hours = int(total // 3600)
    minutes = int((total % 3600) // 60)
    secs = total % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def _ass_escape(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    cleaned = cleaned.replace("\\", "\\\\").replace("{", "(").replace("}", ")")
    return cleaned


def _karaoke_text(words: list[str], duration: float) -> str:
    """Hand every word its own `\\k` slot, proportional to its length."""
    if not words:
        return ""
    weights = [max(2, len(re.sub(r"\W", "", word)) + 1) for word in words]
    total_weight = sum(weights)
    centis = max(len(words), int(round(duration * 100)))
    assigned = 0
    chunks: list[str] = []
    for index, word in enumerate(words):
        share = (
            centis - assigned
            if index == len(words) - 1
            else max(1, int(round(centis * weights[index] / total_weight)))
        )
        assigned += share
        chunks.append(r"{\k" + str(share) + "}" + _ass_escape(word))
    return " ".join(chunks)


def _caption_chunks(words: list[str], max_chars: int = 18, max_words: int = 3) -> list[list[str]]:
    """Break a transcript line into short caption bursts (TikTok-style)."""
    chunks: list[list[str]] = []
    current: list[str] = []
    for word in words:
        candidate = [*current, word]
        too_long = len(" ".join(candidate)) > max_chars or len(candidate) > max_words
        if current and too_long:
            chunks.append(current)
            current = [word]
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def build_ass(
    lines: list[dict[str, Any]],
    *,
    clip_start: float,
    clip_end: float,
    width: int,
    height: int,
    style: str = "viral_pop",
) -> str:
    """Render transcript lines inside the clip window into an `.ass` document.

    Each line is split into short caption bursts; every word carries a `\\k`
    karaoke slot so the highlight follows the voice.
    """
    preset = _STYLE_PRESETS.get(style) or _STYLE_PRESETS["viral_pop"]
    font_size = int(round(height * 0.045 * float(preset["scale"])))
    margin_v = int(round(height * 0.16))
    margin_h = int(round(width * 0.04))

    header = [
        "[Script Info]",
        "; Generated by ClipForge",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour,"
        " BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle,"
        " BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,{font},{size},{primary},{secondary},{outline},&H64000000,-1,0,0,0,"
        "100,100,1,0,1,{outline_w},{shadow},2,{margin_h},{margin_h},{margin_v},1".format(
            font=preset["font"],
            size=font_size,
            primary=preset["primary"],
            secondary=preset["secondary"],
            outline=preset["outline"],
            outline_w=preset["outline_w"],
            shadow=preset["shadow"],
            margin_h=margin_h,
            margin_v=margin_v,
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    events: list[str] = []
    for line in lines:
        try:
            start = float(line.get("start", 0.0))
            end = float(line.get("end", 0.0))
        except (TypeError, ValueError):
            continue
        text = str(line.get("text") or "").strip()
        if not text or end <= start:
            continue
        if end <= clip_start or start >= clip_end:
            continue

        visible_start = max(start, clip_start)
        visible_end = min(end, clip_end)
        duration = visible_end - visible_start
        if duration <= 0.05:
            continue

        words = [word for word in text.split(" ") if word]
        if not words:
            continue
        chunks = _caption_chunks(words)
        total_weight = sum(max(1, len(" ".join(chunk))) for chunk in chunks)
        cursor = visible_start - clip_start
        for position, chunk in enumerate(chunks):
            weight = max(1, len(" ".join(chunk)))
            span = duration * weight / total_weight
            if position == len(chunks) - 1:
                span = max(0.05, visible_end - clip_start - cursor)
            chunk_start = cursor
            chunk_end = min(visible_end - clip_start, cursor + span)
            cursor = chunk_end
            if chunk_end - chunk_start <= 0.04:
                continue
            events.append(
                "Dialogue: 0,{start},{end},Default,,0,0,0,,{text}".format(
                    start=_ass_time(chunk_start),
                    end=_ass_time(chunk_end),
                    text=_karaoke_text(chunk, chunk_end - chunk_start),
                )
            )

    return "\n".join(header + events) + "\n"


# --- encoding ----------------------------------------------------------------


def _encoder_args(encoder: str, *, preview: bool = False) -> list[str]:
    if encoder == "h264_videotoolbox":
        bitrate = "3M" if preview else config.RENDER_VIDEO_BITRATE
        return [
            "-c:v",
            "h264_videotoolbox",
            "-b:v",
            bitrate,
            "-maxrate",
            bitrate,
            "-bufsize",
            "16M",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        config.PREVIEW_PRESET if preview else config.RENDER_PRESET,
        "-crf",
        str(config.PREVIEW_CRF if preview else config.RENDER_CRF),
        "-pix_fmt",
        "yuv420p",
    ]


# --- overlays & audio (Phase 4) ----------------------------------------------

_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)
_FONT_PATH: str | None | bool = False  # False = not looked up yet


def _font_file() -> str | None:
    """First usable TrueType font (drawtext needs one unless fontconfig is on)."""
    global _FONT_PATH
    if _FONT_PATH is not False:
        return _FONT_PATH  # type: ignore[return-value]

    candidates: list[Path] = []
    if config.FONTS_DIR.is_dir():
        candidates += sorted(
            path for path in config.FONTS_DIR.iterdir() if path.suffix.lower() in {".ttf", ".otf"}
        )
    candidates += [Path(item) for item in _FONT_CANDIDATES]
    _FONT_PATH = next((str(path) for path in candidates if path.is_file()), None)
    return _FONT_PATH


def _filter_escape(value: str) -> str:
    """Escape a path/value used inside a filtergraph option."""
    return (
        value.replace("\\", "\\\\\\\\")
        .replace(":", "\\:")
        .replace("'", "\\\\'")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def _position_expr(position: str, *, text: bool) -> tuple[str, str]:
    """x/y expressions for drawtext (`tw`/`th`) or overlay (`w`/`h`)."""
    w = "tw" if text else "w"
    h = "th" if text else "h"
    margin = 48
    if position == "top_left":
        return f"{margin}", f"{margin}"
    if position == "top_right":
        return f"W-{w}-{margin}", f"{margin}"
    if position == "bottom_left":
        return f"{margin}", f"H-{h}-{margin}"
    if position == "center":
        return f"(W-{w})/2", f"(H-{h})/2"
    return f"W-{w}-{margin}", f"H-{h}-{margin}"


def _clampf(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _piecewise_expr(points: list[tuple[float, float]]) -> str:
    """Piecewise-linear FFmpeg expression over `t` (used for the crop path).

    The expression is a *flat sum* of gated segments rather than nested
    ``if(lt(t,…),…)`` calls: FFmpeg's expression parser is recursive and fails
    ("Failed to configure input pad") once a dense path produces a few hundred
    nested levels. Summing ``segment * between(t,…)`` keeps the tree shallow at
    any point count — neighbouring segments agree at their shared boundary, so
    the curve stays continuous.
    """
    if not points:
        return "0"
    if len(points) == 1:
        return f"{points[0][1]:.1f}"

    terms: list[str] = [f"{points[0][1]:.1f}*lt(t,{points[0][0]:.3f})"]
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        span = max(0.001, t1 - t0)
        slope = (v1 - v0) / span
        terms.append(
            f"({v0:.1f}+{slope:.2f}*(t-{t0:.3f}))*between(t,{t0:.3f},{t1:.3f})"
        )
    terms.append(f"{points[-1][1]:.1f}*gt(t,{points[-1][0]:.3f})")
    return "(" + "+".join(terms) + ")"


def _crop_box(width: int, height: int, src_w: int, src_h: int) -> tuple[int, int]:
    """Crop window (cw, ch) inside the source that matches the target aspect."""
    target = width / float(height or 1)
    src_aspect = (src_w or 16) / float(src_h or 9)
    if target < src_aspect:  # target is narrower → cut the sides
        return max(2, int(round(src_h * target))), src_h
    return src_w, max(2, int(round(src_w / target)))


def _facecam_crop(face: dict[str, Any] | None, src_w: int, src_h: int) -> tuple[int, int, int]:
    """Square facecam crop ``(x, y, side)`` centred on the detected face."""
    if face and float(face.get("w") or 0) > 0:
        face_w = float(face["w"]) * src_w
        face_h = float(face.get("h") or 0) * src_h
        # Head-and-shoulders framing: ~2.4x the face height.
        side = max(face_h * 2.4, face_w * 2.2, min(src_w, src_h) * 0.22)
        side = min(side, min(src_w, src_h) * 0.9)
        center_x = float(face["x"]) * src_w
        center_y = float(face["y"]) * src_h + side * 0.10
    else:
        side = min(src_w, src_h) * 0.5
        center_x, center_y = src_w / 2.0, src_h * 0.42
    # Ensure dimensions are even and clamped
    side = int(min(side, src_w, src_h))
    side = max(2, side - (side % 2))
    
    x = int(_clampf(center_x - side / 2.0, 0, max(0, src_w - side)))
    x -= x % 2
    y = int(_clampf(center_y - side / 2.0, 0, max(0, src_h - side)))
    y -= y % 2
    return x, y, side


def _sum_terms(terms: list[str], chunk: int = 12) -> str:
    """Add terms in small parenthesised groups.

    FFmpeg's expression parser is recursive, so a 300-term left-nested sum is
    300 levels deep and eventually fails to configure. Grouping keeps the depth
    at ``chunk + len(terms)/chunk`` instead of ``len(terms)``.
    """
    if not terms:
        return "0"
    groups: list[str] = []
    for start in range(0, len(terms), chunk):
        groups.append("(" + "+".join(terms[start : start + chunk]) + ")")
    if len(groups) == 1:
        return groups[0]
    return _sum_terms(groups, chunk)


def _rdp(points: list[tuple[float, float]], epsilon: float) -> list[tuple[float, float]]:
    """Ramer–Douglas–Peucker simplification of a (t, value) polyline.

    A zoom ramp is smooth, so a 30 fps curve collapses to a handful of segments
    that are visually identical — which is what keeps the FFmpeg expression
    short enough to evaluate every frame.
    """
    if len(points) < 3:
        return list(points)
    start, end = points[0], points[-1]
    span = end[0] - start[0]
    worst = 0.0
    index = 0
    for position in range(1, len(points) - 1):
        t, value = points[position]
        if span <= 1e-9:
            predicted = start[1]
        else:
            predicted = start[1] + (end[1] - start[1]) * (t - start[0]) / span
        error = abs(value - predicted)
        if error > worst:
            worst, index = error, position
    if worst <= epsilon:
        return [start, end]
    left = _rdp(points[: index + 1], epsilon)
    right = _rdp(points[index:], epsilon)
    return left[:-1] + right


def _split_at_cuts(
    points: list[tuple[float, float, float, bool]],
) -> list[list[tuple[float, float, float, bool]]]:
    """Group path points into per-shot runs, meeting exactly at the cut."""
    groups: list[list[tuple[float, float, float, bool]]] = []
    current: list[tuple[float, float, float, bool]] = []
    for point in points:
        if point[3] and current:
            # Both sides share one timestamp so the *last* command written for
            # that instant wins: old value, then new value. Pulled 1 ms early so
            # the first frame on/after the cut already renders the new framing
            # (a frame's PTS rarely lands exactly on the requested time).
            boundary = max(0.0, float(point[0]) - 0.001)
            current[-1] = (boundary, current[-1][1], current[-1][2], current[-1][3])
            groups.append(current)
            current = [(boundary, point[1], point[2], point[3])]
        else:
            current.append(point)
    if current:
        groups.append(current)
    return groups


def _densify(
    points: list[tuple[float, float, float, bool]], fps: float
) -> list[tuple[float, float, float, bool]]:
    """Linearly resample the path at the output frame rate, shot by shot.

    `sendcmd` sets a value and holds it until the next command, so a 10 Hz path
    would move in 10 visible steps per second. Resampling at `fps` turns the
    staircase back into the same linear ramp the old expression produced.

    Resampling happens *inside* each shot: interpolating across a cut would
    smear the crop from the old face to the new one over several frames, which is
    exactly the slide that hard cuts must not have.
    """
    if len(points) < 2 or fps <= 0:
        return points

    out: list[tuple[float, float, float, bool]] = []
    for group in _split_at_cuts(points):
        if len(group) < 2:
            out.extend(group)
            continue
        times = [pt[0] for pt in group]
        span = times[-1] - times[0]
        if span <= 0:
            out.extend(group)
            continue
        count = max(2, int(round(span * fps)) + 1)
        index = 0
        for position in range(count):
            t = times[0] + span * position / (count - 1)
            while index + 1 < len(times) and times[index + 1] < t:
                index += 1
            t0, x0, y0, flag0 = group[index]
            if index + 1 >= len(group):
                out.append((t, x0, y0, flag0 and position == 0))
                continue
            t1, x1, y1, _ = group[index + 1]
            ratio = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
            ratio = min(1.0, max(0.0, ratio))
            out.append(
                (
                    t,
                    x0 + (x1 - x0) * ratio,
                    y0 + (y1 - y0) * ratio,
                    flag0 and position == 0,
                )
            )
    return out


def _sendcmd_path(
    work_dir: Path,
    points: list[tuple[float, float, float, bool]],
    *,
    crop_w: int,
    crop_h: int,
    src_w: int,
    src_h: int,
    fps: float = 30.0,
) -> str:
    """Write the crop path as a `sendcmd` script and return its file name.

    FFmpeg's expression evaluator chokes on long expressions (a few hundred
    terms is enough to fail with "Failed to configure input pad"), so a dense
    per-frame path cannot ride inside `crop=x='…'`. `sendcmd` instead updates
    `crop`'s runtime `x`/`y` options once per path point, which keeps the
    expression empty and the motion frame-accurate.
    """
    max_x = float(max(0, src_w - crop_w))
    max_y = float(max(0, src_h - crop_h))
    points = _densify(points, fps)
    lines: list[str] = []
    for t, cx, cy, _cut in points:
        x = int(round(_clampf(cx * src_w - crop_w / 2.0, 0.0, max_x)))
        y = int(round(_clampf(cy * src_h - crop_h / 2.0, 0.0, max_y)))
        stamp = f"{max(0.0, t):.3f}"
        lines.append(f"{stamp} crop x {x};")
        if max_y > 0:
            lines.append(f"{stamp} crop y {y};")
    name = f"path-{uuid.uuid4().hex[:12]}.cmd"
    (work_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return name


def _zoom_expr(points: list[tuple[float, float, bool]], *, epsilon: float = 0.0035) -> str | None:
    """Zoom factor as a single FFmpeg expression over `t`, or None when flat.

    Crop *size* cannot change per frame in FFmpeg, so zoom is applied the other
    way round: the cropped frame is scaled up to ``W*z x H*z`` with
    ``eval=frame`` and then centre-cropped back to ``W x H``. Scaling up and
    cropping down is mathematically the same as shrinking the crop window, but it
    keeps `crop`'s geometry constant — which is the only form that survives a
    single pass.

    The curve is simplified per shot (RDP) and rebuilt as ``base + Σ ramp +
    Σ jump``: ramps are ``clip()`` so they saturate instead of needing nested
    conditionals, and each hard cut contributes one ``gte()`` step. That means
    the expression has a few dozen terms instead of one per frame, and — more
    importantly — the jump is a genuine discontinuity, so the new zoom is in
    effect on the first frame of the new shot with nothing in between.
    """
    if len(points) < 2:
        return None
    if max(abs(value - 1.0) for _, value, _ in points) < 0.002:
        return None

    groups: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    for t, value, cut in points:
        if cut and current:
            boundary = max(0.0, float(t) - 0.001)
            current[-1] = (boundary, current[-1][1])
            groups.append(current)
            current = [(boundary, float(value))]
        else:
            current.append((float(t), float(value)))
    if current:
        groups.append(current)

    base = groups[0][0][1]
    terms: list[str] = []
    previous_end: float | None = None
    for group_index, group in enumerate(groups):
        simplified = _rdp(group, epsilon)
        if group_index > 0:
            jump = simplified[0][1] - float(previous_end if previous_end is not None else simplified[0][1])
            if abs(jump) > 1e-4:
                terms.append(f"{jump:.5f}*gte(t,{simplified[0][0]:.3f})")
        for (t0, v0), (t1, v1) in zip(simplified, simplified[1:]):
            span = max(0.001, t1 - t0)
            slope = (v1 - v0) / span
            if abs(slope) < 1e-5:
                continue
            terms.append(f"{slope:.5f}*clip(t-{t0:.3f},0,{span:.3f})")
        previous_end = simplified[-1][1]

    if not terms:
        # Constant zoom (a shot with a fixed factor): still a valid expression.
        return f"{base:.4f}"
    return f"{base:.4f}+" + _sum_terms(terms)


def _zoom_scale(width: int, height: int, zoom: str | None) -> str:
    """The `scale` stage: fixed-size when there is no zoom, per-frame when there is."""
    if not zoom:
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
    return (
        f"scale=w='trunc({width}*({zoom})/2)*2':"
        f"h='trunc({height}*({zoom})/2)*2':eval=frame,"
        f"crop={width}:{height}"
    )


def _main_chain(
    label: str,
    width: int,
    height: int,
    *,
    background: str,
    src_w: int,
    src_h: int,
    keyframes: list[dict[str, Any]] | None,
    out: str = "base",
    work_dir: Path | None = None,
    fps: float = 30.0,
) -> str:
    """Scale/crop the source into `width`x`height`, optionally following a face."""
    prefix = f"[{label}]"
    if keyframes and src_w > 0 and src_h > 0:
        crop_w, crop_h = _crop_box(width, height, src_w, src_h)
        if work_dir is not None and len(keyframes) > 2:
            points = [
                (
                    float(item["t"]),
                    float(item["x"]),
                    float(item.get("y") if item.get("y") is not None else 0.5),
                    bool(item.get("cut")),
                )
                for item in keyframes
            ]
            name = _sendcmd_path(
                work_dir,
                points,
                crop_w=crop_w,
                crop_h=crop_h,
                src_w=src_w,
                src_h=src_h,
                fps=fps,
            )
            zoom = _zoom_expr(
                [
                    (
                        float(item["t"]),
                        float(item.get("z") or 1.0),
                        bool(item.get("cut")),
                    )
                    for item in keyframes
                ]
            )
            return (
                f"{prefix}sendcmd=f={name},crop={crop_w}:{crop_h}:0:0,"
                f"{_zoom_scale(width, height, zoom)},setsar=1[{out}]"
            )
        points = [
            (
                float(item["t"]),
                _clampf(
                    float(item["x"]) * src_w - crop_w / 2.0,
                    0.0,
                    float(max(0, src_w - crop_w)),
                ),
            )
            for item in keyframes
        ]
        y_expr = str(max(0, (src_h - crop_h) // 2))
        return (
            f"{prefix}crop={crop_w}:{crop_h}:'{_piecewise_expr(points)}':{y_expr},"
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1[{out}]"
        )
    if background == "blurred":
        return (
            f"{prefix}split=2[mbg][mfg];"
            f"[mbg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},boxblur=24:2,eq=brightness=-0.08[bg];"
            f"[mfg]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2[{out}]"
        )
    return (
        f"{prefix}scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black[{out}]"
    )


def _facecam_chain(
    label: str,
    *,
    width: int,
    height: int,
    src_w: int,
    src_h: int,
    face: dict[str, Any] | None,
    preset: str,
) -> tuple[str, int, int]:
    """Build the facecam branch; returns (graph fragment, cam width, cam height)."""
    x, y, side = _facecam_crop(face, src_w, src_h)
    if preset == "split_top_cam":
        cam_h = max(2, int(round(height * 0.36)))
        cam_h -= cam_h % 2
        return (
            f"[{label}]crop={side}:{side}:{x}:{y},"
            f"scale={width}:{cam_h}:force_original_aspect_ratio=increase,"
            f"crop={width}:{cam_h},setsar=1[cam]",
            width,
            cam_h,
        )
    cam_w = max(2, int(round(width * 0.30)))
    cam_w -= cam_w % 2
    return (
        f"[{label}]crop={side}:{side}:{x}:{y},scale={cam_w}:{cam_w},setsar=1[cam]",
        cam_w,
        cam_w,
    )


def _whole_frame_chain(
    label: str,
    width: int,
    height: int,
    *,
    blur: int = 24,
    dim: float = -0.08,
    out: str = "wf",
) -> str:
    """Letterbox the *uncropped* frame into `width`x`height` over a blurred copy.

    Used for whole_frame shots: nobody is framed, so the landscape original is
    shown whole and the empty space is filled with a blurred, dimmed enlargement
    of the same frame instead of black bars.
    """
    return (
        f"[{label}]split=2[wf_a][wf_b];"
        f"[wf_a]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur={blur}:2,eq=brightness={dim}[wf_bg];"
        f"[wf_b]scale={width}:{height}:force_original_aspect_ratio=decrease[wf_fg];"
        f"[wf_bg][wf_fg]overlay=(W-w)/2:(H-h)/2,setsar=1[{out}]"
    )


def _enable_expr(spans: list[tuple[float, float]]) -> str:
    """`between(t,a,b)+between(t,c,d)` for a list of clip-relative spans."""
    terms = [f"between(t,{max(0.0, lo):.3f},{max(0.0, hi):.3f})" for lo, hi in spans if hi > lo]
    return "+".join(terms) if terms else "0"


def _fit_hook(
    text: str, size_pct: float, width: int, font_path: str | None
) -> tuple[float, str]:
    """Shrink the hook font until the single line fits the frame, then trim if needed.

    `drawtext` neither wraps nor shrinks: a hook wider than the canvas is clipped
    on both sides because it is centred with x=(w-text_w)/2. Measure the real line,
    lower the size until it fits, and if even the minimum size overflows, cut the
    tail with an ellipsis so nothing is ever chopped mid-glyph.
    """
    base = float(size_pct or 7.0)
    if not text or width <= 0:
        return base, text
    budget = width * 0.90

    def measure(px: int, value: str) -> float:
        try:
            from PIL import ImageFont

            font = ImageFont.truetype(font_path, px) if font_path else ImageFont.load_default()
            return float(font.getlength(value))
        except Exception:  # no font file / old Pillow: fall back to an em estimate
            return len(value) * px * 0.60

    def size_at(pct: float) -> int:
        return max(18, int(round(width * pct / 100.0)))

    if measure(size_at(base), text) <= budget:
        return base, text
    lo, hi = 3.0, base
    for _ in range(14):
        mid = (lo + hi) / 2.0
        if measure(size_at(mid), text) <= budget:
            lo = mid
        else:
            hi = mid
    size = round(lo, 2)
    if measure(size_at(size), text) <= budget:
        return size, text
    trimmed = text
    while trimmed and measure(size_at(size), trimmed + "…") > budget:
        trimmed = trimmed[:-1].rstrip()
    if trimmed != text:
        trimmed = f"{trimmed}…"
    return size, trimmed



def _filtergraph(
    *,
    width: int,
    height: int,
    background: str,
    has_subtitles: bool,
    fps: int,
    watermark_text: str | None = None,
    watermark_text_file: str | None = None,
    watermark_image_label: str | None = None,
    watermark_position: str = "bottom_right",
    watermark_opacity: float = 0.75,
    watermark_size: int = 0,
    font_file: str | None = None,
    src_size: tuple[int, int] | None = None,
    face_keyframes: list[dict[str, Any]] | None = None,
    facecam_face: dict[str, Any] | None = None,
    work_dir: Path | None = None,
    streamer_preset: str = "none",
    facecam_position: str = "top_right",
    hook_text_file: str | None = None,
    hook_size: float = 7.0,
    hook_position: str = "top",
    hook_duration: float = 4.0,
    duration: float = 0.0,
    whole_spans: list[tuple[float, float]] | None = None,
) -> str:
    src_w, src_h = src_size or (0, 0)
    whole_spans = [span for span in (whole_spans or []) if span[1] > span[0]]
    keyframes = [item for item in (face_keyframes or []) if "x" in item]
    if src_w <= 0 or src_h <= 0:
        keyframes = []
        logger.warning("Invalid source dimensions (%s, %s) for main_chain; disabling framing", src_w, src_h)
            
    layout = streamer_preset if streamer_preset in {"split_top_cam", "pip_corner"} else "none"
    if layout != "none" and (src_w <= 0 or src_h <= 0):
        layout = "none"

    cam_w = cam_h = 0
    if layout == "split_top_cam":
        cam_h = max(2, int(round(height * 0.36)))
        cam_h -= cam_h % 2
        main_h = height - cam_h
        graph = (
            f"[0:v]split=2[main0][cam0];"
            + _main_chain(
                "main0",
                width,
                main_h,
                background=background,
                src_w=src_w,
                src_h=src_h,
                keyframes=keyframes,
                out="main",
                work_dir=work_dir,
                fps=float(fps),
            )
            + ";"
        )
        cam_fragment, cam_w, cam_h = _facecam_chain(
            "cam0",
            width=width,
            height=height,
            src_w=src_w,
            src_h=src_h,
            face=facecam_face,
            preset=layout,
        )
        graph += cam_fragment + ";[cam][main]vstack=inputs=2[base]"
    elif layout == "pip_corner":
        graph = (
            "[0:v]split=2[main0][cam0];"
            + _main_chain(
                "main0",
                width,
                height,
                background=background,
                src_w=src_w,
                src_h=src_h,
                keyframes=keyframes,
                out="main",
                work_dir=work_dir,
                fps=float(fps),
            )
            + ";"
        )
        cam_fragment, cam_w, cam_h = _facecam_chain(
            "cam0",
            width=width,
            height=height,
            src_w=src_w,
            src_h=src_h,
            face=facecam_face,
            preset=layout,
        )
        margin = max(12, int(round(width * 0.035)))
        if facecam_position == "top_left":
            overlay_x, overlay_y = margin, margin
        elif facecam_position == "bottom_left":
            overlay_x, overlay_y = margin, height - cam_h - margin
        elif facecam_position == "bottom_right":
            overlay_x, overlay_y = width - cam_w - margin, height - cam_h - margin
        else:  # top_right
            overlay_x, overlay_y = width - cam_w - margin, margin
        graph += (
            cam_fragment
            + f";[main][cam]overlay={overlay_x}:{overlay_y}:format=auto,"
            f"drawbox=x={overlay_x}:y={overlay_y}:w={cam_w}:h={cam_h}:"
            f"color=white@0.85:t=4[base]"
        )
    else:
        main_src = "0:v"
        if whole_spans and src_w > 0 and src_h > 0:
            # Two consumers of the same source: one cropped/following, one left
            # whole. The whole_frame copy is composited on top only inside its
            # spans, so a clip without overlap keeps the single-path graph.
            graph = "[0:v]split=2[mainsrc][wfsrc];"
            main_src = "mainsrc"
        else:
            graph = ""
        graph += _main_chain(
            main_src,
            width,
            height,
            background=background,
            src_w=src_w,
            src_h=src_h,
            keyframes=keyframes,
            work_dir=work_dir,
            fps=float(fps),
        )
        if whole_spans and src_w > 0 and src_h > 0:
            graph += ";" + _whole_frame_chain("wfsrc", width, height, out="wf") + ";"
            graph += (
                f"[base][wf]overlay=0:0:format=auto:enable='{_enable_expr(whole_spans)}'[basewf]"
            )

    base_label = "basewf" if (whole_spans and src_w > 0 and src_h > 0 and layout == "none") else "base"
    graph += f";[{base_label}]fps={fps},setsar=1,format=yuv420p[clean]"
    current = "clean"
    if has_subtitles:
        graph += f";[{current}]ass=subs.ass[subbed]"
        current = "subbed"

    opacity = min(1.0, max(0.0, float(watermark_opacity)))
    if watermark_text and watermark_text_file:
        font_size = max(16, int(round(width * ((watermark_size or 0) / 100 or 0.035))))
        x, y = _position_expr(watermark_position, text=True)
        font = (
            f"fontfile='{_filter_escape(font_file)}'"
            if font_file
            else "font='Sans'"
        )
        graph += (
            f";[{current}]drawtext={font}:textfile={watermark_text_file}"
            f":fontcolor=white@{opacity:.2f}:fontsize={font_size}:x={x}:y={y}"
            f":box=1:boxcolor=black@0.35:boxborderw={max(8, font_size // 3)}[wmtext]"
        )
        current = "wmtext"

    if watermark_image_label:
        logo_w = max(32, int(round(width * ((watermark_size or 0) / 100 or 0.14))))
        x, y = _position_expr(watermark_position, text=False)
        graph += (
            f";[{watermark_image_label}]format=rgba,"
            f"colorchannelmixer=aa={opacity:.2f},scale={logo_w}:-1[wmimg];"
            f"[{current}][wmimg]overlay={x}:{y}:format=auto[wmover]"
        )
        current = "wmover"

    if hook_text_file:
        font_size = max(18, int(round(width * (float(hook_size or 7.0) / 100.0))))
        y_expr = {
            "top": "h*0.10",
            "center": "(h-text_h)/2",
            "bottom": "h*0.64",
        }.get(hook_position, "h*0.10")
        if layout == "split_top_cam" and hook_position == "top" and cam_h:
            # The cam strip owns the top of the frame — drop the hook under it.
            y_expr = f"{cam_h}+h*0.04"
        window = max(0.2, float(hook_duration or 0.0))
        enable = (
            f":enable='between(t,0,{min(window, duration):.2f})'"
            if duration and 0 < window < duration
            else ""
        )
        font = (
            f"fontfile='{_filter_escape(font_file)}'"
            if font_file
            else "font='Sans'"
        )
        graph += (
            f";[{current}]drawtext={font}:textfile={hook_text_file}"
            f":fontcolor=white:fontsize={font_size}:x=(w-text_w)/2:y={y_expr}"
            f":borderw={max(3, font_size // 12)}:bordercolor=black@0.85"
            f":box=1:boxcolor=black@0.55:boxborderw={max(10, font_size // 3)}"
            f"{enable}[hook]"
        )
        current = "hook"

    graph += f";[{current}]null[vout]"
    return graph


def _audio_graph(
    *,
    duration: float,
    has_audio: bool,
    voice_boost: float,
    bgm_label: str | None,
    bgm_volume: float,
) -> tuple[str, str | None]:
    """Build the audio half of `-filter_complex`.

    Returns `(fragment, output_label)`; the label is None when there is nothing
    to map (silent source and no BGM).
    """
    parts: list[str] = []
    base: str | None = None
    if has_audio:
        boost = min(2.0, max(0.0, float(voice_boost)))
        if abs(boost - 1.0) > 0.01:
            parts.append(f"[0:a]volume={boost:.2f},alimiter=limit=0.95[a0]")
        else:
            parts.append("[0:a]anull[a0]")
        base = "a0"

    if bgm_label:
        fade = max(0.0, float(config.BGM_FADE_SECONDS))
        fade = min(fade, max(0.1, duration / 4))
        fade_out = max(0.0, duration - fade)
        volume = min(1.0, max(0.0, float(bgm_volume)))
        parts.append(
            f"[{bgm_label}]volume={volume:.2f},"
            f"afade=t=in:st=0:d={fade:.2f},"
            f"afade=t=out:st={fade_out:.2f}:d={fade:.2f},"
            f"atrim=0:{duration:.3f},asetpts=N/SR/TB[abgm]"
        )
        if base:
            parts.append(
                f"[{base}][abgm]amix=inputs=2:duration=first:dropout_transition=0:"
                f"normalize=0[aout]"
            )
        else:
            parts.append("[abgm]anull[aout]")
        return ";".join(parts), "aout"

    if base:
        parts.append(f"[{base}]anull[aout]")
        return ";".join(parts), "aout"
    return ";".join(parts), None


def _parse_progress(line: str) -> float | None:
    match = re.match(r"out_time=(\d+):(\d{2}):(\d{2}(?:\.\d+)?)", line.strip())
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def render_clip(
    source: Path,
    out_path: Path,
    *,
    start: float,
    end: float,
    width: int,
    height: int,
    background: str = "blurred",
    encoder: str | None = None,
    ass_text: str | None = None,
    fps: int | None = None,
    has_audio: bool = True,
    watermark_text: str | None = None,
    watermark_image: Path | None = None,
    watermark_position: str = "bottom_right",
    watermark_opacity: float = 0.75,
    watermark_size: int = 0,
    bgm_path: Path | None = None,
    bgm_volume: float = 0.25,
    bgm_offset: float = 0.0,
    bgm_loop: bool = True,
    voice_boost: float = 1.0,
    # Phase 5 — face tracking, streamer layout, hook text, preview quality.
    src_size: tuple[int, int] | None = None,
    face_keyframes: list[dict[str, Any]] | None = None,
    facecam_face: dict[str, Any] | None = None,
    streamer_preset: str = "none",
    facecam_position: str = "top_right",
    hook_text: str | None = None,
    hook_position: str = "top",
    hook_duration: float = 4.0,
    hook_size: float = 7.0,
    hook_case: str = "upper",
    preview: bool = False,
    on_progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
    whole_spans: list[tuple[float, float]] | None = None,
    frames: int | None = None,
) -> int:
    """Cut `[start, end)` out of `source` and write an MP4 to `out_path`.

    Phase 4 adds a burned-in watermark (text or logo), background music with
    fades and a voice-boost stage. Returns the output size in bytes.

    Phase 8.2 adds `frames`: when set, the output is cut to exactly that many
    frames. The segmented preview needs this because it concatenates the pieces
    with `-c copy` — a single missing frame at a join becomes a 33 ms skip in the
    middle of the timeline, and `-t <seconds>` cannot guarantee a frame count
    when the cut does not land on a frame boundary. The seek is floored to the
    microsecond for the same reason: rounding it up skips the frame that starts
    a hair after the requested time.
    """
    if frames:
        duration = max(0.2, float(frames) / float(fps or config.RENDER_FPS))
    else:
        duration = max(0.2, float(end) - float(start))
    caps = capabilities()
    binary = caps["path"]
    codec = pick_encoder(encoder)
    fps = int(fps or config.RENDER_FPS)
    seek_at = math.floor(max(0.0, float(start)) * 1e6) / 1e6 if frames else max(0.0, float(start))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = out_path.parent
    ass_path = work_dir / "subs.ass"
    use_subs = bool(ass_text) and caps["libass"]
    if use_subs:
        ass_path.write_text(str(ass_text), encoding="utf-8")
    elif ass_path.exists():
        ass_path.unlink(missing_ok=True)

    wm_text = (watermark_text or "").strip()
    wm_text_file: str | None = None
    wm_text_path: Path | None = None
    if wm_text and caps["drawtext"]:
        wm_text_file = f"watermark-{uuid.uuid4().hex[:8]}.txt"
        wm_text_path = work_dir / wm_text_file
        wm_text_path.write_text(wm_text, encoding="utf-8")
    elif wm_text:
        logger.warning("ffmpeg has no drawtext filter — watermark text skipped")

    logo_path = watermark_image if watermark_image and Path(watermark_image).is_file() else None
    music_path = bgm_path if bgm_path and Path(bgm_path).is_file() else None
    if music_path and not caps["audio_filters"]:
        logger.warning("ffmpeg lacks amix/afade — background music skipped")
        music_path = None

    hook_value = " ".join((hook_text or "").split())
    if hook_value and str(hook_case).lower() == "upper":
        hook_value = hook_value.upper()
    hook_size_fit = hook_size
    if hook_value:
        hook_size_fit, hook_value = _fit_hook(hook_value, hook_size, width, _font_file())
    hook_file: str | None = None
    if hook_value and caps["drawtext"]:
        hook_file = f"hook-{uuid.uuid4().hex[:8]}.txt"
        (work_dir / hook_file).write_text(hook_value, encoding="utf-8")
    elif hook_value:
        logger.warning("ffmpeg has no drawtext filter — hook text skipped")

    graph = _filtergraph(
        width=width,
        height=height,
        background=background,
        has_subtitles=use_subs,
        fps=fps,
        watermark_text=wm_text or None,
        watermark_text_file=wm_text_file,
        watermark_image_label="1:v" if logo_path else None,
        watermark_position=watermark_position,
        watermark_opacity=watermark_opacity,
        watermark_size=watermark_size,
        font_file=_font_file(),
        src_size=src_size,
        face_keyframes=face_keyframes,
        work_dir=work_dir,
        facecam_face=facecam_face,
        streamer_preset=streamer_preset,
        facecam_position=facecam_position,
        hook_text_file=hook_file,
        hook_size=hook_size_fit,
        hook_position=hook_position,
        hook_duration=hook_duration,
        duration=duration,
        whole_spans=whole_spans,
    )

    command = [
        binary,
        "-y",
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-ss",
        f"{seek_at:.6f}" if frames else f"{seek_at:.3f}",
        "-i",
        str(source.resolve()),
    ]

    bgm_label: str | None = None
    next_index = 1
    if logo_path:
        command += ["-i", str(logo_path)]
        next_index = 2
    if music_path:
        if bgm_loop:
            command += ["-stream_loop", "-1"]
        if bgm_offset > 0:
            command += ["-ss", f"{float(bgm_offset):.3f}"]
        command += ["-i", str(music_path)]
        bgm_label = f"{next_index}:a"

    audio_fragment, audio_label = _audio_graph(
        duration=duration,
        has_audio=has_audio,
        voice_boost=voice_boost,
        bgm_label=bgm_label,
        bgm_volume=bgm_volume,
    )
    full_graph = f"{graph};{audio_fragment}" if audio_fragment else graph

    command += ["-t", f"{duration:.6f}" if frames else f"{duration:.3f}", "-filter_complex", full_graph, "-map", "[vout]"]
    if audio_label:
        command += [
            "-map",
            f"[{audio_label}]",
            "-c:a",
            "aac",
            "-b:a",
            config.RENDER_AUDIO_BITRATE,
            "-ac",
            "2",
        ]
    command += _encoder_args(codec, preview=preview)
    if frames:
        # Exact frame count, and no `-shortest`: the per-segment audio is thrown
        # away at concat time, so letting a 1-frame-short audio track trim the
        # video would reintroduce the very skip this parameter exists to avoid.
        command += ["-frames:v", str(int(frames)), "-r", str(fps), "-movflags", "+faststart", str(out_path)]
    else:
        command += ["-r", str(fps), "-movflags", "+faststart", "-shortest", str(out_path.resolve())]

    timeout = max(180.0, duration * 20 + 120)
    logger.info(
        "render %s → %s (%s, %.1fs, wm=%s, bgm=%s, boost=%.2f)",
        source.name,
        out_path.name,
        codec,
        duration,
        bool(wm_text or logo_path),
        bool(music_path),
        voice_boost,
    )

    # Track all commands for debugging
    logger.info("ffmpeg command: %s", " ".join(command))

    try:
        process = subprocess.Popen(
            command,
            cwd=str(work_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # Capture stderr separately
            text=True,
        )
    except OSError as exc:
        raise RenderError(f"ffmpeg could not start: {exc}") from exc

    assert process.stdout is not None
    assert process.stderr is not None
    
    # Read both streams to avoid buffer deadlocks
    import queue
    import io

    stdout_queue = queue.Queue()
    stderr_buffer = io.StringIO()

    def enqueue_out(stream, q):
        for line in iter(stream.readline, ''):
            q.put(line)
        stream.close()

    def buffer_err(stream, buf):
        for line in iter(stream.readline, ''):
            buf.write(line)
        stream.close()

    t1 = threading.Thread(target=enqueue_out, args=(process.stdout, stdout_queue))
    t2 = threading.Thread(target=buffer_err, args=(process.stderr, stderr_buffer))
    t1.start()
    t2.start()

    try:
        # Progress parsing
        while True:
            try:
                line = stdout_queue.get(timeout=0.1)
                if cancel is not None and cancel.is_set():
                    process.kill()
                    raise RenderError("Render cancelled.")
                seconds = _parse_progress(line)
                if seconds is not None and on_progress is not None:
                    on_progress(min(0.99, seconds / duration))
            except queue.Empty:
                if process.poll() is not None:
                    break
        
        t1.join()
        t2.join()
        return_code = process.wait(timeout=timeout)
    except Exception as exc:
        process.kill()
        raise RenderError(f"ffmpeg render error: {exc}") from exc
    finally:
        # ... (same cleanup)
        if ass_path.exists():
            ass_path.unlink(missing_ok=True)
        if wm_text_path is not None and wm_text_path.exists():
            wm_text_path.unlink(missing_ok=True)
        if hook_file is not None:
            (work_dir / hook_file).unlink(missing_ok=True)
        for stale in work_dir.glob("path-*.cmd"):
            stale.unlink(missing_ok=True)

    if return_code != 0 or not out_path.exists():
        err_out = stderr_buffer.getvalue()
        logger.error("ffmpeg failed (code %d):\nCommand: %s\nStderr: %s", return_code, " ".join(command), err_out)
        
        # Extract last few relevant non-empty lines
        last_lines = [line.strip() for line in err_out.splitlines() if line.strip()]
        if not last_lines:
            message = "ffmpeg process exited with non-zero status or produced no output file"
        else:
            message = last_lines[-1]
        raise RenderError(f"ffmpeg failed: {message[:400]}")

    if on_progress is not None:
        on_progress(1.0)
    return out_path.stat().st_size


def concat_clips(
    parts: list[Path],
    out_path: Path,
    *,
    cancel: threading.Event | None = None,
) -> int:
    """Join rendered clips into one file (merged compilation).

    Every part is produced with identical codec parameters, so the concat
    demuxer can copy streams; if that fails we re-encode as a fallback.
    """
    if not parts:
        raise RenderError("Nothing to merge.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    listing = out_path.parent / f"{out_path.stem}.concat.txt"
    listing.write_text(
        "\n".join(f"file '{part.resolve().as_posix()}'" for part in parts) + "\n",
        encoding="utf-8",
    )
    binary = capabilities()["path"]
    base = [
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
    ]
    attempts = [
        [*base, "-c", "copy", "-movflags", "+faststart", str(out_path)],
        [
            *base,
            "-c:v",
            "libx264",
            "-preset",
            config.RENDER_PRESET,
            "-crf",
            str(config.RENDER_CRF),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            config.RENDER_AUDIO_BITRATE,
            "-movflags",
            "+faststart",
            str(out_path),
        ],
    ]
    last_error = "unknown error"
    try:
        for command in attempts:
            if cancel is not None and cancel.is_set():
                raise RenderError("Render cancelled.")
            completed = subprocess.run(command, capture_output=True, text=True, timeout=900)
            if completed.returncode == 0 and out_path.exists():
                return out_path.stat().st_size
            last_error = (completed.stderr or completed.stdout or last_error).strip()[-400:]
    finally:
        listing.unlink(missing_ok=True)
    raise RenderError(f"ffmpeg could not merge the clips: {last_error}")


def probe(path: str | Path) -> dict[str, Any]:
    """ffprobe wrapper returning the fields the renderer cares about."""
    binary = shutil.which(config.FFPROBE_BIN) or config.FFPROBE_BIN
    command = [
        binary,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderError(f"ffprobe failed: {exc}") from exc
    if completed.returncode != 0:
        raise RenderError(f"ffprobe could not read {Path(path).name}")

    payload = json.loads(completed.stdout or "{}")
    fmt = payload.get("format", {})
    video = next((s for s in payload.get("streams", []) if s.get("codec_type") == "video"), {})
    return {
        "duration": float(fmt.get("duration") or video.get("duration") or 0.0),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": any(s.get("codec_type") == "audio" for s in payload.get("streams", [])),
    }
