"""Tahap 7c — shot list + activity → per-frame crop path with hard cuts.

This is where the three pieces meet: the shot list says *what* each range looks
like, the tracker's raw path says *where* the face is, and the zoom curve says
*how tight* the frame is. The output is a single dense point list that the
renderer turns into one FFmpeg pass.

The one rule that shapes this module: **smoothing never crosses a cut.** Each
shot is smoothed as its own array, so the filter state (One-Euro history,
dead-zone anchor, SavGol window, speed clamp) is reset at the boundary. That is
what makes the transition a clean snap to the new framing instead of a 200 ms
slide from the old face to the new one.

The dense list also carries a ``cut`` flag on the first point of every shot
after the first; the renderer uses it to emit two commands at the same timestamp
(old value, then new value) so no frame ever renders at an in-between position.
"""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from backend.services.shots import zoom as zoom_mod
from backend.services.shots.model import Shot, normalize, summary
from backend.services.tracking import smoother
from backend.services.tracking.trajectory import _fill_gaps

_JUMP = 0.02  # 2% of frame width = the "did it snap?" threshold


def _crop_box(width: int, height: int, src_w: int, src_h: int) -> tuple[int, int]:
    """Crop window matching the target aspect — must mirror the renderer."""
    target = width / float(height or 1)
    src_aspect = (src_w or 16) / float(src_h or 9)
    if target < src_aspect:
        return max(2, int(round(src_h * target))), src_h
    return src_w, max(2, int(round(src_w / target)))


def _steps_from(payload: dict[str, Any]) -> np.ndarray:
    raw = payload.get("raw") or {}
    count = len(raw.get("cx") or [])
    if count == 0:
        return np.zeros(0)
    duration = float(payload.get("duration") or 0.0)
    if duration <= 0:
        return np.zeros(0)
    return np.linspace(0.0, duration, count)


def _axis_stats(values: np.ndarray, keep: np.ndarray | None = None) -> tuple[float, float, int]:
    """(mean |Δ|, max |Δ|, jumps) ignoring the steps flagged in ``keep``."""
    if values.size < 2:
        return 0.0, 0.0, 0
    deltas = np.abs(np.diff(values))
    if keep is not None and keep.size == deltas.size:
        deltas = deltas[keep]
    if deltas.size == 0:
        return 0.0, 0.0, 0
    return float(deltas.mean()), float(deltas.max()), int((deltas > _JUMP).sum())


def build(
    payload: dict[str, Any],
    params: dict[str, Any],
    *,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    shots: Iterable[Shot | dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Per-frame path (x, y, z) + cut markers + metrics for the whole clip."""
    times = _steps_from(payload)
    if times.size == 0:
        return {"points": [], "shots": [], "metrics": {}, "cuts": []}

    raw = payload.get("raw") or {}
    raw_cx = np.asarray(raw.get("cx") or [], dtype=float)
    raw_cy = np.asarray(raw.get("cy") or [], dtype=float)
    raw_h = np.asarray(raw.get("h") or [], dtype=float)
    if raw_cx.size != times.size:
        raw_cx = np.full(times.size, np.nan)
        raw_cy = np.full(times.size, np.nan)
        raw_h = np.full(times.size, np.nan)

    activity = payload.get("activity") or {}
    talking_all = np.asarray(activity.get("talking") or [], dtype=float)
    jaw_all = np.asarray(activity.get("jaw") or [], dtype=float)
    talk_per_face = activity.get("talk") or []
    target_all = np.asarray(activity.get("target") or [], dtype=int)

    clip_start = float(payload.get("start") or 0.0)
    clip_end = float(payload.get("end") or (clip_start + float(times[-1])))
    duration = max(0.2, clip_end - clip_start)

    source_shots = shots if shots is not None else payload.get("shots")
    shot_list = normalize(source_shots or [], clip_start, clip_end)

    crop_w, crop_h = _crop_box(out_w, out_h, src_w, src_h)
    base_zoom = float(params.get("zoom", 1.0) or 1.0)

    # Zoom is an upscale: the crop is scaled up by z and cropped back, on top of
    # the upscale the vertical crop already costs. On a small source that turns
    # into mush, so the maximum zoom follows the source height rather than a
    # single number for every clip.
    warnings: list[str] = []
    zoom_cap = float(params.get("zoom_max", 1.4) or 1.4)
    if src_h < 1080:
        if src_h >= 720:
            limit = 1.25
        elif src_h >= 480:
            limit = 1.15
        else:
            limit = 1.05
        if limit < zoom_cap:
            warnings.append(
                f"source is {src_w}x{src_h}; zoom limited to {limit:.2f}x to avoid upscaling artefacts"
            )
            zoom_cap = limit
    if zoom_cap != float(params.get("zoom_max", 1.4) or 1.4):
        params = dict(params, zoom_max=zoom_cap)

    filled_cx = _fill_gaps(raw_cx, 0.5)
    filled_cy = _fill_gaps(raw_cy, 0.42)

    points: list[dict[str, Any]] = []
    shot_info: list[dict[str, Any]] = []
    cut_positions: list[float] = []      # clip-relative seconds
    cut_jumps: list[float] = []          # % of frame width
    smooth_cx_all: list[float] = []
    prev_x: float | None = None
    emitted = 0

    for order, shot in enumerate(shot_list):
        rel_start = max(0.0, float(shot.start) - clip_start)
        rel_end = min(duration, float(shot.end) - clip_start)
        if rel_end - rel_start <= 0:
            continue
        if order == len(shot_list) - 1:
            index = np.flatnonzero((times >= rel_start - 1e-6) & (times <= rel_end + 1e-6))
        else:
            index = np.flatnonzero((times >= rel_start - 1e-6) & (times < rel_end - 1e-6))
        if index.size < 2:
            continue

        # --- position: smoothed *inside* the shot only --------------------
        segment_cx = smoother.smooth_axis(filled_cx[index], times[index], params)
        segment_cy = smoother.smooth_axis(filled_cy[index], times[index], params)
        segment_cx = np.clip(segment_cx, 0.05, 0.95)
        segment_cy = np.clip(segment_cy, 0.08, 0.92)

        # Compute the auto centre before overriding so the UI can show it.
        auto_cx_mean = float(np.mean(segment_cx))

        # Manual crop centre: clamp so the crop window stays inside the frame.
        # The crop window is `crop_w / src_w` wide; the usable cx range is
        # [half_crop .. 1 - half_crop] in normalised coordinates. When a zoom
        # factor is active we also shrink the effective window.
        if shot.frame_cx is not None and shot.kind != "whole_frame":
            # Best-guess zoom for clamping: use median of the forthcoming
            # zoom_values, but we build those next — use the fixed value when
            # known, else the base zoom as a conservative estimate.
            z_guess = float(shot.zoom_value or base_zoom)
            effective_crop_w = crop_w / max(z_guess, 1.0)
            half = (effective_crop_w / float(src_w)) * 0.5
            
            # Ensure coordinates are within valid range (clamped) and even (for FFmpeg crop)
            clamped = float(np.clip(shot.frame_cx, half, 1.0 - half))
            
            # Convert to pixel for even check
            x_pix = int(clamped * src_w - (crop_w / z_guess) / 2.0)
            x_pix -= x_pix % 2
            # Convert back to normalized cx (approximate)
            segment_cx = np.full(index.size, (x_pix + (crop_w / z_guess) / 2.0) / src_w)
            
            segment_cy = smoother.smooth_axis(filled_cy[index], times[index], params)
            segment_cy = np.clip(segment_cy, 0.08, 0.92)
            y_pix = int(np.mean(segment_cy) * src_h - (crop_h / z_guess) / 2.0)
            y_pix -= y_pix % 2
            segment_cy = np.full(index.size, (y_pix + (crop_h / z_guess) / 2.0) / src_h)

        # --- zoom: own curve, own smoothing, own state --------------------
        fixed = shot.zoom_value
        face_ratio = raw_h[index] * float(src_h) / float(max(1, crop_h))
        if fixed is not None:
            zoom_values = np.full(index.size, float(fixed))
        else:
            talking_slice: np.ndarray | None = None
            if shot.kind == "follow_person" and (shot.person_id or shot.person) and talk_per_face:
                person_index = _person_index(str(shot.person_id or shot.person), data.get("person_index"))
                if person_index is not None and person_index < len(talk_per_face):
                    talking_slice = np.asarray(talk_per_face[person_index], dtype=float)[index] > 0.5
            if jaw_all.size == times.size:
                talking_slice = zoom_mod.talking_mask(jaw_all[index], params)
            if talking_slice is None and talking_all.size == times.size:
                talking_slice = talking_all[index] > 0.5
            if talking_slice is None and target_all.size == times.size:
                talking_slice = target_all[index] >= 0
            zoom_values = zoom_mod.build(
                times[index],
                talking_slice,
                np.nan_to_num(face_ratio, nan=0.0),
                params,
                entry=shot.zoom_entry,
            )
        zoom_values = np.clip(zoom_values * base_zoom, float(params.get("zoom_min", 1.0)),
                              float(params.get("zoom_max", 1.4)))

        is_cut = emitted > 0
        for position, step in enumerate(index):
            points.append(
                {
                    "t": round(float(times[step]), 3),
                    "x": round(float(segment_cx[position]), 5),
                    "y": round(float(segment_cy[position]), 5),
                    "z": round(float(zoom_values[position]), 4),
                    "cut": bool(is_cut and position == 0),
                }
            )
        smooth_cx_all.extend(float(value) for value in segment_cx)

        if is_cut:
            cut_positions.append(round(float(times[index[0]]), 3))
            if prev_x is not None:
                cut_jumps.append(round(abs(float(segment_cx[0]) - prev_x) * 100.0, 2))
        prev_x = float(segment_cx[-1])
        emitted += 1

        info = shot.to_dict()
        # Compute clamp bounds for this shot so the UI can render the slider
        # correctly without knowing src_w or zoom.
        z_guess = float(shot.zoom_value or base_zoom)
        effective_crop_w = crop_w / max(z_guess, 1.0)
        half = (effective_crop_w / float(src_w)) * 0.5
        info.update(
            {
                "clipStart": round(rel_start, 3),
                "clipEnd": round(rel_end, 3),
                "samples": int(index.size),
                # Framing hint for the UI slider.
                "autoCx": round(auto_cx_mean, 5),
                "cxMin": round(float(np.clip(half, 0.0, 0.5)), 5),
                "cxMax": round(float(np.clip(1.0 - half, 0.5, 1.0)), 5),
            }
        )
        # Report the centre the renderer will actually use. A stored frameCx
        # outside the crop window is silently clamped at render time, so echoing
        # the raw value would make the slider claim a framing the clip never got.
        if info.get("frameCx") is not None and shot.kind != "whole_frame":
            info["frameCx"] = round(float(np.clip(shot.frame_cx, half, 1.0 - half)), 5)
        info.update(zoom_mod.describe(zoom_values))
        shot_info.append(info)

    # --- metrics ---------------------------------------------------------
    # Steps that straddle a cut are excluded from the jitter comparison: a hard
    # cut is *supposed* to move the crop, and counting it would flatter the
    # smoothing (or punish it) for the wrong reason.
    keep = np.ones(max(0, times.size - 1), dtype=bool)
    for cut in cut_positions:
        position = int(np.searchsorted(times, cut))
        for offset in (position - 1, position):
            if 0 <= offset < keep.size:
                keep[offset] = False

    smooth_arr = np.asarray(smooth_cx_all, dtype=float)
    raw_jitter, raw_peak, raw_jumps = _axis_stats(filled_cx, keep)

    # The smoothed path is a concatenation of shots, so its own cut markers are
    # exact: a delta that *lands* on a cut point is a framing change, not motion.
    cut_flags = np.asarray([bool(point["cut"]) for point in points], dtype=bool)
    smooth_keep = ~cut_flags[1:] if cut_flags.size > 1 else np.zeros(0, dtype=bool)
    smooth_jitter, smooth_peak, smooth_jumps = _axis_stats(smooth_arr, smooth_keep)

    stats = summary(shot_list)
    zoom_all = np.asarray([point["z"] for point in points], dtype=float) if points else np.ones(1)
    # Zoom smoothness: the spec asks for no "goyang", which shows up as rapid
    # direction changes and large per-step steps. Cut deltas are excluded — a
    # deliberate snap is not wobble.
    step_dt = float(np.median(np.diff(times))) if times.size > 1 else 0.1
    zoom_delta = np.abs(np.diff(zoom_all)) if zoom_all.size > 1 else np.zeros(1)
    zoom_keep = smooth_keep if smooth_keep.size == zoom_delta.size else np.ones(zoom_delta.size, dtype=bool)
    zoom_delta_kept = zoom_delta[zoom_keep] if zoom_keep.size == zoom_delta.size else zoom_delta
    zoom_signed = np.diff(zoom_all) if zoom_all.size > 1 else np.zeros(1)
    zoom_signed_kept = zoom_signed[zoom_keep] if zoom_keep.size == zoom_signed.size else zoom_signed
    zoom_turns = int(
        sum(
            1
            for a, b in zip(zoom_signed_kept, zoom_signed_kept[1:])
            if a * b < 0 and abs(a) > 1e-4 and abs(b) > 1e-4
        )
    )
    metrics = {
        "jitter_raw_pct": round(raw_jitter * 100, 3),
        "jitter_smoothed_pct": round(smooth_jitter * 100, 3),
        "jitter_cut_pct": round(100.0 * (1.0 - smooth_jitter / raw_jitter), 1)
        if raw_jitter > 1e-6
        else 0.0,
        "peak_raw_pct": round(raw_peak * 100, 2),
        "peak_smoothed_pct": round(smooth_peak * 100, 2),
        "jumps_raw": raw_jumps,
        "jumps_smoothed": smooth_jumps,
        "cuts": len(cut_positions),
        "cuts_per_minute": round(len(cut_positions) / max(0.001, duration / 60.0), 2),
        "cut_jumps_pct": cut_jumps,
        "cut_jump_max_pct": round(max(cut_jumps), 2) if cut_jumps else 0.0,
        "zoom_min": round(float(zoom_all.min()), 3),
        "zoom_max": round(float(zoom_all.max()), 3),
        "zoom_mean": round(float(zoom_all.mean()), 3),
        "zoom_step_max": round(float(zoom_delta_kept.max()) if zoom_delta_kept.size else 0.0, 4),
        "zoom_speed_max": round(
            float(zoom_delta_kept.max() / max(1e-3, step_dt)) if zoom_delta_kept.size else 0.0, 3
        ),
        "zoom_turns": zoom_turns,
        "zoom_cap": round(zoom_cap, 2),
        "warnings": warnings,
        "shots": stats["shots"],
        "avg_shot_seconds": stats["avg_shot_seconds"],
        "min_shot_seconds": stats["min_shot_seconds"],
        "kinds": stats["kinds"],
        # Counted from the list this plan was built with, so a manual
        # "whole_frame" edit is reflected immediately instead of showing the
        # detection draft's numbers.
        "whole_frame_shots": sum(1 for shot in shot_list if shot.kind == "whole_frame"),
        "whole_frame_seconds": round(
            sum(shot.duration for shot in shot_list if shot.kind == "whole_frame"), 3
        ),
        "steps": int(times.size),
        "crop_box": [int(crop_w), int(crop_h)],
        "src_size": [int(src_w), int(src_h)],
    }

    return {
        "points": points,
        "shots": shot_info,
        "cuts": cut_positions,
        "metrics": metrics,
        "source": "manual" if any(shot.source == "manual" for shot in shot_list) else "auto",
    }


def _person_index(label: str, mapping: dict[str, int] | None = None) -> int | None:
    """Map "A"/"B"/"C" (or "1"/"2") to a track index.

    ``mapping`` is the payload's ``person_index``: after the user ignores or
    merges people, row *i* of the talk matrix is no longer person *i* of the
    alphabet, so the letter has to be resolved through the same table the
    People panel used.
    """
    text = str(label or "").strip()
    if not text:
        return None
    if mapping:
        for key in (text, text.upper(), text.lower()):
            if key in mapping:
                try:
                    return int(mapping[key])
                except (TypeError, ValueError):
                    break
    text = text.upper()
    if text.isdigit():
        return max(0, int(text) - 1)
    if len(text) == 1 and "A" <= text <= "Z":
        return ord(text) - ord("A")
    return None
