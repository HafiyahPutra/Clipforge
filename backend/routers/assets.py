"""Asset endpoints for Phase 4: BGM tracks and watermark logos.

Kept as small REST calls (not SSE) — the files are uploaded once and then
referenced by name from `project.render_preset`.
"""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from backend.services import asset_service

router = APIRouter(prefix="/api", tags=["Assets"])


@router.get("/assets")
def list_assets() -> dict:
    return asset_service.list_assets()


@router.post("/upload-bgm", status_code=201)
async def upload_bgm(file: UploadFile = File(...)) -> dict:
    return await _store("bgm", file)


@router.post("/upload-watermark", status_code=201)
async def upload_watermark(file: UploadFile = File(...)) -> dict:
    return await _store("watermarks", file)


@router.get("/assets/{kind}/{name}")
def download_asset(kind: str, name: str) -> FileResponse:
    try:
        path = asset_service.resolve(kind, name)
    except asset_service.AssetInvalid as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(path, filename=path.name)


@router.delete("/assets/{kind}/{name}", status_code=204)
def delete_asset(kind: str, name: str) -> None:
    try:
        asset_service.delete(kind, name)
    except asset_service.AssetInvalid as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _store(kind: str, file: UploadFile) -> dict:
    data = await file.read()
    try:
        return asset_service.save(kind, file.filename or "", data)
    except asset_service.AssetInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
