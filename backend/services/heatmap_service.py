"""Audio-energy heatmap (F-21) — the local fallback when YouTube retention data
is unavailable, and the data source for the timeline in the Analyze page.

RMS per window over the extracted 16 kHz mono WAV, normalised to 0–1 and lightly
smoothed so the curve reads like a retention graph instead of a seismograph.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

DEFAULT_POINTS = 100


def audio_energy(wav_path: str | Path, points: int = DEFAULT_POINTS) -> list[dict[str, float]]:
    """Return `points` windows of `{start_time, end_time, value}` in 0–1."""
    path = Path(wav_path)
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        sample_rate = handle.getframerate()
        frame_count = handle.getnframes()
        raw = handle.readframes(frame_count)

    if channels != 1:
        # extract_audio() always writes mono; guard anyway.
        samples = np.frombuffer(raw, dtype=np.int16).reshape(-1, channels).mean(axis=1)
    else:
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)

    samples = samples.astype(np.float32)
    total = samples.size
    duration = total / sample_rate if sample_rate else 0.0
    if total == 0 or points <= 0:
        return []

    window = max(1, total // points)
    buckets: list[float] = []
    for index in range(points):
        start = index * window
        if start >= total:
            break
        chunk = samples[start : start + window]
        if chunk.size == 0:
            break
        buckets.append(float(np.sqrt(np.mean(np.square(chunk))) if chunk.size else 0.0))

    if not buckets:
        return []

    values = np.array(buckets, dtype=np.float32)
    peak = float(values.max()) or 1.0
    values = values / peak

    # 3-point moving average: smooths single-frame spikes without hiding peaks.
    if values.size >= 3:
        kernel = np.ones(3, dtype=np.float32) / 3.0
        values = np.convolve(values, kernel, mode="same")

    values = np.clip(values, 0.0, 1.0) ** 0.75  # lift quiet speech so the curve is readable

    step = duration / len(values) if duration else 0.0
    return [
        {
            "start_time": round(index * step, 3),
            "end_time": round(min(duration, (index + 1) * step), 3),
            "value": round(float(value), 4),
        }
        for index, value in enumerate(values)
    ]


def engagement_for(start: float, end: float, heatmap: list[dict[str, float]]) -> float:
    """Average heatmap value inside `[start, end]` (0–1)."""
    if not heatmap:
        return 0.0
    total = 0.0
    count = 0
    for point in heatmap:
        if point["end_time"] <= start or point["start_time"] >= end:
            continue
        total += float(point["value"])
        count += 1
    return round(total / count, 4) if count else 0.0


def attach_engagement(
    segments: list[dict[str, object]], heatmap: list[dict[str, float]]
) -> list[dict[str, object]]:
    """Copy transcript segments and stamp each with its mean audio energy."""
    enriched: list[dict[str, object]] = []
    for segment in segments:
        enriched.append(
            {
                **segment,
                "engagement": engagement_for(
                    float(segment["start"]), float(segment["end"]), heatmap
                ),
            }
        )
    return enriched
