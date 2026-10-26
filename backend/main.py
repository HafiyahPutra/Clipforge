"""ClipForge FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend import config
from backend.routers import ai, analyze, assets, editor, projects, render, system
from backend.services import system_service

logger = config.logger


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    logger.info("ClipForge %s starting", config.VERSION)
    logger.info("AI provider: %s (model=%s)", config.AI_BASE_URL, config.AI_MODEL or "<unset>")
    logger.info("Whisper: %s / %s / %s", config.WHISPER_MODEL, config.WHISPER_DEVICE, config.WHISPER_COMPUTE_TYPE)
    logger.info("Projects: %s", config.PROJECTS_DIR)
    system_service.start_cleanup_worker()
    yield
    system_service.stop_cleanup_worker()
    logger.info("ClipForge stopped")


app = FastAPI(
    title=config.APP_TITLE,
    version=config.VERSION,
    description="Local-first auto-clipper: long videos in, vertical shorts out.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("X-XSS-Protection", "1; mode=block")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


app.include_router(system.router)
app.include_router(ai.router)
app.include_router(analyze.router)
app.include_router(projects.router)
app.include_router(render.router)
app.include_router(assets.router)
app.include_router(editor.router)


@app.exception_handler(404)
async def api_not_found(request: Request, exc):  # type: ignore[no-untyped-def]
    # A route raised HTTPException(404, "...") — keep its message.
    detail = getattr(exc, "detail", "Not Found")
    if detail and detail != "Not Found":
        return JSONResponse(status_code=404, content={"detail": detail})
    if request.url.path.startswith("/api"):
        return JSONResponse(
            status_code=404,
            content={"detail": f"Unknown API route: {request.url.path}"},
        )
    index = config.DIST_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse(
        status_code=404,
        content={
            "detail": "Frontend build not found. Run `npm run dev` (Vite on :5173) "
            "or `npm run build` to serve the SPA from FastAPI."
        },
    )


# Serve the built SPA (Docker / production) without shadowing the API routes.
if (config.DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=config.DIST_DIR / "assets"), name="assets")


@app.get("/", include_in_schema=False)
def index() -> Response:
    index_file = config.DIST_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return JSONResponse(
        {
            "app": config.APP_NAME,
            "version": config.VERSION,
            "message": "Frontend dev server runs on http://localhost:5173. API docs at /docs.",
        }
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host=config.HOST, port=config.PORT, reload=True)
