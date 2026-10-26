"""Shot list pipeline — data model, draft, zoom, and per-shot path planning.

This package is the backbone of the Phase 7 framing system. It does **not** do
any detection itself; it consumes the activity timeline produced by
``tracking`` and turns it into a list of shots with a crop path per shot.
"""

from backend.services.shots import plan, zoom  # re-export for convenience
from backend.services.shots.model import Shot, normalize, summary

__all__ = ["Shot", "normalize", "summary", "plan", "zoom"]
