"""Shared shapes for the tracking package (kept free of heavy imports)."""

from __future__ import annotations

from typing import Any, TypedDict


class FaceBox(TypedDict, total=False):
    x: float
    y: float
    w: float
    h: float


class TrajectoryPoint(TypedDict):
    t: float
    x: float
    y: float
    w: float
    h: float


class TrackingMetrics(TypedDict, total=False):
    jitter_raw_pct: float
    jitter_smoothed_pct: float
    jitter_raw_px: float
    jitter_smoothed_px: float
    jitter_cut_pct: float
    jumps_raw: int
    jumps_smoothed: int
    hold_steps: int
    steps: int
    speaker_switches: int
    talking_ratio: float
    src_size: list[int]


class FacePayload(TypedDict, total=False):
    start: float
    end: float
    duration: float
    samples: int
    detections: int
    detect_interval: int
    hits: int
    coverage: float
    face: FaceBox | None
    keyframes: list[dict[str, float]]
    trajectory: list[TrajectoryPoint]
    metrics: TrackingMetrics
    faces: int
    available: bool
    detector: str
    clip_type: str
    params: dict[str, Any]
    auto_detect: dict[str, Any]
    elapsed_ms: int
    cached: bool
