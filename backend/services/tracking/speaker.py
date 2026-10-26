"""Tahap 4 — who is actually talking?

``jawOpen`` from the FaceLandmarker blendshapes is averaged over
``speaker_window`` seconds, and a new speaker is only adopted after they have
been above ``speaker_threshold`` for ``speaker_min_switch`` seconds. That
hysteresis is what stops the crop from ping-ponging on every laugh or breath.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from backend.services.tracking.tracker import TrackSeries


@dataclass
class SpeakerDecision:
    target: np.ndarray      # index into `series` per output step, -1 = nobody
    switches: int           # how many times the subject changed
    talking_ratio: float    # share of steps where the chosen face was talking
    # Per-face activity, kept for the shot list (Tahap 7). `talk[i, s]` is True
    # when face i is above its own speaking threshold at step s; `talking` is the
    # same test applied to the followed face; `overlap` counts simultaneous
    # speakers, which is what whole_frame will key off in Tahap 2.
    talk: np.ndarray | None = None
    talking: np.ndarray | None = None
    overlap: np.ndarray | None = None
    # Smoothed jawOpen of the followed face per step. The strict `talk` mask is
    # tuned for "is this a speaker change" and is deliberately sparse; zoom needs
    # a denser "is a mouth moving" signal, so it gates on this instead.
    jaw: np.ndarray | None = None


def _moving_average(values: np.ndarray, window: int) -> np.ndarray:
    if window <= 1 or values.size == 0:
        return values
    window = min(window, values.size)
    kernel = np.ones(window, dtype=float) / window
    padded = np.pad(values, (window // 2, window - 1 - window // 2), mode="edge")
    return np.convolve(padded, kernel, mode="valid")


def choose_target(
    series: list[TrackSeries],
    times: np.ndarray,
    params: dict,
) -> SpeakerDecision:
    steps = times.size
    if not series:
        return SpeakerDecision(target=np.full(steps, -1, dtype=int), switches=0, talking_ratio=0.0)

    fps = max(1.0, float(params.get("expr_hz") or 10.0))
    window = max(1, int(round(float(params.get("speaker_window", 0.6)) * fps)))
    min_switch = float(params.get("speaker_min_switch", 1.5))
    use_speaker = bool(params.get("use_speaker", True)) and any(
        np.nanmax(item.jaw) > 0 for item in series
    )

    jaws = np.vstack([_moving_average(item.jaw, window) for item in series])
    valid = np.vstack([item.valid for item in series])

    # jawOpen is not comparable across people or camera distances, so each face
    # is measured against its own percentile, bounded by the configured range.
    floor = float(params.get("speaker_threshold_floor", 0.05))
    cap = float(params.get("speaker_threshold", 0.18))
    pct = float(params.get("speaker_threshold_percentile", 60))
    limits = []
    for row_index, item in enumerate(series):
        sample = jaws[row_index][valid[row_index]]
        if sample.size:
            limits.append(float(np.clip(np.percentile(sample, pct), floor, max(floor, cap))))
        else:
            limits.append(cap)
    limits_arr = np.asarray(limits, dtype=float)

    # Speaking mask per face (shared by both branches below).
    talk = valid & (jaws >= limits_arr[:, None])

    if not use_speaker:
        # Follow the biggest face for the whole clip (talking head, gaming cam).
        weights = np.asarray([item.weight for item in series], dtype=float)
        order = np.argsort(weights)[::-1]
        target = np.full(steps, -1, dtype=int)
        for index in order:
            mask = (target < 0) & valid[index]
            target[mask] = index
        chosen = target >= 0
        ratio = (
            float(np.mean(jaws[target[chosen], np.flatnonzero(chosen)])) if chosen.any() else 0.0
        )
        talking = np.zeros(steps, dtype=bool)
        jaw_steps = np.zeros(steps, dtype=float)
        for step in range(steps):
            index = int(target[step])
            if index >= 0:
                talking[step] = bool(talk[index, step])
                jaw_steps[step] = float(jaws[index, step])
        return SpeakerDecision(
            target=target,
            switches=0,
            talking_ratio=round(ratio, 3),
            talk=talk,
            talking=talking,
            overlap=talk.sum(axis=0).astype(int),
            jaw=jaw_steps,
        )

    target = np.full(steps, -1, dtype=int)
    current = -1
    candidate = -1
    candidate_since = 0.0
    switches = 0
    talking = 0

    for step in range(steps):
        alive = np.flatnonzero(valid[:, step])
        if alive.size == 0:
            target[step] = current if current >= 0 else -1
            continue
        scores = jaws[alive, step]
        best_local = int(np.argmax(scores))
        best = int(alive[best_local])
        best_score = float(scores[best_local])

        if current < 0 or not valid[current, step]:
            current = best
            candidate = -1
            switches += 1
        elif best != current and best_score >= limits_arr[best]:
            if candidate != best:
                candidate = best
                candidate_since = times[step]
            elif times[step] - candidate_since >= min_switch:
                current = best
                candidate = -1
                switches += 1
        else:
            candidate = -1

        target[step] = current
        if current >= 0 and jaws[current, step] >= limits_arr[current]:
            talking += 1

    talking_steps = np.zeros(steps, dtype=bool)
    jaw_steps = np.zeros(steps, dtype=float)
    for step in range(steps):
        index = int(target[step])
        if index >= 0:
            talking_steps[step] = bool(talk[index, step])
            jaw_steps[step] = float(jaws[index, step])

    return SpeakerDecision(
        target=target,
        switches=max(0, switches - 1),
        talking_ratio=round(talking / max(1, steps), 3),
        talk=talk,
        talking=talking_steps,
        overlap=talk.sum(axis=0).astype(int),
        jaw=jaw_steps,
    )
