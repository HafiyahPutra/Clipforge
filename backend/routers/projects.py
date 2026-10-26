"""Project history endpoints (Phase 2).

CRUD + duplicate + JSON export/import over the filesystem store in
``data/projects``. No database, per decision D3.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Response

from backend.schemas.project import (
    Project,
    ProjectCreate,
    ProjectListResponse,
    ProjectUpdate,
)
from backend.services import project_store

logger = logging.getLogger("clipforge.projects")
router = APIRouter(prefix="/api/projects", tags=["Projects"])


def _not_found(project_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Project {project_id} not found.")


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _slug(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-._")
    return (cleaned or "project")[:60]


@router.get("", response_model=ProjectListResponse)
def list_projects() -> ProjectListResponse:
    projects, total = project_store.list_all()
    return ProjectListResponse(projects=projects, count=len(projects), total_size_bytes=total)


@router.post("", response_model=Project, status_code=201)
def create_project(payload: ProjectCreate) -> Project:
    try:
        return project_store.create(payload)
    except project_store.ProjectInvalid as exc:
        raise _bad_request(exc) from exc


@router.post("/import", response_model=Project, status_code=201)
def import_project(payload: Any = Body(...)) -> Project:
    try:
        return project_store.import_payload(payload)
    except project_store.ProjectInvalid as exc:
        raise _bad_request(exc) from exc


@router.get("/{project_id}", response_model=Project)
def get_project(project_id: str) -> Project:
    try:
        return project_store.load(project_id)
    except project_store.ProjectNotFound as exc:
        raise _not_found(project_id) from exc
    except project_store.ProjectInvalid as exc:
        raise _bad_request(exc) from exc


@router.put("/{project_id}", response_model=Project)
def update_project(project_id: str, payload: ProjectUpdate) -> Project:
    try:
        return project_store.update(project_id, payload)
    except project_store.ProjectNotFound as exc:
        raise _not_found(project_id) from exc
    except project_store.ProjectInvalid as exc:
        raise _bad_request(exc) from exc


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str) -> Response:
    try:
        project_store.delete(project_id)
    except project_store.ProjectNotFound as exc:
        raise _not_found(project_id) from exc
    except project_store.ProjectInvalid as exc:
        raise _bad_request(exc) from exc
    return Response(status_code=204)


@router.post("/{project_id}/duplicate", response_model=Project, status_code=201)
def duplicate_project(project_id: str) -> Project:
    try:
        return project_store.duplicate(project_id)
    except project_store.ProjectNotFound as exc:
        raise _not_found(project_id) from exc
    except project_store.ProjectInvalid as exc:
        raise _bad_request(exc) from exc


@router.get("/{project_id}/export")
def export_project(project_id: str) -> Response:
    try:
        project = project_store.load(project_id)
        text = project_store.export_text(project_id)
    except project_store.ProjectNotFound as exc:
        raise _not_found(project_id) from exc
    except project_store.ProjectInvalid as exc:
        raise _bad_request(exc) from exc
    filename = f"{_slug(project.name)}-{project.id}.json"
    return Response(
        content=text,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
