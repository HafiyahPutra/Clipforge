"""AI provider passthrough endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from backend import config
from backend.schemas.common import ModelsResponse
from backend.services import ai_service

router = APIRouter(tags=["AI"])


@router.get("/api/models", response_model=ModelsResponse)
def models(
    base_url: str | None = Query(None, description="OpenAI-compatible base URL"),
    api_key: str | None = Query(None, description="Provider API key"),
) -> ModelsResponse:
    """Proxy `GET {base_url}/models` so the browser never needs CORS access."""
    resolved_base = (base_url or config.AI_BASE_URL).rstrip("/")
    try:
        items = ai_service.list_models(resolved_base, api_key)
    except ai_service.AIProviderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return ModelsResponse(models=items, count=len(items), base_url=resolved_base)
