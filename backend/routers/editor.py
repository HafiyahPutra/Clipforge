"""Editor + preview endpoints (Phase 5).

The editor is intentionally thin: the browser holds the working copy of the
clip list and render settings, and this router provides the three things the
browser cannot do alone:

* ``POST /api/editor/trim``      — persist a clip's in/out trim to the project JSON
* ``POST /api/preview``          — render a fast low-res preview of one clip
* ``GET  /api/preview/file/{f}`` — stream that preview back to the <video> tag
* ``GET  /api/preview/faces``    — face path debug info (keyframes + coverage)
"""

from __future__ import annotations

import logging
import re
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from backend import config
from backend.schemas.project import Project, ProjectUpdate
from backend.schemas.render import (
    STREAMER_PRESETS,
    AutoDetectResponse,
    ClipTypeInfo,
    FacePathResponse,
    PeopleActionRequest,
    PeopleHistoryRequest,
    PeopleImpact,
    PeopleResponse,
    PersonInfo,
    PreviewFullRequest,
    PreviewFullResponse,
    PreviewFullStatus,
    PreviewRequest,
    PreviewResponse,
    ShotHistoryRequest,
    ShotInfo,
    ShotListResponse,
    ShotMergeRequest,
    ShotMoveRequest,
    ShotPatchRequest,
    ShotRedetectRequest,
    ShotSaveRequest,
    ShotSplitRequest,
    StreamerPresetInfo,
)
from backend.services import (
    edit_service,
    face_service,
    media_service,
    people,
    people_store,
    preview_service,
    project_store,
    shot_edit,
    shot_store,
)
from backend.services.shots import model as shot_model
from backend.services.tracking import config as tracking_config
from backend.services.tracking import pipeline as tracking_pipeline

logger = logging.getLogger("clipforge.editor")
router = APIRouter(tags=["Editor"])

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")


class TrimRequest(BaseModel):
    clip_index: int = Field(ge=0)
    trim_start: float | None = None
    trim_end: float | None = None

    @field_validator("trim_start", "trim_end")
    @classmethod
    def _non_negative(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return round(max(0.0, float(value)), 3)


@router.get("/api/editor/presets", response_model=list[StreamerPresetInfo])
def list_streamer_presets() -> list[StreamerPresetInfo]:
    return list(STREAMER_PRESETS)


@router.get("/api/editor/clip-types", response_model=list[ClipTypeInfo])
def list_clip_types() -> list[ClipTypeInfo]:
    """Clip-type presets with their resolved tracking parameters."""
    return [ClipTypeInfo(**item) for item in face_service.presets()]


@router.post("/api/editor/clip-types/detect/{project_id}", response_model=AutoDetectResponse)
def detect_clip_type(project_id: str, clip_index: int = 0) -> AutoDetectResponse:
    """Suggest a clip type for one clip (the user can still override it)."""
    try:
        project = project_store.load(project_id)
    except project_store.ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found.") from exc
    if clip_index < 0 or clip_index >= len(project.clips):
        raise HTTPException(status_code=404, detail="Clip index out of range.")
    clip = project.clips[clip_index]
    start, end = edit_service.clip_window(clip)
    source = media_service.ensure_source(project.model_dump())
    try:
        result = face_service.suggest(source, start, end)
    except Exception as exc:  # noqa: BLE001 - suggestion is best effort
        logger.warning("auto-detect failed: %s", exc)
        return AutoDetectResponse(project_id=project.id, clip_index=clip_index)
    return AutoDetectResponse(
        project_id=project.id,
        clip_index=clip_index,
        suggested=str(result.get("suggested") or "talking_head"),
        max_faces=int(result.get("max_faces") or 0),
        samples=int(result.get("samples") or 0),
        aspect=float(result.get("aspect") or 0.0),
        reason=str(result.get("reason") or ""),
    )


@router.post("/api/editor/trim/{project_id}", response_model=Project)
def save_trim(project_id: str, payload: TrimRequest) -> Project:
    try:
        project = project_store.load(project_id)
    except project_store.ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found.") from exc
    except project_store.ProjectInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if payload.clip_index >= len(project.clips):
        raise HTTPException(status_code=404, detail="Clip index out of range.")

    clip = project.clips[payload.clip_index]
    start = payload.trim_start if payload.trim_start is not None else clip.start_time
    end = payload.trim_end if payload.trim_end is not None else clip.end_time
    if end - start < edit_service.MIN_CLIP_SECONDS:
        raise HTTPException(
            status_code=400,
            detail=f"Clip must be at least {edit_service.MIN_CLIP_SECONDS:.1f}s long.",
        )

    clips = list(project.clips)
    clips[payload.clip_index] = clip.model_copy(
        update={
            "trim_start": round(max(0.0, start), 3),
            "trim_end": round(end, 3),
        }
    )
    try:
        return project_store.update(project_id, ProjectUpdate(clips=clips))
    except project_store.ProjectInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/preview", response_model=PreviewResponse)
def create_preview(payload: PreviewRequest) -> PreviewResponse:
    try:
        result = preview_service.render_preview(
            payload.project_id,
            payload.clip_index,
            payload.settings,
        )
    except project_store.ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Project {payload.project_id} not found.") from exc
    except preview_service.PreviewInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("preview failed")
        raise HTTPException(status_code=500, detail=str(exc)[:300]) from exc
    return PreviewResponse(**result)


@router.get("/api/preview/file/{name}")
def get_preview_file(name: str) -> FileResponse:
    if not _NAME_RE.match(name) or not name.endswith(".mp4"):
        raise HTTPException(status_code=400, detail="Invalid preview file name.")
    path = config.PREVIEWS_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Preview not found.")
    return FileResponse(path, media_type="video/mp4", filename=name)


@router.delete("/api/preview/cache")
def clear_previews() -> dict[str, int]:
    removed = preview_service.cleanup(max_age_seconds=0)
    return {"removed": removed}


@router.get("/api/preview/faces", response_model=FacePathResponse)
def face_path(project_id: str, clip_index: int = 0, clip_type: str | None = None) -> FacePathResponse:
    try:
        project = project_store.load(project_id)
    except project_store.ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found.") from exc
    if clip_index < 0 or clip_index >= len(project.clips):
        raise HTTPException(status_code=404, detail="Clip index out of range.")

    clip = project.clips[clip_index]
    start, end = edit_service.clip_window(clip)
    source = media_service.ensure_source(project.model_dump())
    data = face_service.detect(source, start, end, clip_type=clip_type)
    return FacePathResponse(
        project_id=project.id,
        clip_index=clip_index,
        start=start,
        end=end,
        duration=round(max(0.0, end - start), 3),
        samples=int(data.get("samples") or 0),
        hits=int(data.get("hits") or 0),
        coverage=float(data.get("coverage") or 0.0),
        detector=str(data.get("detector") or "none"),
        available=bool(data.get("available")),
        keyframes=[{"t": float(kf["t"]), "x": float(kf["x"])} for kf in data.get("keyframes") or []],
        cached=bool(data.get("cached")),
        trajectory=[
            {
                "t": float(pt["t"]),
                "x": float(pt["x"]),
                "y": float(pt["y"]),
                "w": float(pt["w"]),
                "h": float(pt["h"]),
            }
            for pt in data.get("trajectory") or []
        ],
        metrics=data.get("metrics") or {},
        faces=int(data.get("faces") or 0),
        hit_ratio=float(data.get("hit_ratio") or 0.0),
        clip_type=str(data.get("clip_type") or "auto"),
        params=data.get("params") or {},
    )


# --- Phase 7, stage 3: manual shot-list editor -------------------------------
#
# The shot list is the single source of truth for the render, so it needs an API
# that can read it, replace it, and apply one edit at a time. Every mutation
# goes through `shot_edit` (pure functions) and is persisted through
# `shot_store` before it is returned — the browser never holds the only copy.


def _clip_ctx(project_id: str, clip_index: int):
    """Project + clip + effective window, or an HTTP error."""
    try:
        project = project_store.load(project_id)
    except project_store.ProjectNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found.") from exc
    except project_store.ProjectInvalid as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if clip_index < 0 or clip_index >= len(project.clips):
        raise HTTPException(status_code=404, detail="Clip index out of range.")
    start, end = edit_service.clip_window(project.clips[clip_index])
    return project, project.clips[clip_index], start, end


# Detection is cached on disk, but re-reading that cache and re-deriving the
# draft still costs several seconds per call — and the editor calls this on
# every boundary drag. A tiny in-process memo keeps editing interactive;
# ``fresh=True`` (the Re-detect button) always bypasses it.
_DRAFT_MEMO: dict[tuple, tuple[float, list, dict]] = {}
_DRAFT_MEMO_TTL = 120.0
_DRAFT_MEMO_MAX = 8


def _draft_shots(project, start: float, end: float, clip_type: str | None, *, fresh: bool = False,
                 clip_index: int = 0):
    """Fresh detection → draft shot list (absolute times) + its metrics."""
    source = media_service.ensure_source(project.model_dump())
    # The people revision is part of the key: ignoring a face changes the draft,
    # so a memoised draft from before the correction must not be served.
    revision = people_store.revision(project.id, clip_index)
    key = (str(source), round(start, 3), round(end, 3), clip_type or "", revision)
    if not fresh:
        hit = _DRAFT_MEMO.get(key)
        if hit is not None and (time.monotonic() - hit[0]) < _DRAFT_MEMO_TTL:
            return list(hit[1]), hit[2]
    data = face_service.detect(source, start, end, clip_type=clip_type, use_cache=not fresh)
    data = edit_service.apply_people(data, project.id, clip_index)
    raw = data.get("shots") or []
    if raw:
        shots = shot_model.normalize(raw, start, end)
    else:
        shots = [shot_model.Shot(start=start, end=end, kind="follow_speaker")]
    if not fresh:
        if len(_DRAFT_MEMO) >= _DRAFT_MEMO_MAX:
            _DRAFT_MEMO.pop(min(_DRAFT_MEMO, key=lambda item: _DRAFT_MEMO[item][0]), None)
        _DRAFT_MEMO[key] = (time.monotonic(), list(shots), data)
    return shots, data


def _metrics_for(project, clip_index: int, shots, data) -> tuple[dict, list[str], list[dict]]:
    """Plan metrics for the *current* list, so the UI can show cuts/min and zoom."""
    if not data or not data.get("raw"):
        return {}, [], []
    try:
        plan = edit_service.face_plan(data, width=1080, height=1920, shots=shots)
    except Exception:  # noqa: BLE001 - metrics are best effort
        return {}, [], []
    metrics = (plan or {}).get("metrics") or {}
    warnings = [str(item) for item in metrics.get("warnings") or []]
    plan_shots = (plan or {}).get("shots") or []
    return metrics, warnings, plan_shots


def _id_by_letter(data) -> dict[str, str]:
    """``{"A": "p1", …}`` — pair each letter with the group id on the same row.

    ``person_index`` carries both spellings (``{"A": 0, "p1": 0, "B": 1, "p2": 1}``),
    so the id is whichever key on that row looks like a group id. The pairing stays
    right even after a merge or an ignore renumbers the rows.
    """
    mapping = (data or {}).get("person_index") or {}
    rows: dict[int, dict[str, str]] = {}
    for key, idx in mapping.items():
        text = str(key)
        try:
            row = int(idx)
        except (TypeError, ValueError):
            continue
        slot = rows.setdefault(row, {})
        if len(text) == 1 and text.isalpha():
            slot["letter"] = text.upper()
        elif text[:1] in {"p", "P"} and text[1:].isdigit():
            slot["id"] = text
    return {v["letter"]: v["id"] for v in rows.values() if "letter" in v and "id" in v}


def _response(project, clip_index: int, start: float, end: float, shots, *, origin: str, data,
              updated_at=None, stored=None):
    # Shots written before ``personId`` existed carry only the letter; give them the
    # stable id on the way out so the editor can address a person by id from now on.
    ids = _id_by_letter(data)
    if ids:
        shots = shot_model.backfill_person_ids(shots, ids)
    metrics, warnings, plan_shots = _metrics_for(project, clip_index, shots, data)
    sidecar = stored if stored is not None else (shot_store.load(project.id, clip_index) or {})

    # Plan build results (autoCx, cxMin, cxMax) are matched back to the shots by index.
    out_shots: list[ShotInfo] = []
    lookup = {s.get("index"): s for s in plan_shots if s.get("index")}
    for shot in shots:
        raw = shot.to_dict()
        hint = lookup.get(shot.index)
        if hint:
            for key in ("autoCx", "cxMin", "cxMax"):
                if key in hint:
                    raw[key] = hint[key]
            # `frameCx` comes back clamped to the crop window: the renderer
            # clamps it too, so reporting the stored value would let the slider
            # show a framing the clip never gets. Only override when the plan
            # actually built this shot (hint present) — a shot outside the
            # current trim window has no renderable centre.
            if raw.get("frameCx") is not None and hint.get("frameCx") is not None:
                raw["frameCx"] = hint["frameCx"]
        out_shots.append(ShotInfo(**raw))

    return ShotListResponse(
        project_id=project.id,
        clip_index=clip_index,
        start=round(start, 3),
        end=round(end, 3),
        duration=round(max(0.0, end - start), 3),
        origin=origin,
        updated_at=updated_at,
        shots=out_shots,
        summary=shot_model.summary(shots),
        metrics=metrics,
        warnings=warnings,
        can_undo=bool(sidecar.get("past")),
        can_redo=bool(sidecar.get("future")),
        undo_depth=len(sidecar.get("past") or []),
        redo_depth=len(sidecar.get("future") or []),
    )


def _working(project, clip_index: int, start: float, end: float, clip_type: str | None, *, fresh: bool = False):
    """Stored list if the user has one, otherwise the draft (not persisted yet)."""
    stored = shot_store.load(project.id, clip_index)
    if stored and not fresh:
        shots = shot_model.normalize(stored.get("shots") or [], start, end)
        if shots:
            return shots, "stored", stored.get("updatedAt")
    shots, data = _draft_shots(project, start, end, clip_type, fresh=fresh, clip_index=clip_index)
    return shots, "draft", None


def _persist(project, clip_index: int, shots, start: float, end: float, note: str | None = None,
             previous=None):
    """Persist one edit, recording the list it came from so it can be undone."""
    try:
        return shot_store.save(
            project.id, clip_index, shots, window=(start, end), note=note, previous=previous
        )
    except shot_store.ShotStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/editor/shots/{project_id}", response_model=ShotListResponse)
def get_shots(project_id: str, clip_index: int = 0, clip_type: str | None = None) -> ShotListResponse:
    """Current shot list for one clip: the stored edit, or the draft."""
    project, _clip, start, end = _clip_ctx(project_id, clip_index)
    shots, origin, updated_at = _working(project, clip_index, start, end, clip_type)
    _shots_unused, data = _draft_shots(project, start, end, clip_type, clip_index=clip_index)
    return _response(project, clip_index, start, end, shots, origin=origin, data=data, updated_at=updated_at)


@router.put(
    "/api/editor/shots/{project_id}",
    response_model=ShotListResponse,
    deprecated=True,
)
def put_shots(project_id: str, payload: ShotSaveRequest, clip_type: str | None = None) -> ShotListResponse:
    """Deprecated: replace the whole list. Use the per-edit endpoints instead.

    Kept so an existing client keeps working. It stores whatever boundaries it is
    given as one opaque history step, so undo cannot say what it undid; `/patch`,
    `/split`, `/merge`, `/move` and `/redetect` describe the intent and are what
    the editor calls.
    """
    project, _clip, start, end = _clip_ctx(project_id, payload.clip_index)
    shots = shot_model.normalize([item.model_dump() for item in payload.shots], start, end)
    before, _o, _u = _working(project, payload.clip_index, start, end, clip_type)
    stored = _persist(project, payload.clip_index, shots, start, end, note="replace", previous=before)
    _shots_unused, data = _draft_shots(project, start, end, clip_type, clip_index=payload.clip_index)
    return _response(
        project,
        payload.clip_index,
        start,
        end,
        shots,
        origin="stored",
        data=data,
        updated_at=stored.get("updatedAt"),
        stored=stored,
    )


@router.post("/api/editor/shots/{project_id}/split", response_model=ShotListResponse)
def split_shot(project_id: str, payload: ShotSplitRequest, clip_type: str | None = None) -> ShotListResponse:
    project, _clip, start, end = _clip_ctx(project_id, payload.clip_index)
    shots, _origin, _updated = _working(project, payload.clip_index, start, end, clip_type)
    try:
        edited = shot_edit.split(shots, payload.at, start=start, end=end)
    except shot_edit.ShotEditError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    stored = _persist(project, payload.clip_index, edited, start, end, note="split", previous=shots)
    _shots_unused, data = _draft_shots(project, start, end, clip_type, clip_index=payload.clip_index)
    return _response(
        project, payload.clip_index, start, end, edited,
        origin="stored", data=data, updated_at=stored.get("updatedAt"), stored=stored,
    )


@router.post("/api/editor/shots/{project_id}/merge", response_model=ShotListResponse)
def merge_shots(project_id: str, payload: ShotMergeRequest, clip_type: str | None = None) -> ShotListResponse:
    project, _clip, start, end = _clip_ctx(project_id, payload.clip_index)
    shots, _origin, _updated = _working(project, payload.clip_index, start, end, clip_type)
    before = shot_model.normalize(shots, start, end)
    try:
        edited = shot_edit.merge(shots, payload.index, start=start, end=end)
    except shot_edit.ShotEditError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    notes = shot_edit.merge_warnings(before[payload.index - 1], before[payload.index])
    stored = _persist(project, payload.clip_index, edited, start, end, note="merge", previous=shots)
    _shots_unused, data = _draft_shots(project, start, end, clip_type, clip_index=payload.clip_index)
    result = _response(
        project, payload.clip_index, start, end, edited,
        origin="stored", data=data, updated_at=stored.get("updatedAt"), stored=stored,
    )
    result.warnings = list(result.warnings) + notes
    return result


@router.post("/api/editor/shots/{project_id}/move", response_model=ShotListResponse)
def move_shot_boundary(project_id: str, payload: ShotMoveRequest, clip_type: str | None = None) -> ShotListResponse:
    project, _clip, start, end = _clip_ctx(project_id, payload.clip_index)
    shots, _origin, _updated = _working(project, payload.clip_index, start, end, clip_type)
    try:
        edited = shot_edit.move_boundary(shots, payload.index, payload.at, start=start, end=end)
    except shot_edit.ShotEditError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    stored = _persist(project, payload.clip_index, edited, start, end, note="move", previous=shots)
    _shots_unused, data = _draft_shots(project, start, end, clip_type, clip_index=payload.clip_index)
    return _response(
        project, payload.clip_index, start, end, edited,
        origin="stored", data=data, updated_at=stored.get("updatedAt"), stored=stored,
    )


@router.post("/api/editor/shots/{project_id}/patch", response_model=ShotListResponse)
def patch_shot(project_id: str, payload: ShotPatchRequest, clip_type: str | None = None) -> ShotListResponse:
    """Retarget one shot: kind (e.g. → whole_frame), person, zoom, lock."""
    project, _clip, start, end = _clip_ctx(project_id, payload.clip_index)
    shots, _origin, _updated = _working(project, payload.clip_index, start, end, clip_type)
    patch = {
        key: getattr(payload, key)
        for key in ("kind", "person", "personId", "zoom", "zoomEntry", "locked")
        if getattr(payload, key) is not None
    }
    # frameCx can be set to null to restore auto — include it whenever the client
    # sent the field explicitly, even when the value is null.
    if "frameCx" in payload.model_fields_set:
        patch["frameCx"] = payload.frameCx
    if not patch:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    try:
        edited = shot_edit.update(shots, payload.index, patch, start=start, end=end)
    except shot_edit.ShotEditError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    stored = _persist(project, payload.clip_index, edited, start, end, note="patch", previous=shots)
    _shots_unused, data = _draft_shots(project, start, end, clip_type, clip_index=payload.clip_index)
    return _response(
        project, payload.clip_index, start, end, edited,
        origin="stored", data=data, updated_at=stored.get("updatedAt"), stored=stored,
    )


@router.post("/api/editor/shots/{project_id}/redetect", response_model=ShotListResponse)
def redetect_shots(project_id: str, payload: ShotRedetectRequest, clip_type: str | None = None) -> ShotListResponse:
    """Re-run detection but keep every manual/locked shot exactly where it is."""
    project, _clip, start, end = _clip_ctx(project_id, payload.clip_index)
    stored = shot_store.load(project.id, payload.clip_index)
    current = shot_model.normalize((stored or {}).get("shots") or [], start, end)
    draft, data = _draft_shots(project, start, end, clip_type, fresh=payload.fresh, clip_index=payload.clip_index)
    if not current:
        edited = shot_edit.reset(draft, start=start, end=end)
    else:
        edited = shot_edit.redetect(current, draft, start=start, end=end)
    saved = _persist(project, payload.clip_index, edited, start, end, note="redetect",
                     previous=current or draft)
    return _response(
        project, payload.clip_index, start, end, edited,
        origin="stored", data=data, updated_at=saved.get("updatedAt"), stored=saved,
    )


@router.post("/api/editor/shots/{project_id}/undo", response_model=ShotListResponse)
def undo_shot_edit(project_id: str, payload: ShotHistoryRequest, clip_type: str | None = None) -> ShotListResponse:
    """Step back one edit. History lives in the sidecar, so it survives a reload."""
    project, _clip, start, end = _clip_ctx(project_id, payload.clip_index)
    try:
        stored = shot_store.undo(project.id, payload.clip_index)
    except shot_store.ShotStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if stored is None:
        raise HTTPException(status_code=409, detail="Nothing to undo.")
    shots = shot_model.normalize(stored.get("shots") or [], start, end)
    _shots_unused, data = _draft_shots(project, start, end, clip_type, clip_index=payload.clip_index)
    return _response(
        project, payload.clip_index, start, end, shots,
        origin="stored", data=data, updated_at=stored.get("updatedAt"), stored=stored,
    )


@router.post("/api/editor/shots/{project_id}/redo", response_model=ShotListResponse)
def redo_shot_edit(project_id: str, payload: ShotHistoryRequest, clip_type: str | None = None) -> ShotListResponse:
    """Step forward again after an undo."""
    project, _clip, start, end = _clip_ctx(project_id, payload.clip_index)
    try:
        stored = shot_store.redo(project.id, payload.clip_index)
    except shot_store.ShotStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if stored is None:
        raise HTTPException(status_code=409, detail="Nothing to redo.")
    shots = shot_model.normalize(stored.get("shots") or [], start, end)
    _shots_unused, data = _draft_shots(project, start, end, clip_type, clip_index=payload.clip_index)
    return _response(
        project, payload.clip_index, start, end, shots,
        origin="stored", data=data, updated_at=stored.get("updatedAt"), stored=stored,
    )


@router.delete("/api/editor/shots/{project_id}", response_model=ShotListResponse)
def reset_shots(project_id: str, clip_index: int = 0, clip_type: str | None = None) -> ShotListResponse:
    """Drop the stored list and go back to the pure draft."""
    project, _clip, start, end = _clip_ctx(project_id, clip_index)
    try:
        shot_store.delete(project.id, clip_index)
    except shot_store.ShotStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    shots, data = _draft_shots(project, start, end, clip_type, clip_index=clip_index)
    return _response(project, clip_index, start, end, shots, origin="draft", data=data)


# --- Tahap 8, stage 1: the "People on screen" panel --------------------------
#
# The tracker returns every face it saw, but only the user knows that one of
# them is a poster on the wall and that the guest who left the frame at 00:40
# and came back at 00:52 is the same person. This block turns those judgements
# into a small state document (one group per person, members = tracker ids) and
# re-derives the framing from it — no re-detection, no new dependency.

def _people_ctx(project_id: str, clip_index: int, clip_type: str | None):
    """Project + window + the *uncorrected* analysis + the current people state."""
    project, _clip, start, end = _clip_ctx(project_id, clip_index)
    source = media_service.ensure_source(project.model_dump())
    data = face_service.detect(source, start, end, clip_type=clip_type)
    series = data.get("series") or []
    try:
        state = people_store.load_state(project.id, clip_index)
    except people_store.PeopleStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state = people.ensure_state(state, series)
    return project, start, end, data, state


def _impact(before: dict, after: dict, stored: list[dict] | None, labels: list[str]) -> PeopleImpact:
    """How many draft boundaries moved, and which stored shots point at a ghost."""
    before_starts = [round(float(item.get("start") or 0.0), 3) for item in (before.get("shots") or [])]
    after_starts = [round(float(item.get("start") or 0.0), 3) for item in (after.get("shots") or [])]
    changed = sum(
        1
        for position, value in enumerate(after_starts)
        if position >= len(before_starts) or abs(value - before_starts[position]) > 0.02
    )
    active = {str(label).upper() for label in labels}
    missing = 0
    manual = 0
    for shot in stored or []:
        if shot.get("locked") or str(shot.get("source")) == "manual":
            manual += 1
        person = shot.get("person")
        if shot.get("kind") == "follow_person" and person and str(person).upper() not in active:
            missing += 1
    parts = [f"{len(after_starts)} draft shots"]
    if changed:
        parts.append(f"{changed} boundary change(s)")
    if missing:
        parts.append(f"{missing} stored shot(s) follow a person who is no longer active")
    if manual:
        parts.append(f"{manual} manual shot(s) kept untouched")
    return PeopleImpact(
        draft_shots_before=len(before_starts),
        draft_shots_after=len(after_starts),
        changed_boundaries=changed,
        stored_shots=len(stored or []),
        manual_shots=manual,
        shots_with_missing_person=missing,
        summary=", ".join(parts),
    )


def _people_response(
    project,
    clip_index: int,
    start: float,
    end: float,
    data: dict,
    state: dict,
    *,
    sidecar: dict | None = None,
    impact: PeopleImpact | None = None,
    elapsed_ms: int = 0,
) -> PeopleResponse:
    series = data.get("series") or []
    sidecar = sidecar if sidecar is not None else (people_store.load(project.id, clip_index) or {})
    base = dict(
        project_id=project.id,
        clip_index=clip_index,
        start=round(start, 3),
        end=round(end, 3),
        duration=round(max(0.0, end - start), 3),
        expected=state.get("expected"),
        revision=int(state.get("revision") or 1),
        can_undo=bool(sidecar.get("past")),
        can_redo=bool(sidecar.get("future")),
        undo_depth=len(sidecar.get("past") or []),
        redo_depth=len(sidecar.get("future") or []),
        elapsed_ms=elapsed_ms,
    )
    if not series:
        return PeopleResponse(
            **base, impact=impact, people=[], warnings=[], person_index={}, suggestions=[]
        )

    times = tracking_pipeline.times_from_payload(data)
    params = tracking_config.resolve(data.get("clip_type") or "auto")
    view = people.derive(series, state, times, params)
    rebuilt = tracking_pipeline.rebuild(data, state)
    labels = [str(item.get("letter") or "") for item in view["people"] if not item.get("ignored")]
    stored = shot_store.load(project.id, clip_index) or {}
    return PeopleResponse(
        **base,
        detected=view["detected"],
        active=view["active"],
        people=[PersonInfo(**item) for item in view["people"]],
        warnings=view["warnings"],
        person_index=rebuilt.get("person_index") or {},
        suggestions=people.merge_suggestions(series, state, times),
        impact=impact if impact is not None else _impact(data, rebuilt, stored.get("shots"), labels),
    )


@router.get("/api/editor/people/{project_id}", response_model=PeopleResponse)
def get_people(project_id: str, clip_index: int = 0, clip_type: str | None = None) -> PeopleResponse:
    """Who is on screen, when, and how sure the tracker is — plus clickable problems."""
    started = time.perf_counter()
    project, start, end, data, state = _people_ctx(project_id, clip_index, clip_type)
    return _people_response(
        project, clip_index, start, end, data, state,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


@router.patch("/api/editor/people/{project_id}", response_model=PeopleResponse)
def patch_people(
    project_id: str, payload: PeopleActionRequest, clip_type: str | None = None
) -> PeopleResponse:
    """Apply one correction: rename, ignore, restore, merge, split, expected count.

    The change is persisted in the people sidecar with the same undo stack as the
    shot list, and the response carries an :class:`PeopleImpact` so the UI can
    say "3 shots affected" before the user commits to a re-detect.
    """
    started = time.perf_counter()
    project, start, end, data, state = _people_ctx(project_id, payload.clip_index, clip_type)
    try:
        new_state, note = people.apply_action(
            state,
            payload.action,
            person=payload.person,
            target=payload.target,
            label=payload.label,
            at=payload.at,
            expected=payload.expected,
        )
    except people.PeopleError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        sidecar = people_store.save(
            project.id,
            payload.clip_index,
            new_state,
            window=(start, end),
            note=note,
            previous=state,
        )
    except people_store.PeopleStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _DRAFT_MEMO.clear()  # the draft depends on the people state
    return _people_response(
        project, payload.clip_index, start, end, data, new_state,
        sidecar=sidecar, elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


@router.post("/api/editor/people/{project_id}/undo", response_model=PeopleResponse)
def undo_people(project_id: str, payload: PeopleHistoryRequest, clip_type: str | None = None) -> PeopleResponse:
    project, start, end, data, state = _people_ctx(project_id, payload.clip_index, clip_type)
    try:
        sidecar = people_store.undo(project.id, payload.clip_index)
    except people_store.PeopleStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if sidecar is None:
        raise HTTPException(status_code=409, detail="Nothing to undo.")
    _DRAFT_MEMO.clear()
    return _people_response(
        project, payload.clip_index, start, end, data, sidecar.get("state") or state, sidecar=sidecar
    )


@router.post("/api/editor/people/{project_id}/redo", response_model=PeopleResponse)
def redo_people(project_id: str, payload: PeopleHistoryRequest, clip_type: str | None = None) -> PeopleResponse:
    project, start, end, data, state = _people_ctx(project_id, payload.clip_index, clip_type)
    try:
        sidecar = people_store.redo(project.id, payload.clip_index)
    except people_store.PeopleStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if sidecar is None:
        raise HTTPException(status_code=409, detail="Nothing to redo.")
    _DRAFT_MEMO.clear()
    return _people_response(
        project, payload.clip_index, start, end, data, sidecar.get("state") or state, sidecar=sidecar
    )


@router.delete("/api/editor/people/{project_id}", response_model=PeopleResponse)
def reset_people(project_id: str, clip_index: int = 0, clip_type: str | None = None) -> PeopleResponse:
    """Throw away every correction and go back to "one person per tracker id"."""
    project, start, end, data, state = _people_ctx(project_id, clip_index, clip_type)
    try:
        people_store.delete(project.id, clip_index)
    except people_store.PeopleStoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _DRAFT_MEMO.clear()
    fresh = people.default_state(data.get("series") or [])
    return _people_response(project, clip_index, start, end, data, fresh, sidecar={})


@router.get("/api/editor/people/{project_id}/overlay")
def people_overlay(project_id: str, clip_index: int = 0, clip_type: str | None = None) -> dict:
    """Per-step face boxes for the preview overlay (only fetched when toggled on).

    Downsampled to ~5 Hz: the overlay is a visual aid, not a render input, and a
    full-rate payload would be megabytes for a long clip.
    """
    project, start, end, data, state = _people_ctx(project_id, clip_index, clip_type)
    series = data.get("series") or []
    if not series:
        return {"times": [], "people": []}
    times = tracking_pipeline.times_from_payload(data)
    params = tracking_config.resolve(data.get("clip_type") or "auto")
    effective, labels = people.effective_series(series, state, times)
    view = people.derive(series, state, times, params)
    by_id = {item["id"]: item for item in view["people"]}
    stride = max(1, int(round(0.2 / max(1e-3, float(times[1] - times[0])))) if times.size > 1 else 1)

    out = []
    for position, item in enumerate(effective):
        person = next(
            (entry for entry in view["people"] if str(entry.get("letter")) == str(labels[position])),
            None,
        )
        talk = people._talk_mask(item.jaw, item.valid, params)
        out.append(
            {
                "letter": labels[position],
                "id": (person or {}).get("id"),
                "label": (person or {}).get("label"),
                "color": (person or {}).get("color"),
                "ignored": bool((person or {}).get("ignored")),
                "frames": [
                    [
                        round(float(times[index]), 3),
                        round(float(item.cx[index]), 4),
                        round(float(item.cy[index]), 4),
                        round(float(item.w[index]), 4),
                        round(float(item.h[index]), 4),
                        1 if talk[index] else 0,
                    ]
                    for index in range(0, times.size, stride)
                    if item.valid[index]
                ],
            }
        )
    return {"start": round(start, 3), "end": round(end, 3), "people": out, "by_id": list(by_id)}


@router.get("/api/editor/people/{project_id}/thumb")
def person_thumb(project_id: str, clip_index: int = 0, person_id: str = "", clip_type: str | None = None):
    """A face crop for the People list, cut from the source video on first use."""
    project, start, end, data, state = _people_ctx(project_id, clip_index, clip_type)
    series = data.get("series") or []
    if not series:
        raise HTTPException(status_code=404, detail="No faces in this clip.")
    times = tracking_pipeline.times_from_payload(data)
    params = tracking_config.resolve(data.get("clip_type") or "auto")
    view = people.derive(series, state, times, params)
    person = next((item for item in view["people"] if item["id"] == person_id), None)
    if not person or not person.get("best"):
        raise HTTPException(status_code=404, detail="Unknown person.")

    cached = people_store.thumb_path(project.id, clip_index, person_id)
    if not cached.is_file():
        import cv2  # local import: the editor must work without a display stack

        best = person["best"]
        source = media_service.ensure_source(project.model_dump())
        capture = cv2.VideoCapture(str(source))
        try:
            capture.set(cv2.CAP_PROP_POS_MSEC, (start + float(best["t"])) * 1000.0)
            ok, frame = capture.read()
        finally:
            capture.release()
        if not ok or frame is None:
            raise HTTPException(status_code=500, detail="Could not read that frame.")
        height, width = frame.shape[:2]
        pad = 0.45
        x0 = int(max(0.0, (best["cx"] - best["w"] * (0.5 + pad))) * width)
        x1 = int(min(1.0, (best["cx"] + best["w"] * (0.5 + pad))) * width)
        y0 = int(max(0.0, (best["cy"] - best["h"] * (0.5 + pad))) * height)
        y1 = int(min(1.0, (best["cy"] + best["h"] * (0.5 + pad))) * height)
        if x1 - x0 < 8 or y1 - y0 < 8:
            raise HTTPException(status_code=404, detail="Face box too small for a thumbnail.")
        crop = frame[y0:y1, x0:x1]
        target = 128
        scale = target / max(1, min(crop.shape[:2]))
        crop = cv2.resize(crop, (max(1, int(crop.shape[1] * scale)), max(1, int(crop.shape[0] * scale))))
        cached.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(cached), crop, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    return FileResponse(cached, media_type="image/jpeg")


# ---------------------------------------------------------------------------
# Tahap 8.2 — full-duration preview with per-shot segment cache
# ---------------------------------------------------------------------------
@router.post("/api/preview/full/{project_id}", response_model=PreviewFullResponse)
def render_full_preview(
    project_id: str, payload: PreviewFullRequest | None = None
) -> PreviewFullResponse:
    from backend.services import preview_cache

    clip_index = payload.clip_index if payload else 0
    settings = payload.settings if payload else None
    try:
        result = preview_cache.build_full(project_id, clip_index, settings)
    except preview_cache.PreviewCacheError as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:300]) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("full preview failed")
        raise HTTPException(status_code=500, detail=str(exc)[:300]) from exc
    return PreviewFullResponse(**result)


@router.get("/api/preview/full/{project_id}/status", response_model=PreviewFullStatus)
def full_preview_status(project_id: str, clip_index: int = 0) -> PreviewFullStatus:
    from backend.services import preview_cache

    manifest = preview_cache.read_manifest(project_id, clip_index) or {}
    ready = bool(manifest.get("file")) and (
        config.PREVIEWS_DIR / str(manifest["file"])
    ).is_file()
    return PreviewFullStatus(
        project_id=project_id,
        clip_index=clip_index,
        ready=ready,
        file=manifest.get("file", ""),
        url=f"/api/preview/file/{manifest['file']}" if manifest.get("file") else "",
        duration=manifest.get("duration", 0.0),
        full_duration=manifest.get("full_duration", 0.0),
        truncated=manifest.get("truncated", False),
        width=manifest.get("width", 0),
        height=manifest.get("height", 0),
        segments=manifest.get("segments", []),
        cache_bytes=manifest.get("cache_bytes", 0),
        elapsed_ms=manifest.get("elapsed_ms", 0),
        rendered_segments=manifest.get("rendered_segments", 0),
        cached_segments=manifest.get("cached_segments", 0),
    )


@router.delete("/api/preview/full/{project_id}")
def purge_full_preview(project_id: str, clip_index: int = 0) -> dict[str, int]:
    from backend.services import preview_cache

    removed = preview_cache.purge(project_id, clip_index)
    return {"removed": removed}
