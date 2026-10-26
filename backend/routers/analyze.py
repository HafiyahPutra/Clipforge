"""`POST /api/analyze` and `POST /api/analyze/upload` — both stream SSE.

The pipeline runs in a worker thread; a queue bridges it into the async
generator so the HTTP response stays responsive (keep-alive comments every 15s)
and a client disconnect cancels the run at the next checkpoint.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from backend import config
from backend.schemas.analyze import AnalyzeRequest
from backend.services import analyze_service
from backend.utils.sse import SSE_HEADERS, sse, sse_comment

router = APIRouter(tags=["Analyze"])

_SENTINEL = object()
_TERMINAL_EVENTS = {"complete", "error", "cancelled"}


def _stream(
    payload: AnalyzeRequest,
    *,
    uploaded_path: str | None = None,
    uploaded_name: str | None = None,
) -> StreamingResponse:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    cancel = threading.Event()

    def emit(event: str, data: dict[str, Any]) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (event, data))

    def worker() -> None:
        try:
            analyze_service.run(
                payload,
                emit=emit,
                cancel=cancel,
                uploaded_path=uploaded_path,
                uploaded_name=uploaded_name,
            )
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

    thread = threading.Thread(target=worker, name="clipforge-analyze", daemon=True)
    thread.start()

    async def event_stream() -> AsyncIterator[str]:
        try:
            yield sse({"stage": "queued", "percent": 0, "message": "Job accepted"}, event="progress")
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield sse_comment("working")
                    continue
                if item is _SENTINEL:
                    break
                event, data = item
                yield sse(data, event=event)
                if event in _TERMINAL_EVENTS:
                    break
        finally:
            cancel.set()  # client gone or finished: stop the worker at its next checkpoint

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={**SSE_HEADERS, "X-Content-Type-Options": "nosniff"},
    )


@router.post("/api/analyze")
async def analyze(payload: AnalyzeRequest) -> StreamingResponse:
    """Analyze a YouTube / Google Drive / direct URL. Response is `text/event-stream`."""
    if not (payload.url or "").strip():
        raise HTTPException(status_code=422, detail="A video URL is required.")
    return _stream(payload)


@router.post("/api/analyze/upload")
async def analyze_upload(
    file: UploadFile = File(...),
    duration: str = Form("30s"),
    model: str | None = Form(None),
    base_url: str | None = Form(None),
    api_key: str | None = Form(None),
    custom_prompt: str | None = Form(None),
    target_clip_count: int | None = Form(None),
    range_start: float | None = Form(None),
    range_end: float | None = Form(None),
    language: str | None = Form(None),
) -> StreamingResponse:
    """Upload a local video (single multipart request) and analyze it."""
    limit_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
    temp_path = config.UPLOADS_DIR / f"{uuid.uuid4().hex[:12]}-{_safe_filename(file.filename)}"

    written = 0
    try:
        with temp_path.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > limit_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File is larger than the {config.MAX_UPLOAD_MB} MB limit. "
                        "Raise MAX_UPLOAD_MB in .env to allow bigger uploads.",
                    )
                handle.write(chunk)
    except HTTPException:
        temp_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    if written == 0:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="The uploaded file is empty.")

    payload = AnalyzeRequest(
        duration=duration if duration in {"15s", "30s", "60s", "auto"} else "30s",
        model=model,
        base_url=base_url,
        api_key=api_key,
        custom_prompt=custom_prompt,
        target_clip_count=target_clip_count,
        range_start=range_start,
        range_end=range_end,
        language=language if language in {"id", "en", "auto"} else None,
        source_type="upload",
    )
    return _stream(payload, uploaded_path=str(temp_path), uploaded_name=file.filename)


def _safe_filename(name: str | None) -> str:
    """Strip directories and unsafe characters from an uploaded filename."""
    cleaned = (name or "video.mp4").replace("\\", "/").split("/")[-1]
    safe = "".join(ch for ch in cleaned if ch.isalnum() or ch in {".", "-", "_", " "})
    return safe[:80] or "video.mp4"
