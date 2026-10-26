"""Tahap 6 — turn tracked faces into one crop path, plus a smoothness metric.

The output is a dense list of normalised centres on the ``expr_hz`` grid. The
renderer converts it into an FFmpeg ``crop`` expression, so the camera pans
between those points instead of stepping between a handful of keyframes.

``metrics`` compares the raw (unsmoothed) path with the final one so the effect
of the pipeline is measurable instead of anecdotal:

    jitter_raw / jitter_smoothed   mean |Δcentre| per step, in % of frame width
    peak_raw / peak_smoothed       largest single-step move, in % of the width
    jumps_raw / jumps_smoothed     steps that move more than 2% of the width

``peak`` is the honest "did it snap?" number: a speaker switch that used to
teleport in one step now pans over several steps, so it lowers the peak while
it may raise the jump *count*.
"""

from __future__ import annotations

import numpy as np

from backend.services.tracking import smoother
from backend.services.tracking.speaker import SpeakerDecision
from backend.services.tracking.tracker import TrackSeries

_JUMP = 0.02  # 2% of the frame width


def _fill_gaps(values: np.ndarray, fallback: float) -> np.ndarray:
    if values.size == 0:
        return values
    out = values.copy()
    mask = np.isnan(out)
    if mask.all():
        return np.full_like(out, fallback)
    # Forward/backward hold: keep the last known position instead of resetting.
    last = None
    for i in range(out.size):
        if np.isnan(out[i]):
            out[i] = last if last is not None else np.nan
        else:
            last = out[i]
    first_valid = int(np.flatnonzero(~np.isnan(out))[0])
    out[:first_valid] = out[first_valid]
    return np.nan_to_num(out, nan=fallback)


def _jitter(values: np.ndarray) -> tuple[float, float, int]:
    if values.size < 2:
        return 0.0, 0.0, 0
    deltas = np.abs(np.diff(values))
    return float(deltas.mean()), float(deltas.max()), int((deltas > _JUMP).sum())


def build(
    series: list[TrackSeries],
    times: np.ndarray,
    decision: SpeakerDecision,
    params: dict,
    *,
    src_w: int = 1920,
    src_h: int = 1080,
) -> dict:
    """Dense crop path + metrics for the clip window."""
    steps = times.size
    if steps == 0:
        return {"points": [], "metrics": {}, "faces": 0}

    raw_cx = np.full(steps, np.nan)
    raw_cy = np.full(steps, np.nan)
    raw_w = np.full(steps, np.nan)
    raw_h = np.full(steps, np.nan)
    for step in range(steps):
        index = int(decision.target[step])
        if index < 0 or index >= len(series):
            continue
        item = series[index]
        raw_cx[step] = item.cx[step]
        raw_cy[step] = item.cy[step]
        raw_w[step] = item.w[step]
        raw_h[step] = item.h[step]

    filled_cx = _fill_gaps(raw_cx, 0.5)
    filled_cy = _fill_gaps(raw_cy, 0.42)
    filled_w = _fill_gaps(raw_w, 0.20)
    filled_h = _fill_gaps(raw_h, 0.28)

    smooth_cx = smoother.smooth_axis(filled_cx, times, params)
    smooth_cy = smoother.smooth_axis(filled_cy, times, params)
    smooth_w = smoother.smooth_axis(filled_w, times, params)
    smooth_h = smoother.smooth_axis(filled_h, times, params)

    # Keep the crop inside the frame with a small safety margin.
    smooth_cx = np.clip(smooth_cx, 0.05, 0.95)
    smooth_cy = np.clip(smooth_cy, 0.08, 0.92)

    points = [
        {
            "t": round(float(times[i]), 3),
            "x": round(float(smooth_cx[i]), 5),
            "y": round(float(smooth_cy[i]), 5),
            "w": round(float(smooth_w[i]), 5),
            "h": round(float(smooth_h[i]), 5),
        }
        for i in range(steps)
    ]

    jitter_raw, peak_raw, jumps_raw = _jitter(filled_cx)
    jitter_smooth, peak_smooth, jumps_smooth = _jitter(smooth_cx)
    return {
        "points": points,
        "faces": len(series),
        "metrics": {
            "jitter_raw_pct": round(jitter_raw * 100, 3),
            "jitter_smoothed_pct": round(jitter_smooth * 100, 3),
            "jitter_raw_px": round(jitter_raw * src_w, 1),
            "jitter_smoothed_px": round(jitter_smooth * src_w, 1),
            "jitter_cut_pct": round(
                100.0 * (1.0 - jitter_smooth / jitter_raw), 1
            )
            if jitter_raw > 1e-6
            else 0.0,
            "peak_raw_pct": round(peak_raw * 100, 2),
            "peak_smoothed_pct": round(peak_smooth * 100, 2),
            "jumps_raw": jumps_raw,
            "jumps_smoothed": jumps_smooth,
            "hold_steps": int(np.isnan(raw_cx).sum()),
            "steps": int(steps),
            "speaker_switches": int(decision.switches),
            "talking_ratio": float(decision.talking_ratio),
            "src_size": [int(src_w), int(src_h)],
        },
        # Kept so the shot planner can rebuild a path per shot without
        # re-running detection (Tahap 7). Values are on the same step grid as
        # `points`, so indices line up with `activity` below.
        "raw": {
            "cx": [round(float(value), 5) for value in filled_cx],
            "cy": [round(float(value), 5) for value in filled_cy],
            "h": [round(float(value), 5) for value in filled_h],
        },
        "activity": {
            "target": [int(value) for value in decision.target],
            "talking": [1 if value else 0 for value in (decision.talking if decision.talking is not None else np.zeros(steps, dtype=bool))],
            "talk": [
                [1 if value else 0 for value in row]
                for row in (decision.talk if decision.talk is not None else np.zeros((0, steps), dtype=bool))
            ],
            "overlap": [int(value) for value in (decision.overlap if decision.overlap is not None else np.zeros(steps, dtype=int))],
            "jaw": [round(float(value), 4) for value in (decision.jaw if decision.jaw is not None else np.zeros(steps, dtype=float))],
        },
    }
