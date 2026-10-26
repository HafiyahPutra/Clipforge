"""Server-Sent Events helpers (used by /api/analyze and render progress later)."""

from __future__ import annotations

import json
from typing import Any

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def sse(data: dict[str, Any], event: str | None = None) -> str:
    """Format a single SSE frame."""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {payload}\n\n"


def sse_comment(text: str = "keep-alive") -> str:
    """SSE comment frame, useful to keep idle connections open."""
    return f": {text}\n\n"
