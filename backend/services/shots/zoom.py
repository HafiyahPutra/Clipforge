"""Tahap 7b — dynamic zoom *inside* a shot.

The camera pushes in while the followed person is talking and relaxes back to
1.0x when they stop. Everything here is per shot: the caller resets the state at
every boundary, so a zoom never travels across a hard cut (that would look like
a dolly move stitched onto a snap).

Why not simply zoom to a constant factor
----------------------------------------
A fixed tighter crop loses the wide shot for the pauses, which is where the
"listener reaction" reads. Tying zoom to *speech* keeps the wide framing when
nobody talks and makes the push-in feel motivated.

Why not zoom to the measured face size directly
-----------------------------------------------
``jawOpen``-driven speech detection is noisy at 10 Hz; driving the lens off raw
face size would make the frame breathe with detection error. The target is a
*ratio* (``zoom_face_ratio``: how much of the output height the face should
fill), the strength is scaled per clip type, and the resulting curve is rate
limited and Savitzky-Golay smoothed before it ever reaches FFmpeg.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from backend.services.tracking import smoother


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _moving_average(values: np.ndarray, window: int) -> np.ndarray:
    if window <= 1 or values.size == 0:
        return values
    window = min(window, values.size)
    kernel = np.ones(window, dtype=float) / window
    padded = np.pad(values, (window // 2, window - 1 - window // 2), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def target_curve(face_ratio: np.ndarray, params: dict[str, Any]) -> np.ndarray:
    """Zoom factor that would make the face fill ``zoom_face_ratio`` of the frame."""
    zoom_min = float(params.get("zoom_min", 1.0))
    zoom_max = float(params.get("zoom_max", 1.4))
    wanted = float(params.get("zoom_face_ratio", 0.30))
    strength = _clamp(float(params.get("zoom_strength", 0.0) or 0.0), 0.0, 1.0)
    if strength <= 0 or face_ratio.size == 0:
        return np.ones_like(face_ratio)
    safe = np.clip(face_ratio, 1e-3, 1.0)
    raw = np.clip(wanted / safe, zoom_min, zoom_max)
    # Strength scales the *excess* over 1.0x, so 0.6 means "60% of the push-in".
    return np.clip(1.0 + strength * (raw - 1.0), zoom_min, zoom_max)


def talking_mask(jaw: np.ndarray | None, params: dict[str, Any]) -> np.ndarray | None:
    """Dense "mouth is moving" mask derived from the followed face's jawOpen.

    The speaker tracker's own mask is tuned for *speaker changes*: it is sparse on
    purpose (a change needs a clear, sustained winner). Zoom needs the opposite —
    a continuous signal of whether the person is mid-sentence — so it uses a
    relative gate: above ``zoom_talk_ratio`` of that face's own 90th percentile
    jawOpen. Relative, because jawOpen is not comparable across people, cameras
    or distances.
    """
    if jaw is None:
        return None
    values = np.asarray(jaw, dtype=float)
    if values.size == 0:
        return None
    moving = values[values > 0.001]
    if moving.size < 3:
        return None
    ratio = _clamp(float(params.get("zoom_talk_ratio", 0.45) or 0.45), 0.05, 0.95)
    reference = float(np.percentile(moving, 90))
    gate = max(0.02, reference * ratio)
    return values >= gate


def build(
    times: np.ndarray,
    talking: np.ndarray | None,
    face_ratio: np.ndarray,
    params: dict[str, Any],
    *,
    entry: str = "normal",
) -> np.ndarray:
    """Zoom factor per step for one shot (state starts fresh at step 0).

    The push-in target is **one value per shot** (the median face size in that
    shot), not a per-step value. Driving the lens off frame-by-frame face size
    makes it breathe with every syllable and detection wobble — the "zoom
    goyang" failure. One target plus a state machine gives the intended shape:
    hold at 1.0x, ease in once speech is sustained, stay put while talking, ease
    back out after a real pause.
    """
    steps = int(times.size)
    if steps == 0:
        return np.ones(0)
    wanted = target_curve(face_ratio, params)
    if float(np.max(wanted)) <= 1.0001:
        return np.ones(steps)

    delay = max(0.0, float(params.get("zoom_delay", 0.6)))
    release = max(0.0, float(params.get("zoom_release", 0.9)))
    attack = max(0.05, float(params.get("zoom_attack", 0.55)))
    decay = max(0.05, float(params.get("zoom_decay", 0.75)))
    rate_cap = max(0.01, float(params.get("zoom_max_rate", 0.5)))

    if talking is None:
        talking = np.ones(steps, dtype=bool)
    talking = np.asarray(talking, dtype=bool)

    # Smooth the mouth-activity gate before using it: syllable-level gaps are not
    # pauses. An averaged gate with two thresholds (enter/exit) is what stops the
    # lens from pumping in and out between words.
    hz = max(1.0, 1.0 / max(1e-3, float(times[1] - times[0])) if steps > 1 else 10.0)
    gate_window = max(1, int(round(float(params.get("zoom_gate_window", 1.0)) * hz)))
    env = _moving_average(talking.astype(float), gate_window)
    enter = _clamp(float(params.get("zoom_enter", 0.40) or 0.40), 0.05, 0.95)
    exit_at = _clamp(float(params.get("zoom_exit", 0.18) or 0.18), 0.0, enter)

    # One target per shot: the median push-in the face size calls for.
    target = float(np.median(wanted))
    if target <= 1.0001:
        return np.ones(steps)

    out = np.ones(steps, dtype=float)
    active = False
    current = target if (entry == "instant" and bool(talking[0])) else 1.0
    talk_run = 0.0
    quiet_run = 0.0

    for step in range(steps):
        dt = float(times[step] - times[step - 1]) if step else (
            float(times[1] - times[0]) if steps > 1 else 0.1
        )
        dt = max(1e-3, dt)
        if env[step] >= enter:
            talk_run += dt
            quiet_run = 0.0
        elif env[step] <= exit_at:
            quiet_run += dt
            talk_run = 0.0
        # between the thresholds: keep both counters (hysteresis band)

        if not active and talk_run >= delay:
            active = True
        elif active and quiet_run >= release:
            active = False

        goal = target if active else 1.0
        gap = goal - current
        if abs(gap) > 1e-6:
            # Ease-in/ease-out: the permitted step shrinks as we approach the
            # target, so the lens never arrives at full speed and stops dead.
            ramp = attack if gap > 0 else decay
            eased = min(rate_cap, abs(gap) / ramp)
            move = min(abs(gap), eased * dt)
            current += move if gap > 0 else -move
        out[step] = current

    window = int(params.get("zoom_smooth", 7) or 0)
    if window > 2:
        out = smoother.savgol(out, window, 2)
    # SavGol can overshoot the rate limit it was fed, so enforce it once more on
    # the final curve: the spec's "batas kecepatan per detik" has to hold after
    # every stage, not just the one that produced the ramp.
    for step in range(1, steps):
        dt = max(1e-3, float(times[step] - times[step - 1]))
        limit = rate_cap * dt
        delta = out[step] - out[step - 1]
        if abs(delta) > limit:
            out[step] = out[step - 1] + (limit if delta > 0 else -limit)
    return np.clip(out, float(params.get("zoom_min", 1.0)), float(params.get("zoom_max", 1.4)))


def describe(values: np.ndarray) -> dict[str, float]:
    """Summary of one shot's zoom curve, for the API/debug overlay."""
    if values.size == 0:
        return {"zoom_start": 1.0, "zoom_end": 1.0, "zoom_max": 1.0, "zoom_min": 1.0}
    deltas = np.abs(np.diff(values)) if values.size > 1 else np.zeros(1)
    return {
        "zoom_start": round(float(values[0]), 3),
        "zoom_end": round(float(values[-1]), 3),
        "zoom_max": round(float(values.max()), 3),
        "zoom_min": round(float(values.min()), 3),
        "zoom_step_max": round(float(deltas.max()), 4),
    }
