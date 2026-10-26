"""Filesystem-backed project store (Phase 2).

One JSON document per project in ``data/projects/<id>.json``; listing reads the
directory instead of keeping an index (decision D3: no database). Writes are
atomic (temp file + ``os.replace``) so an interrupted save never leaves a
half-written project behind.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from backend import config
from backend.schemas.project import (
    NAME_MAX,
    Project,
    ProjectCreate,
    ProjectSource,
    ProjectSummary,
    ProjectUpdate,
    ProjectVideo,
    utc_now,
)

logger = logging.getLogger("clipforge.projects")

_ID_RE = re.compile(r"^prj_\d{8}_[a-z0-9]{4,12}$")
_LOCK = threading.Lock()


class ProjectNotFound(Exception):
    """The requested project id does not exist on disk."""


class ProjectInvalid(Exception):
    """The payload or the id is not usable."""


def new_id() -> str:
    return f"prj_{datetime.now().strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"


def _path(project_id: str) -> Path:
    if not _ID_RE.match(project_id or ""):
        raise ProjectInvalid(f"Invalid project id: {project_id!r}")
    return config.PROJECTS_DIR / f"{project_id}.json"


def _write(project: Project) -> Project:
    path = _path(project.id)
    payload = project.model_dump(mode="json")
    tmp = path.with_suffix(".json.tmp")
    with _LOCK:
        config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    return project


def _read(project_id: str) -> Project:
    path = _path(project_id)
    if not path.exists():
        raise ProjectNotFound(project_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectInvalid(f"Project {project_id} is unreadable: {exc}") from exc
    return Project.model_validate(raw)


def _summary(project: Project, size_bytes: int) -> ProjectSummary:
    return ProjectSummary(
        id=project.id,
        name=project.name,
        created_at=project.created_at,
        updated_at=project.updated_at,
        video_title=project.video.title,
        thumbnail=project.video.thumbnail,
        duration=project.video.duration,
        source_type=project.source.type,
        clip_count=len(project.clips),
        size_bytes=size_bytes,
    )


def list_all() -> tuple[list[ProjectSummary], int]:
    """Return every readable project (newest first) plus the total size on disk."""
    config.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    items: list[ProjectSummary] = []
    total = 0
    for path in config.PROJECTS_DIR.glob("*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            project = Project.model_validate(raw)
        except Exception as exc:  # corrupt file: skip, never break the list
            logger.warning("skipping unreadable project %s: %s", path.name, exc)
            continue
        size = path.stat().st_size
        total += size
        items.append(_summary(project, size))
    items.sort(key=lambda item: item.updated_at, reverse=True)
    return items, total


def load(project_id: str) -> Project:
    return _read(project_id)


def _default_name(payload: ProjectCreate) -> str:
    candidate = (payload.name or "").strip() or payload.video.title.strip() or "Untitled project"
    return candidate[:NAME_MAX]


def _thumbnail(source: ProjectSource, video: ProjectVideo) -> str | None:
    """Fallback thumbnail so every project card has a visual (PRD F-56).

    yt-dlp supplies one when it downloads, but a re-analysis from the cached
    transcript skips the download, so derive the YouTube poster from the id.
    """
    if video.thumbnail:
        return video.thumbnail
    if source.type == "youtube" and source.video_id:
        return f"https://i.ytimg.com/vi/{source.video_id}/hqdefault.jpg"
    return None


def create(payload: ProjectCreate) -> Project:
    now = utc_now()
    video = payload.video.model_copy(update={"thumbnail": _thumbnail(payload.source, payload.video)})
    project = Project(
        id=new_id(),
        name=_default_name(payload),
        created_at=now,
        updated_at=now,
        source=payload.source,
        video=video,
        summary=payload.summary,
        language=payload.language,
        clips=payload.clips,
        heatmap=payload.heatmap,
        transcript=payload.transcript,
        render_preset=payload.render_preset,
        ai=payload.ai,
    )
    logger.info("creating project %s (%s clips)", project.id, len(project.clips))
    return _write(project)


def update(project_id: str, patch: ProjectUpdate) -> Project:
    project = _read(project_id)
    data = project.model_dump()
    changes = patch.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in changes.items():
        data[key] = value
    data["updated_at"] = utc_now()
    updated = Project.model_validate(data)
    return _write(updated)


def touch(project_id: str) -> Project:
    """Bump ``updated_at`` without changing content (used after a render)."""
    project = _read(project_id)
    project.updated_at = utc_now()
    return _write(project)


def record_export(project_id: str, entry: ProjectExport) -> Project:
    """Insert (or replace by filename) a render result in ``project.exports``."""
    project = _read(project_id)
    kept = [item for item in project.exports if item.file != entry.file]
    project.exports = [entry, *kept][:50]
    project.updated_at = utc_now()
    return _write(project)


def delete(project_id: str) -> None:
    path = _path(project_id)
    if not path.exists():
        raise ProjectNotFound(project_id)
    with _LOCK:
        path.unlink()


def duplicate(project_id: str) -> Project:
    project = _read(project_id)
    now = utc_now()
    clone = project.model_copy(
        update={
            "id": new_id(),
            "name": f"{project.name[: NAME_MAX - 8]} (copy)",
            "created_at": now,
            "updated_at": now,
            "exports": [],
        }
    )
    return _write(clone)


def export_text(project_id: str) -> str:
    project = _read(project_id)
    return json.dumps(project.model_dump(mode="json"), ensure_ascii=False, indent=2)


def import_payload(raw: Any) -> Project:
    """Import a previously exported project JSON as a new project."""
    if isinstance(raw, dict) and "project" in raw and isinstance(raw["project"], dict):
        raw = raw["project"]
    if not isinstance(raw, dict):
        raise ProjectInvalid("Import expects a project object.")
    data = dict(raw)
    data.pop("id", None)
    now = utc_now()
    data["created_at"] = now
    data["updated_at"] = now
    data["exports"] = []
    try:
        parsed = Project.model_validate({**data, "id": new_id()})
    except Exception as exc:
        raise ProjectInvalid(f"Not a valid ClipForge project file: {exc}") from exc
    parsed.name = f"{parsed.name[: NAME_MAX - 12]} (imported)"
    return _write(parsed)
