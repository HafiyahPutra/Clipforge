"""Tahap 7a — turn speaker activity into a *draft* shot list (hard cuts only).

Input is the per-step activity produced by Tahap 4: for every analysis step, who
the tracker currently follows and whether each face is talking. Output is a shot
list where every boundary is a hard cut placed shortly *before* the new speaker
starts, not after the tracker has committed.

Three rules keep the cuts watchable, because a hard cut has no transition to
hide behind:

1. **Look-ahead.** The cut lands ``cut_lookahead`` seconds before the new voice
   starts, so the new face is already on screen when the sentence begins.
   Deriving the cut from the tracker's switch time instead would put it
   ``speaker_min_switch`` late (1.5 s of speech shown on the old face).
2. **Minimum shot length.** A cut closer than ``shot_min_seconds`` to the
   previous one is pushed out rather than emitted.
3. **Rate limit.** If the draft still cuts more than ``cut_max_per_minute``
   times per minute, the shortest shots are merged until it fits. Alternating
   laughter must not become a strobe.

Whole-frame shots are not created here yet (Tahap 2 adds overlap detection);
this module only produces ``follow_speaker`` shots.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from backend.services.shots.model import Shot, summary


def _talk_runs(mask: np.ndarray, times: np.ndarray) -> list[tuple[float, float]]:
    """Contiguous ``True`` runs of a per-step boolean mask as (start, end) times."""
    runs: list[tuple[float, float]] = []
    if mask.size == 0:
        return runs
    start: float | None = None
    for step in range(mask.size):
        if mask[step] and start is None:
            start = float(times[step])
        elif not mask[step] and start is not None:
            runs.append((start, float(times[step])))
            start = None
    if start is not None:
        runs.append((start, float(times[-1])))
    return runs


def _onset_before(runs: list[tuple[float, float]], moment: float) -> float | None:
    """Start of the talk run that contains (or immediately precedes) ``moment``."""
    best: float | None = None
    for run_start, run_end in runs:
        if run_start <= moment + 0.35:
            if run_end >= moment - 0.05:
                best = run_start
        elif best is None:
            break
    return best


def _runs(
    mask: np.ndarray,
    times: np.ndarray,
    min_length: float,
    max_gap: float,
) -> list[tuple[float, float]]:
    """Contiguous ``True`` runs of `mask` as ``(start, end)`` seconds.

    Gaps up to `max_gap` are bridged (a breath inside an overlap) and runs
    shorter than `min_length` are dropped.
    """
    if mask.size == 0 or not mask.any():
        return []
    edges = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    spans: list[list[float]] = []
    for lo, hi in zip(starts, ends):
        begin, finish = float(times[lo]), float(times[min(hi, times.size - 1)])
        if spans and begin - spans[-1][1] <= max_gap:
            spans[-1][1] = finish
            continue
        spans.append([begin, finish])
    return [(a, b) for a, b in spans if b - a >= min_length]


def _turns(
    times: np.ndarray,
    target: np.ndarray,
    talk: np.ndarray | None,
    min_talk: float,
) -> tuple[list[tuple[float, float, int]], int]:
    """Turns as ``(onset, end, face)`` in time order, plus how many were dropped.

    With the per-face talk matrix (normal payload) a turn is any talk run at
    least ``min_talk`` long — the tracker's own switch is ignored, because it
    lags the voice by ``speaker_min_switch``. With only the followed face's mask
    (older callers, tests) the boundaries come from ``target``, the onset is
    looked back to the start of that face's talk run, and the run itself must
    still last ``min_talk`` or the turn is dropped.
    """
    mask = None if talk is None else np.asarray(talk)
    dropped = 0

    if mask is not None and mask.ndim == 2:
        runs: list[tuple[float, float, int]] = []
        for face in range(mask.shape[0]):
            for run_start, run_end in _talk_runs(mask[face], times):
                if run_end - run_start >= min_talk:
                    runs.append((run_start, run_end, face))
                else:
                    dropped += 1
        runs.sort()
        return runs, dropped

    turns: list[tuple[float, float, int]] = []
    runs_1d = _talk_runs(mask, times) if mask is not None else []
    previous = -1
    for step in range(times.size):
        face = int(target[step])
        if face < 0:
            continue
        if face == previous:
            continue
        moment = float(times[step])
        onset = moment
        length: float | None = None
        for run_start, run_end in runs_1d:
            if run_start - 1e-6 <= moment <= run_end + 1e-6:
                onset, length = run_start, run_end - run_start
                break
            if moment < run_start <= moment + 0.35:
                onset, length = run_start, run_end - run_start
                break
        if length is not None and length < min_talk:
            dropped += 1
            previous = face
            continue
        turns.append((onset, moment, face))
        previous = face
    return turns, dropped


def build_draft(
    times: np.ndarray,
    target: np.ndarray,
    talk: np.ndarray,
    params: dict[str, Any],
    *,
    start: float,
    end: float,
    person_labels: list[str] | None = None,
    speech: np.ndarray | None = None,
    overlap: np.ndarray | None = None,
) -> tuple[list[Shot], dict[str, Any]]:
    """Draft shot list for ``[start, end)`` plus diagnostics.

    ``times`` is clip-relative; ``start``/``end`` are absolute source seconds, so
    the returned shots use absolute time (see :mod:`model`).
    """
    duration = max(0.1, float(end) - float(start))
    min_shot = max(0.5, float(params.get("shot_min_seconds", 2.0)))
    min_talk = max(0.2, float(params.get("cut_min_talk", 0.8)))
    lookahead = max(0.0, float(params.get("cut_lookahead", 0.15)))
    max_rate = float(params.get("cut_max_per_minute", 6.0) or 0.0)

    times = np.asarray(times, dtype=float)
    target = np.asarray(target, dtype=int)
    talk = np.asarray(talk, dtype=bool) if talk is not None else None
    if times.size < 2:
        return (
            [Shot(index=1, start=start, end=end, kind="follow_speaker")],
            {"switches": 0, "cuts": 0, "cuts_per_minute": 0.0, "skipped": 0, "relaxed": 0},
        )

    labels = person_labels or []
    min_overlap = max(0.4, float(params.get("overlap_min_seconds", 1.2)))
    overlap_gap = max(0.0, float(params.get("overlap_max_gap", 0.3)))
    whole_enabled = bool(params.get("whole_frame_enabled", True))
    speech_gate = speech is not None and np.asarray(speech).size == times.size

    # --- candidate boundaries: the onset of a sustained turn -----------------
    # The cut is placed on the *voice onset* of the new speaker, never on the
    # tracker's switch: the tracker waits `speaker_min_switch` (1.5 s) before it
    # commits, and by then a short turn is already half over. When the per-face
    # talk matrix is available (the normal payload) the turns are read straight
    # from it, so a turn the tracker never committed to still gets its cut.
    cuts: list[float] = []          # clip-relative
    turns, dropped_turns = _turns(times, target, talk, min_talk)
    skipped = dropped_turns
    per_face = talk is not None and np.asarray(talk).ndim == 2
    matrix = np.asarray(talk) if per_face else None
    current = -1
    for run_start, run_end, face in turns:
        if face == current:
            continue
        if per_face:
            window = (times >= run_start) & (times <= run_end)
            others = [
                other
                for other in range(matrix.shape[0])
                if other != face
                and window.any()
                and float(np.mean(matrix[other][window])) > 0.5
            ]
            if others:
                # Two people talking at once is not a turn — it becomes a
                # whole_frame shot further down.
                skipped += 1
                continue
        if speech_gate:
            # The mouth can move without a voice (laughter, chewing, a nod).
            # Requiring the voice activity mask inside the turn keeps those out
            # of the shot list; the tolerance is applied by the caller when the
            # mask is sampled onto the activity grid.
            window = (times >= run_start) & (times <= run_end)
            if window.any() and not bool(np.asarray(speech)[window].any()):
                skipped += 1
                continue
        candidate = run_start - lookahead
        if candidate <= 0.0 or candidate >= duration - 0.25:
            current = face           # already on screen when the clip starts
            skipped += 1
            continue
        if cuts and candidate - cuts[-1] < min_shot:
            candidate = cuts[-1] + min_shot
            if candidate >= duration - 0.25:
                skipped += 1
                continue
        cuts.append(round(candidate, 3))
        current = face

    # --- whole_frame spans: two or more people talking at the same time ------
    spans: list[tuple[float, float]] = []
    if whole_enabled:
        if overlap is not None and np.asarray(overlap).size == times.size:
            mask = np.asarray(overlap) >= 2
        elif matrix is not None and matrix.shape[0] >= 2:
            mask = matrix.sum(axis=0) >= 2
        else:
            mask = None
        if mask is not None and mask.any():
            spans = _runs(mask, times, min_overlap, overlap_gap)
            # A cut inside a whole_frame span is pointless: both sides show the
            # same wide shot, so the boundary is dropped rather than merged.
            cuts = [
                value
                for value in cuts
                if not any(lo + 1e-6 < value < hi - 1e-6 for lo, hi in spans)
            ]

    def _kind_at(moment: float) -> str:
        return "whole_frame" if any(lo <= moment <= hi for lo, hi in spans) else "follow_speaker"

    def _assemble(boundaries: list[float], minimum: float) -> list[Shot]:
        edges = [0.0] + [value for value in boundaries if minimum <= value <= duration - minimum]
        edges.append(duration)
        edges = sorted(set(edges))
        shots: list[Shot] = []
        for position, (left, right) in enumerate(zip(edges, edges[1:]), start=1):
            if right - left < 0.2:
                continue
            kind = _kind_at((left + right) / 2.0)
            shots.append(
                Shot(
                    index=position,
                    start=round(start + left, 3),
                    end=round(start + right, 3),
                    kind=kind,
                    source="auto",
                    zoom=1.0 if kind == "whole_frame" else "auto",
                )
            )
        return shots

    boundaries = list(cuts)
    for lo, hi in spans:
        if lo > 0.0:
            boundaries.append(round(lo, 3))
        if hi < duration:
            boundaries.append(round(hi, 3))

    shots = _assemble(boundaries, min_shot)
    relaxed = 0

    # --- rate limit: merge the shortest shot until the cut rate is sane -----
    if max_rate > 0:
        for _ in range(12):
            rate = (len(shots) - 1) / (duration / 60.0) if duration > 0 else 0.0
            if rate <= max_rate or len(shots) <= 2:
                break
            shortest = min(range(1, len(shots)), key=lambda i: shots[i].duration)
            drop = shots[shortest].start - start
            boundaries = [value for value in boundaries if abs(value - drop) > 1e-6]
            shots = _assemble(boundaries, min_shot)
            relaxed += 1

    if len(shots) == 1:
        shots[0].start, shots[0].end = round(start, 3), round(end, 3)
        shots[0].kind = _kind_at(duration / 2.0)
        shots[0].zoom = 1.0 if shots[0].kind == "whole_frame" else "auto"

    stats = summary(shots)
    diagnostics = {
        "switches": int(len(cuts)),
        "cuts": max(0, len(shots) - 1),
        "cuts_per_minute": round((len(shots) - 1) / max(0.001, duration / 60.0), 2),
        "skipped": skipped,
        "relaxed": relaxed,
        "persons": labels,
        "avg_shot_seconds": stats["avg_shot_seconds"],
        "min_shot_seconds": stats["min_shot_seconds"],
        "whole_frame_shots": sum(1 for shot in shots if shot.kind == "whole_frame"),
        "whole_frame_seconds": round(
            sum(shot.duration for shot in shots if shot.kind == "whole_frame"), 2
        ),
        "overlap_spans": [[round(lo, 2), round(hi, 2)] for lo, hi in spans],
        "speech_gate": bool(speech_gate),
    }
    return shots, diagnostics
