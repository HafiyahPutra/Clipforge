"""Shared helpers: clip trim window, face path, clip type and hook line.

Both the full render (`render_service`) and the editor preview
(`preview_service`) resolve the same values here so "what you preview" and
"what you export" stay identical.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from backend.services import face_service, people, people_store, shot_store
from backend.services.shots import plan as shot_plan
from backend.services.tracking import config as tracking_config
from backend.services.tracking import pipeline as tracking_pipeline

logger = logging.getLogger("clipforge.edit")

MIN_CLIP_SECONDS = 0.4
STREAMER_PRESETS = ("split_top_cam", "pip_corner")
MAX_PATH_POINTS = 240  # keeps the FFmpeg crop expression compact


def stored_shots(project_id: str, clip_index: int) -> list[dict[str, Any]] | None:
    """The user's edited shot list for a clip, or ``None`` when there is none.

    Render and preview both call this so an edit reaches the export without any
    extra plumbing — the shot list is the source of truth, not the draft.
    """
    try:
        raw = shot_store.load(project_id, clip_index)
    except shot_store.ShotStoreError:
        return None
    shots = (raw or {}).get("shots")
    return shots if isinstance(shots, list) and shots else None


def shots_rev(project_id: str, clip_index: int) -> str:
    """Short hash of the stored list — folded into the preview cache key.

    Without this a preview would keep serving the pre-edit framing: the project
    JSON does not change when a boundary moves, so nothing else in the key would.
    """
    shots = stored_shots(project_id, clip_index)
    if not shots:
        return "none"
    blob = json.dumps(shots, sort_keys=True, default=str).encode()
    return hashlib.sha1(blob).hexdigest()[:10]


def clip_window(clip: Any) -> tuple[float, float]:
    """Resolve the effective in/out of a clip (editor trim wins over AI cut)."""
    start = clip.trim_start if getattr(clip, "trim_start", None) is not None else clip.start_time
    end = clip.trim_end if getattr(clip, "trim_end", None) is not None else clip.end_time
    start = max(0.0, float(start))
    end = float(end)
    if end - start < MIN_CLIP_SECONDS:
        end = start + MIN_CLIP_SECONDS
    return round(start, 3), round(end, 3)


def clip_type(settings: Any) -> str | None:
    """Selected clip type (``None`` = let the tracker auto-detect)."""
    value = getattr(settings, "clipType", None)
    if value is None:
        return None
    value = str(value).strip().lower()
    if value in {"", "auto", "none"}:
        return None
    return value if value in tracking_config.CLIP_TYPES else None


def wants_face(settings: Any) -> bool:
    return bool(getattr(settings, "faceTracking", False)) or (
        getattr(settings, "streamerPreset", "none") in STREAMER_PRESETS
    )


def apply_people(
    data: dict[str, Any] | None,
    project_id: str | None,
    clip_index: int = 0,
) -> dict[str, Any] | None:
    """Fold the user's people corrections into a tracking payload.

    The tracker does not know that a face was a poster or that two ids are the
    same guest — the People panel stores that as a sidecar. Re-deriving here (a
    few ms of NumPy over the cached series) means preview, render and the editor
    all see the corrected list without any of them knowing about corrections.
    """
    if not data or not project_id:
        return data
    if not data.get("series"):
        return data  # legacy cache entry: nothing to re-derive from
    try:
        state = people_store.load_state(project_id, clip_index)
    except people_store.PeopleStoreError:
        return data
    if not state:
        return data
    try:
        state = people.ensure_state(state, data["series"])
        return tracking_pipeline.rebuild(data, state)
    except Exception as exc:  # noqa: BLE001 - a correction must never break a render
        logger.warning("could not apply people corrections: %s", exc)
        return data


def face_path(
    source: Any,
    start: float,
    end: float,
    settings: Any,
    *,
    project_id: str | None = None,
    clip_index: int = 0,
) -> dict[str, Any] | None:
    """Detect (or reuse cached) face data for the clip window."""
    if not wants_face(settings):
        return None
    try:
        data = face_service.detect(source, start, end, clip_type=clip_type(settings))
    except Exception:  # noqa: BLE001 - tracking is best effort
        return None
    data = apply_people(data, project_id, clip_index)
    if not data.get("available") and not data.get("trajectory"):
        return None
    if not data.get("trajectory") and not data.get("face"):
        return None
    return data


def face_plan(
    data: dict[str, Any] | None,
    *,
    width: int | None = None,
    height: int | None = None,
    shots: list[Any] | None = None,
) -> dict[str, Any] | None:
    """Full framing plan (path + shots + metrics) for one clip window.

    This is the Tahap 7 entry point: the shot list decides *what* each range
    looks like, the tracked path decides *where* the face is, and the zoom curve
    decides *how tight* the frame is. Everything is smoothed per shot, so a cut
    is a clean snap rather than a slide.
    """
    if not data:
        return None
    if not data.get("raw"):
        return None  # legacy cache entry — caller falls back to the old path
    size = (data.get("metrics") or {}).get("src_size") or []
    if len(size) == 2:
        src_w, src_h = int(size[0]), int(size[1])
    else:
        src_w, src_h = 1920, 1080
    out_w = int(width or 1080)
    out_h = int(height or 1920)
    params = tracking_config.resolve(data.get("clip_type") or "auto")
    try:
        plan = shot_plan.build(
            data,
            params,
            src_w=src_w,
            src_h=src_h,
            out_w=out_w,
            out_h=out_h,
            shots=shots if shots is not None else data.get("shots"),
        )
    except Exception:  # noqa: BLE001 - framing must never break a render
        return None
    # The draft's own numbers (how many whole_frame shots, how many people, which
    # gate was used) live on the tracking payload, not on the framing plan, so
    # carry them across for the sidecar and the debug HUD.
    draft = data.get("shot_metrics") or {}
    metrics = plan.setdefault("metrics", {})
    for key in ("whole_frame_shots", "whole_frame_seconds", "overlap_spans", "persons", "speech_gate"):
        # The plan already counts whole_frame shots from the list it was built
        # with; the draft value is only a fallback for older payloads.
        if key in draft and key not in metrics:
            metrics[key] = draft[key]
    return plan


def whole_spans(plan: dict | None, start: float, end: float) -> list[tuple[float, float]]:
    """Clip-relative `(t0, t1)` spans where the plan says `whole_frame`.

    The engine composites the untouched landscape frame over the face-follow
    crop inside these spans, so it needs them relative to the trimmed clip,
    which starts at `start` on the source timeline.
    """
    spans: list[tuple[float, float]] = []
    for shot in (plan or {}).get("shots") or []:
        if str(shot.get("kind")) != "whole_frame":
            continue
        lo = max(float(start), float(shot.get("start") or start))
        hi = min(float(end), float(shot.get("end") or end))
        if hi - lo > 0.05:
            spans.append((round(lo - start, 3), round(hi - start, 3)))
    return spans

def face_keyframes(
    data: dict[str, Any] | None,
    *,
    width: int | None = None,
    height: int | None = None,
    shots: list[Any] | None = None,
) -> list[dict[str, Any]] | None:
    """Crop path for the renderer: ``{t, x, y, z, cut}`` per analysis step.

    Phase 5 handed over ≤10 keyframes, which is why the crop moved in visible
    steps; Phase 6 passed the dense trajectory. Now each point also carries the
    zoom factor and a ``cut`` flag, so one `sendcmd` script drives position and
    one expression drives zoom — still a single FFmpeg pass.

    No decimation here: `sendcmd` holds a value until the next command, so the
    point count only affects file size, and dropping points could drop a cut.
    """
    if not data:
        return None

    plan = face_plan(data, width=width, height=height, shots=shots)
    if plan and plan.get("points"):
        return plan["points"]

    # Fallback: cached payload from before Tahap 7 (no raw arrays). Keep the old
    # behaviour so an existing cache still renders instead of losing the face.
    points = data.get("trajectory") or data.get("keyframes") or []
    points = [item for item in points if isinstance(item, dict) and "x" in item]
    if len(points) < 2:
        return None
    if len(points) > MAX_PATH_POINTS:
        stride = len(points) / float(MAX_PATH_POINTS)
        picked = [points[int(i * stride)] for i in range(MAX_PATH_POINTS - 1)]
        picked.append(points[-1])
        points = picked
    return points


def facecam_face(data: dict[str, Any] | None) -> dict[str, Any] | None:
    if not data:
        return None
    return data.get("face") or None


def effective_streamer(settings: Any) -> str:
    """Streamer layout: explicit choice wins, otherwise the clip type's layout.

    A "gaming" clip type implies the top-cam layout, but never overrides a
    layout the user picked by hand.
    """
    chosen = getattr(settings, "streamerPreset", "none") or "none"
    if chosen in STREAMER_PRESETS:
        return chosen
    name = clip_type(settings)
    if not name:
        return "none"
    layout = tracking_config.CLIP_TYPES[name].get("layout")
    return layout if layout in STREAMER_PRESETS else "none"


def hook_line(settings: Any) -> str | None:
    """Compose ``prefix + hookText + suffix`` into one line (or None)."""
    parts = [
        getattr(settings, "hookPrefix", None),
        getattr(settings, "hookText", None),
        getattr(settings, "hookSuffix", None),
    ]
    text = " ".join(" ".join(part.split()) for part in parts if part)
    text = " ".join(text.split())
    if not text:
        return None
    return text[:120]
