"""Health, version and runtime information."""

from __future__ import annotations

from fastapi import APIRouter

from backend import config
from backend.schemas.common import HealthResponse, VersionResponse
from backend.services import system_service, whisper_service

router = APIRouter(tags=["System"])


@router.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", app=config.APP_NAME, version=config.VERSION)


@router.get("/api/ai/health")
def ai_health(
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> dict[str, object]:
    """Validate the AI configuration end-to-end (key, base URL, model name).

    Used by the Settings screen's "test connection" button and by monitoring.
    It never raises: the payload always says *why* something is wrong, with the
    provider's own message included (API keys are redacted).
    """
    from backend.services import ai_service

    resolved_base = (base_url or config.AI_BASE_URL or "").rstrip("/")
    checks: dict[str, object] = {
        "base_url": resolved_base,
        "configured_model": (model or config.AI_MODEL or "").strip(),
        "has_api_key": bool(api_key or config.AI_API_KEY),
        "timeout_seconds": config.AI_TIMEOUT_SECONDS,
        "max_retries": config.AI_MAX_RETRIES,
    }

    try:
        models = ai_service.list_models(resolved_base, api_key)
        checks["models_ok"] = True
        checks["models_count"] = len(models)
        checks["models"] = [item["id"] for item in models][:25]
    except ai_service.AIProviderError as exc:
        checks["models_ok"] = False
        checks["models_error"] = exc.message
        checks["models_error_detail"] = exc.detail
        checks["status"] = "error"
        return checks
    except Exception as exc:  # noqa: BLE001 - diagnostic endpoint must not raise
        checks["models_ok"] = False
        checks["models_error"] = f"{type(exc).__name__}: {exc}"
        checks["status"] = "error"
        return checks

    chosen = (model or config.AI_MODEL or "").strip()
    if not chosen:
        available = [item["id"] for item in models]
        chosen = next((name for name in ("auto", "free-stack") if name in available), "")
        chosen = chosen or (available[0] if available else "")
        checks["model_auto_selected"] = chosen or None

    if chosen:
        try:
            ok, error = ai_service.check_model(resolved_base, api_key, chosen)
            checks["model_ok"] = ok
            if not ok:
                checks["model_error"] = error
                checks["status"] = "error"
                return checks
        except Exception as exc:  # noqa: BLE001
            checks["model_ok"] = False
            checks["model_error"] = f"{type(exc).__name__}: {exc}"
            checks["status"] = "error"
            return checks
    else:
        checks["model_ok"] = False
        checks["model_error"] = "The provider advertises no usable model."
        checks["status"] = "error"
        return checks

    checks["status"] = "ok"
    return checks


@router.get("/api/system/version", response_model=VersionResponse)
def version() -> VersionResponse:
    return VersionResponse(
        name="ClipForge",
        version=config.VERSION,
        phase=config.PHASE,
        paths={
            "data": str(config.DATA_DIR),
            "media": str(config.MEDIA_DIR),
            "temp": str(config.TEMP_DIR),
            "exports": str(config.EXPORTS_DIR),
        },
    )


@router.get("/api/system/info")
def info() -> dict[str, object]:
    """Safe-to-expose runtime configuration used by the Settings screen."""
    return {
        **config.public_config(),
        "whisper_status": whisper_service.status(),
        "temp": system_service.storage_info(),
    }
