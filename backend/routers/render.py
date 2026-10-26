"""Render + export endpoints (Phase 3)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse, StreamingResponse

from backend import config
from backend.schemas.render import (
    HardwareAccelResponse,
    RenderBatchRequest,
    RenderBatchResponse,
    RenderBatchState,
)
from backend.services import project_store, render_service, video_engine
from backend.utils.sse import SSE_HEADERS, sse, sse_comment

logger = logging.getLogger("clipforge.render")

router = APIRouter(prefix="/api", tags=["Render"])


@router.get("/hardware-accel", response_model=HardwareAccelResponse)
def hardware_accel() -> HardwareAccelResponse:
    """What this machine can encode with (videotoolbox vs libx264, libass or not)."""
    return HardwareAccelResponse(**video_engine.hardware_info())


@router.post(
    "/render-batch",
    response_model=RenderBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_batch(payload: RenderBatchRequest) -> RenderBatchResponse:
    try:
        state = render_service.start(payload.project_id, payload.clip_indices, payload.settings)
    except project_store.ProjectNotFound:
        raise HTTPException(status_code=404, detail="Project not found.") from None
    except render_service.RenderInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except video_engine.RenderError as exc:
        logger.error("Render batch error: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except Exception as exc:
        # Never answer with a bare "internal error": the caller needs the real
        # cause to act on it (this is how the FfprobeError typo stayed hidden).
        logger.exception("Unexpected render error")
        raise HTTPException(
            status_code=500,
            detail=f"Render failed: {type(exc).__name__}: {exc}",
        ) from exc

    return RenderBatchResponse(
        batch_id=state.batch_id,
        project_id=state.project_id,
        clip_count=len(state.clips),
        progress_url=f"/api/render-progress/{state.batch_id}",
    )


@router.get("/render-batch/{batch_id}", response_model=RenderBatchState)
def get_batch(batch_id: str) -> RenderBatchState:
    try:
        return render_service.get(batch_id)
    except render_service.RenderInvalid as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.post("/render-batch/{batch_id}/retry", response_model=RenderBatchState)
def retry_batch(batch_id: str, clip_indices: list[int] | None = None) -> RenderBatchState:
    try:
        return render_service.retry(batch_id, clip_indices)
    except render_service.RenderInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/render-batch/{batch_id}/cancel", response_model=RenderBatchState)
def cancel_batch(batch_id: str) -> RenderBatchState:
    try:
        return render_service.cancel(batch_id)
    except render_service.RenderInvalid as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


@router.get("/render-progress/{batch_id}")
async def render_progress(batch_id: str) -> StreamingResponse:
    """SSE: snapshot first, then one `clip`/`batch` event per state change."""

    async def stream():
        try:
            subscriber, snapshot = render_service.subscribe(batch_id)
        except render_service.RenderInvalid:
            yield sse({"message": "Unknown render batch."}, "error")
            return

        try:
            yield sse(snapshot, "snapshot")
            while True:
                try:
                    event, payload = await asyncio.wait_for(subscriber.queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield sse_comment()
                    continue
                yield sse(payload, event)
                if event in {"complete", "error"}:
                    break
        except asyncio.CancelledError:  # client disconnected
            raise
        finally:
            render_service.unsubscribe(batch_id, subscriber)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/download-rendered/{file_name}")
def download_rendered(
    file_name: str,
    project: str = Query(..., description="Project id that owns the file"),
) -> FileResponse:
    try:
        path = render_service.safe_export_path(project, file_name)
    except render_service.RenderInvalid as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return FileResponse(path, media_type="video/mp4", filename=file_name)


@router.get("/download-batch-zip/{batch_id}")
def download_zip(batch_id: str) -> FileResponse:
    try:
        archive = render_service.zip_path(batch_id)
    except render_service.RenderInvalid as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return FileResponse(archive, media_type="application/zip", filename=archive.name)


@router.get("/exports")
def list_exports() -> dict[str, object]:
    """Everything currently on disk under `exports/` (newest project first)."""
    root: Path = config.EXPORTS_DIR
    projects: list[dict[str, object]] = []
    for folder in sorted(root.iterdir()) if root.exists() else []:
        if not folder.is_dir():
            continue
        files = [
            {
                "file": item.name,
                "size_bytes": item.stat().st_size,
                "url": f"/api/download-rendered/{item.name}?project={folder.name}",
            }
            for item in sorted(folder.glob("*.mp4"))
        ]
        projects.append({"project_id": folder.name, "files": files, "count": len(files)})
    return {"projects": projects}
