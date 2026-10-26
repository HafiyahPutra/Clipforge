"""Tahap 3 — face detection + landmarks (MediaPipe, with YuNet as fallback).

MediaPipe Tasks gives us two things the old YuNet-only path could not:
  * several faces per frame (needed to follow the active speaker), and
  * 478 landmarks + blendshapes, so ``jawOpen`` becomes a reliable talking cue
    instead of guessing from a bounding box that also contains the chin.

Both models ship in ``backend/cascades`` (``blaze_face_short_range.tflite`` and
``face_landmarker.task``) — no download at runtime, no cloud, CPU only.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np

logger = logging.getLogger("clipforge.tracking")

_FACE_DETECTOR = "blaze_face_short_range.tflite"
_FACE_LANDMARKER = "face_landmarker.task"
_YUNET = "face_detection_yunet_2023mar.onnx"

# Inner-lip landmarks (MediaPipe face mesh indices) used for the debug overlay.
_LIP_POINTS = (13, 14, 61, 291, 78, 308, 0, 17)
_MOUTH_TOP, _MOUTH_BOTTOM = 13, 14


@dataclass
class FaceHit:
    """One detected face, normalised to the analysis frame."""

    cx: float
    cy: float
    w: float
    h: float
    jaw: float | None = None          # 0 = mouth closed, 1 = wide open
    score: float = 0.0
    points: list[tuple[float, float]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cx": round(self.cx, 5),
            "cy": round(self.cy, 5),
            "w": round(self.w, 5),
            "h": round(self.h, 5),
            "jaw": None if self.jaw is None else round(self.jaw, 4),
            "score": round(self.score, 3),
        }


class MediaPipeDetector:
    """BlazeFace (boxes) + FaceLandmarker (mesh, blendshapes)."""

    kind = "mediapipe"

    def __init__(self, models_dir: Path) -> None:
        os.environ.setdefault("GLOG_minloglevel", "2")
        os.environ.setdefault("MEDIAPIPE_DISABLE_GPU", "1")

        import mediapipe as mp
        from mediapipe.tasks.python import vision
        from mediapipe.tasks.python.core.base_options import BaseOptions

        self._mp = mp
        self._vision = vision
        detector_path = models_dir / _FACE_DETECTOR
        landmarker_path = models_dir / _FACE_LANDMARKER
        if not detector_path.is_file() or not landmarker_path.is_file():
            raise FileNotFoundError("mediapipe models missing from backend/cascades")

        self._detector = vision.FaceDetector.create_from_options(
            vision.FaceDetectorOptions(
                base_options=BaseOptions(model_asset_path=str(detector_path)),
                running_mode=vision.RunningMode.IMAGE,
                min_detection_confidence=0.4,
            )
        )
        self._landmarker = vision.FaceLandmarker.create_from_options(
            vision.FaceLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(landmarker_path)),
                running_mode=vision.RunningMode.IMAGE,
                num_faces=4,
                min_face_detection_confidence=0.4,
                min_face_presence_confidence=0.4,
                min_tracking_confidence=0.4,
                output_face_blendshapes=True,
            )
        )

    # -- internals ---------------------------------------------------------

    def _image(self, rgb: np.ndarray) -> Any:
        return self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)

    @staticmethod
    def _bbox_of(landmarks: list[Any], width: int, height: int) -> tuple[float, float, float, float]:
        xs = [float(point.x) for point in landmarks]
        ys = [float(point.y) for point in landmarks]
        x0, x1 = max(0.0, min(xs)), min(1.0, max(xs))
        y0, y1 = max(0.0, min(ys)), min(1.0, max(ys))
        return (x0 + x1) / 2.0, (y0 + y1) / 2.0, max(0.01, x1 - x0), max(0.01, y1 - y0)

    @staticmethod
    def _jaw_from_landmarks(landmarks: list[Any], height: float) -> float:
        if len(landmarks) <= _MOUTH_BOTTOM or height <= 1e-6:
            return 0.0
        top = landmarks[_MOUTH_TOP]
        bottom = landmarks[_MOUTH_BOTTOM]
        gap = abs(float(bottom.y) - float(top.y))
        return float(min(1.0, max(0.0, gap / (height * 0.35))))

    def detect(self, bgr: np.ndarray) -> list[FaceHit]:
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        image = self._image(rgb)
        height, width = bgr.shape[:2]

        boxes = []
        result = self._detector.detect(image)
        for detection in result.detections or []:
            box = detection.bounding_box
            score = float(detection.categories[0].score) if detection.categories else 0.0
            boxes.append(
                (
                    float(box.origin_x) / width,
                    float(box.origin_y) / height,
                    float(box.width) / width,
                    float(box.height) / height,
                    score,
                )
            )

        hits: list[FaceHit] = []
        mesh = self._landmarker.detect(image)
        blendshapes = mesh.face_blendshapes or []
        for index, landmarks in enumerate(mesh.face_landmarks or []):
            cx, cy, w, h = self._bbox_of(landmarks, width, height)
            jaw = None
            if index < len(blendshapes) and blendshapes[index]:
                for item in blendshapes[index]:
                    if item.category_name == "jawOpen":
                        jaw = float(item.score)
                        break
            if jaw is None:
                jaw = self._jaw_from_landmarks(landmarks, h)
            points = [
                (float(landmarks[i].x), float(landmarks[i].y))
                for i in _LIP_POINTS
                if i < len(landmarks)
            ]
            hits.append(FaceHit(cx=cx, cy=cy, w=w, h=h, jaw=jaw, score=1.0, points=points))

        if hits:
            return hits

        # Landmarker found nothing (tiny/blurred face) → keep the plain boxes.
        for x, y, w, h, score in boxes:
            hits.append(FaceHit(cx=x + w / 2.0, cy=y + h / 2.0, w=w, h=h, score=score))
        return hits

    def close(self) -> None:
        for item in (self._detector, self._landmarker):
            try:
                item.close()
            except Exception:  # noqa: BLE001 - best effort shutdown
                pass


class YuNetDetector:
    """Fallback when mediapipe is not installed: boxes only, largest face kept."""

    kind = "yunet"

    def __init__(self, model: Path) -> None:
        self._net = cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.6, 0.3, 5000)

    def detect(self, bgr: np.ndarray) -> list[FaceHit]:
        height, width = bgr.shape[:2]
        if not width or not height:
            return []
        self._net.setInputSize((width, height))
        _, faces = self._net.detect(bgr)
        hits: list[FaceHit] = []
        for row in faces if faces is not None else []:
            x, y, w, h = (float(value) for value in row[:4])
            score = float(row[-1]) if len(row) > 4 else 0.0
            hits.append(
                FaceHit(
                    cx=(x + w / 2.0) / width,
                    cy=(y + h / 2.0) / height,
                    w=w / width,
                    h=h / height,
                    score=score,
                )
            )
        return hits

    def close(self) -> None:
        return None


_detector: Any = None
_detector_lock = threading.Lock()


def load_detector(models_dir: Path | None = None) -> Any:
    """Build (once) the best detector available on this machine."""
    global _detector
    with _detector_lock:
        if _detector is not None:
            return _detector
        if models_dir is None:
            from backend import config

            models_dir = config.CASCADES_DIR
        try:
            _detector = MediaPipeDetector(models_dir)
            logger.info("tracking: MediaPipe FaceDetector + FaceLandmarker")
            return _detector
        except Exception as exc:  # noqa: BLE001 - any import/runtime issue → fallback
            logger.warning("tracking: mediapipe unavailable (%s)", exc)
        model = models_dir / _YUNET
        if model.is_file():
            try:
                _detector = YuNetDetector(model)
                logger.info("tracking: YuNet fallback (%s)", model.name)
                return _detector
            except (cv2.error, OSError) as exc:
                logger.warning("tracking: cannot load %s (%s)", model.name, exc)
        logger.warning("tracking: no face detector available")
        _detector = None
        return _detector


def available() -> bool:
    return load_detector() is not None


def detector_kind() -> str:
    detector = load_detector()
    return getattr(detector, "kind", "none") if detector else "none"
