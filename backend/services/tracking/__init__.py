"""Face tracking package (OpenCV + MediaPipe).

Import surface:

    from backend.services.tracking import analyze, resolve, catalog, suggest
"""

from backend.services.tracking.config import (
    CLIP_TYPES,
    DEFAULTS,
    STABLE_TYPES,
    catalog,
    defaults,
    resolve,
    suggest,
)
from backend.services.tracking.pipeline import CACHE_VERSION, analyze, auto_detect

__all__ = [
    "CLIP_TYPES",
    "DEFAULTS",
    "STABLE_TYPES",
    "CACHE_VERSION",
    "analyze",
    "auto_detect",
    "catalog",
    "defaults",
    "resolve",
    "suggest",
]
