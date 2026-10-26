"""Tahap 3 — give every face an identity and fill the gaps between detections.

The detector runs on every ``detect_interval``-th analysed frame; this module
turns that sparse list into one dense series per face id:

    detections  →  greedy IoU/centroid association  →  linear interpolation

Identity matters for two reasons: the crop no longer jumps between two speakers
when their boxes swap size, and "who is talking" can be measured over time
instead of being re-guessed from scratch on every frame.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from backend.services.tracking.detector import FaceHit

logger = logging.getLogger("clipforge.tracking")


@dataclass
class Track:
    id: int
    times: list[float] = field(default_factory=list)
    hits: list[FaceHit] = field(default_factory=list)
    last_step: int = 0

    @property
    def last(self) -> FaceHit:
        return self.hits[-1]


def _iou(a: FaceHit, b: FaceHit) -> float:
    ax0, ax1 = a.cx - a.w / 2, a.cx + a.w / 2
    ay0, ay1 = a.cy - a.h / 2, a.cy + a.h / 2
    bx0, bx1 = b.cx - b.w / 2, b.cx + b.w / 2
    by0, by1 = b.cy - b.h / 2, b.cy + b.h / 2
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = ix * iy
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 1e-9 else 0.0


def _centroid_distance(a: FaceHit, b: FaceHit) -> float:
    return float(np.hypot(a.cx - b.cx, a.cy - b.cy))


def build_tracks(
    detections: list[tuple[float, list[FaceHit]]],
    *,
    iou_threshold: float,
    max_age: int,
) -> list[Track]:
    """Associate detections across time into stable ids."""
    tracks: list[Track] = []
    next_id = 1
    for step, (time, hits) in enumerate(detections):
        candidates: list[tuple[float, int, int]] = []
        for track in tracks:
            if step - track.last_step > max_age:
                continue
            for hit_index, hit in enumerate(hits):
                score = _iou(track.last, hit)
                if score < iou_threshold:
                    # Accept a nearby centroid as a weak match (fast head motion).
                    distance = _centroid_distance(track.last, hit)
                    if distance > max(0.10, track.last.w * 1.6):
                        continue
                    score = max(score, 0.5 * (1.0 - distance))
                candidates.append((score, track.id, hit_index))

        candidates.sort(reverse=True)
        used_tracks: set[int] = set()
        used_hits: set[int] = set()
        by_id = {track.id: track for track in tracks}
        for score, track_id, hit_index in candidates:
            if track_id in used_tracks or hit_index in used_hits:
                continue
            track = by_id[track_id]
            track.times.append(time)
            track.hits.append(hits[hit_index])
            track.last_step = step
            used_tracks.add(track_id)
            used_hits.add(hit_index)

        for hit_index, hit in enumerate(hits):
            if hit_index in used_hits:
                continue
            tracks.append(
                Track(id=next_id, times=[time], hits=[hit], last_step=step)
            )
            next_id += 1

    return tracks


@dataclass
class TrackSeries:
    """One face id sampled on the output time grid."""

    id: int
    cx: np.ndarray
    cy: np.ndarray
    w: np.ndarray
    h: np.ndarray
    jaw: np.ndarray
    valid: np.ndarray
    detections: int

    @property
    def weight(self) -> float:
        if not self.valid.any():
            return 0.0
        return float(np.nanmedian(self.w[self.valid]) * np.nanmedian(self.h[self.valid]))

    def coverage(self) -> float:
        return float(self.valid.mean()) if self.valid.size else 0.0


def dense_series(
    tracks: list[Track],
    times: np.ndarray,
    *,
    hold_seconds: float,
) -> list[TrackSeries]:
    """Interpolate each track onto ``times``; hold the last value for a while."""
    series: list[TrackSeries] = []
    for track in tracks:
        if not track.times:
            continue
        t_src = np.asarray(track.times, dtype=float)
        order = np.argsort(t_src)
        t_src = t_src[order]
        hits = [track.hits[i] for i in order]
        cx = np.asarray([hit.cx for hit in hits], dtype=float)
        cy = np.asarray([hit.cy for hit in hits], dtype=float)
        w = np.asarray([hit.w for hit in hits], dtype=float)
        h = np.asarray([hit.h for hit in hits], dtype=float)
        jaw = np.asarray([hit.jaw if hit.jaw is not None else np.nan for hit in hits], dtype=float)

        # np.interp cannot hold a value past the last sample, so we extend the
        # source by `hold_seconds` with the final measurement repeated.
        t_ext = np.append(t_src, t_src[-1] + max(0.0, hold_seconds))
        cx_ext = np.append(cx, cx[-1])
        cy_ext = np.append(cy, cy[-1])
        w_ext = np.append(w, w[-1])
        h_ext = np.append(h, h[-1])
        jaw_ext = np.append(jaw, jaw[-1])

        valid = (times >= t_src[0]) & (times <= t_src[-1] + max(0.0, hold_seconds))
        out = TrackSeries(
            id=track.id,
            cx=np.interp(times, t_ext, cx_ext),
            cy=np.interp(times, t_ext, cy_ext),
            w=np.interp(times, t_ext, w_ext),
            h=np.interp(times, t_ext, h_ext),
            jaw=np.interp(times, t_ext, np.nan_to_num(jaw_ext, nan=0.0)),
            valid=valid,
            detections=len(track.times),
        )
        # A face we barely saw is noise, not a subject.
        if out.coverage() < 0.08:
            continue
        series.append(out)
    return series
