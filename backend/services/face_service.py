"""Face tracking facade (Phase 6).

The heavy lifting moved to :mod:`backend.services.tracking`, which runs the
OpenCV + MediaPipe pipeline (sampling → detection → identity → active speaker →
smoothing → crop path). This module keeps the old public API — ``detect``,
``available``, ``clear_cache`` — so render, preview and editor code keeps
working, and adds the ``clip_type`` argument that selects a tracking preset.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend import config
from backend.services.tracking import analyze, auto_detect
from backend.services.tracking import config as tracking_config
from backend.services.tracking.detector import available as _available


def available() -> bool:
    """True when a face detector could be loaded."""
    return _available()


def detect(
    video: str | Path,
    start: float,
    end: float,
    *,
    use_cache: bool = True,
    clip_type: str | None = None,
) -> dict[str, Any]:
    """Detect faces in ``[start, end)``; returns the crop trajectory + metrics.

    The payload keeps the Phase 5 keys (``face``, ``keyframes``, ``coverage``,
    ``detector``, ``cached``) and adds ``trajectory`` (dense, per ``expr_hz``
    points), ``metrics`` (jitter before/after), ``faces`` (number of ids) and
    ``clip_type``.
    """
    return analyze(video, start, end, clip_type, use_cache=use_cache)


def presets() -> list[dict[str, Any]]:
    """Clip-type presets (used by ``/api/editor/clip-types``)."""
    return tracking_config.catalog()


def suggest(video: str | Path, start: float, end: float) -> dict[str, Any]:
    """Auto-detect a clip type from face count + frame shape."""
    return auto_detect(Path(video), start, end)


def clear_cache(video: str | Path | None = None) -> int:
    """Drop cached face paths (all, or only those of one source file)."""
    removed = 0
    for item in config.FACES_DIR.glob("*.json"):
        try:
            payload = json.loads(item.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            item.unlink(missing_ok=True)
            continue
        if video is not None and Path(video).name not in json.dumps(payload):
            continue
        item.unlink(missing_ok=True)
        removed += 1
    return removed
