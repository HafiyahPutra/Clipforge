"""Tahap 1–2 — read the clip window and hand small RGB frames to the detector.

Frames come from ``cv2.VideoCapture`` with real timestamps (``CAP_PROP_POS_MSEC``),
so nothing assumes a constant FPS: the analysis grid is built from the times the
decoder actually reports. ``grab()`` skips the colour conversion for frames we
do not need, which keeps a 10-minute 1080p source affordable.

Some OpenCV wheels (notably the one inside the Docker image) can open a file but
decode zero frames for AV1 sources, because their bundled FFmpeg only exposes the
hardware path. When that happens we fall back to piping raw BGR frames out of the
FFmpeg binary the renderer already uses, which decodes everything.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np

from backend.services import video_engine

logger = logging.getLogger("clipforge.tracking")


@dataclass
class SampledFrame:
    t: float          # seconds relative to the clip start
    index: int        # position in the analysis grid
    image: np.ndarray  # BGR, already downscaled to the analysis width


def _scaled(width: int, height: int, target_width: int) -> tuple[int, int]:
    scale = min(1.0, target_width / float(width or 1))
    out_w = max(2, int(round(width * scale)))
    out_h = max(2, int(round(height * scale)))
    return out_w, out_h


def probe_video(video: Path) -> dict[str, float]:
    """FPS, size and duration straight from the container metadata."""
    capture = cv2.VideoCapture(str(video))
    info = {"fps": 0.0, "width": 0.0, "height": 0.0, "duration": 0.0, "frames": 0.0}
    try:
        if capture.isOpened():
            info["fps"] = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
            info["width"] = float(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0.0)
            info["height"] = float(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0.0)
            info["frames"] = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
            if info["fps"] > 0 and info["frames"] > 0:
                info["duration"] = info["frames"] / info["fps"]
    finally:
        capture.release()

    if info["width"] <= 0 or info["height"] <= 0:
        try:
            probed = video_engine.probe(video)
            info["width"] = float(probed.get("width") or 0)
            info["height"] = float(probed.get("height") or 0)
            info["duration"] = float(probed.get("duration") or info["duration"])
        except Exception:  # noqa: BLE001 - metadata is best effort
            pass
    if info["fps"] <= 0:
        info["fps"] = 30.0
    return info


def _frames_cv2(
    video: Path, start: float, end: float, sample_fps: float, target_width: int
) -> list[SampledFrame]:
    capture = cv2.VideoCapture(str(video))
    if not capture.isOpened():
        return []
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 30.0)
    step = max(1, int(round(fps / max(0.1, sample_fps))))
    frames: list[SampledFrame] = []
    try:
        if start > 0:
            capture.set(cv2.CAP_PROP_POS_MSEC, start * 1000.0)
        raw_index = 0
        while True:
            if not capture.grab():
                break
            position = float(capture.get(cv2.CAP_PROP_POS_MSEC) or 0.0) / 1000.0
            if position <= 0.0:  # some containers never report POS_MSEC
                position = start + raw_index / fps
            if position >= end:
                break
            if raw_index % step == 0:
                ok, frame = capture.retrieve()
                if not ok or frame is None:
                    raw_index += 1
                    continue
                height, width = frame.shape[:2]
                out_w, out_h = _scaled(width, height, target_width)
                if (out_w, out_h) != (width, height):
                    frame = cv2.resize(frame, (out_w, out_h), interpolation=cv2.INTER_AREA)
                frames.append(
                    SampledFrame(
                        t=max(0.0, position - start),
                        index=len(frames),
                        image=frame,
                    )
                )
            raw_index += 1
    finally:
        capture.release()
    return frames


def _frames_ffmpeg(
    video: Path, start: float, end: float, sample_fps: float, target_width: int
) -> list[SampledFrame]:
    info = probe_video(video)
    width, height = int(info["width"]), int(info["height"])
    if width <= 0 or height <= 0:
        return []
    out_w, out_h = _scaled(width, height, target_width)
    out_w -= out_w % 2
    out_h -= out_h % 2
    window = max(0.2, end - start)
    command = [
        video_engine.capabilities()["path"],
        "-v", "error", "-nostdin",
        "-ss", f"{max(0.0, start):.3f}",
        "-t", f"{window:.3f}",
        "-i", str(video),
        "-an",
        "-vf", f"fps={max(0.1, sample_fps)},scale={out_w}:{out_h}",
        "-pix_fmt", "bgr24",
        "-f", "rawvideo",
        "pipe:1",
    ]
    frame_bytes = out_w * out_h * 3
    frames: list[SampledFrame] = []
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        logger.warning("tracking: ffmpeg could not start (%s)", exc)
        return []
    assert process.stdout is not None
    try:
        while True:
            buffer = process.stdout.read(frame_bytes)
            if not buffer or len(buffer) < frame_bytes:
                break
            image = np.frombuffer(buffer, dtype=np.uint8).reshape((out_h, out_w, 3)).copy()
            frames.append(
                SampledFrame(t=len(frames) / max(0.1, sample_fps), index=len(frames), image=image)
            )
    finally:
        process.stdout.close()
        process.wait(timeout=30)
    return frames


def sample_frames(
    video: Path, start: float, end: float, *, sample_fps: float, target_width: int = 640
) -> list[SampledFrame]:
    """Sampled frames for ``[start, end)`` — cv2 first, FFmpeg pipe as fallback."""
    frames = _frames_cv2(video, start, end, sample_fps, target_width)
    if frames:
        return frames
    logger.info("tracking: cv2 decoded nothing, falling back to the ffmpeg pipe")
    return _frames_ffmpeg(video, start, end, sample_fps, target_width)


def iter_thumbnail_frames(
    video: Path, start: float, end: float, count: int = 8, target_width: int = 640
) -> Iterator[SampledFrame]:
    """A handful of frames spread over the window (used by auto-detect)."""
    span = max(0.2, end - start)
    step = span / max(1, count)
    for i in range(count):
        point = start + i * step
        frames = _frames_cv2(video, point, point + step * 0.9, 1.0, target_width)
        if frames:
            yield frames[0]
        else:
            piped = _frames_ffmpeg(video, point, point + step * 0.9, 1.0, target_width)
            if piped:
                yield piped[0]
