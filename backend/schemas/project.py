"""Project history schemas (Phase 2).

A project is a single JSON document on disk — no database, per decision D3.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from backend.schemas.analyze import HeatmapPoint, TranscriptLine, ViralClip

ProjectSourceType = Literal["youtube", "gdrive", "upload", "url"]
ExportStatus = Literal["queued", "rendering", "completed", "failed"]

NAME_MAX = 120


def utc_now() -> str:
    """ISO-8601 UTC timestamp with a trailing Z (matches the PRD example)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ProjectSource(BaseModel):
    type: ProjectSourceType = "url"
    url: str | None = None
    video_id: str | None = None
    local_path: str | None = None


class ProjectVideo(BaseModel):
    title: str = "Untitled video"
    duration: float = 0.0
    channel: str | None = None
    thumbnail: str | None = None


class ProjectAi(BaseModel):
    base_url: str | None = None
    model: str | None = None


class ProjectExport(BaseModel):
    file: str
    created_at: str = Field(default_factory=utc_now)
    size_bytes: int = 0
    status: ExportStatus = "completed"
    # Phase 3 render metadata (optional so Phase 2 exports still load).
    clip_index: int | None = None
    title: str | None = None
    batch_id: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None


class Project(BaseModel):
    id: str
    name: str
    created_at: str
    updated_at: str
    source: ProjectSource = Field(default_factory=ProjectSource)
    video: ProjectVideo = Field(default_factory=ProjectVideo)
    summary: str = ""
    language: str | None = None
    clips: list[ViralClip] = Field(default_factory=list)
    heatmap: list[HeatmapPoint] = Field(default_factory=list)
    transcript: list[TranscriptLine] = Field(default_factory=list)
    render_preset: dict[str, Any] = Field(default_factory=dict)
    exports: list[ProjectExport] = Field(default_factory=list)
    ai: ProjectAi = Field(default_factory=ProjectAi)


class ProjectSummary(BaseModel):
    id: str
    name: str
    created_at: str
    updated_at: str
    video_title: str = "Untitled video"
    thumbnail: str | None = None
    duration: float = 0.0
    source_type: ProjectSourceType = "url"
    clip_count: int = 0
    size_bytes: int = 0


class ProjectListResponse(BaseModel):
    projects: list[ProjectSummary]
    count: int = 0
    total_size_bytes: int = 0


class ProjectCreate(BaseModel):
    """Payload the Analyze page posts once an analysis finishes."""

    name: str | None = None
    source: ProjectSource = Field(default_factory=ProjectSource)
    video: ProjectVideo = Field(default_factory=ProjectVideo)
    summary: str = ""
    language: str | None = None
    clips: list[ViralClip] = Field(default_factory=list)
    heatmap: list[HeatmapPoint] = Field(default_factory=list)
    transcript: list[TranscriptLine] = Field(default_factory=list)
    render_preset: dict[str, Any] = Field(default_factory=dict)
    ai: ProjectAi = Field(default_factory=ProjectAi)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split()).strip()[:NAME_MAX]
        return cleaned or None


class ProjectUpdate(BaseModel):
    """Partial update — only the fields present in the body are applied."""

    name: str | None = None
    summary: str | None = None
    language: str | None = None
    clips: list[ViralClip] | None = None
    heatmap: list[HeatmapPoint] | None = None
    transcript: list[TranscriptLine] | None = None
    render_preset: dict[str, Any] | None = None
    exports: list[ProjectExport] | None = None
    video: ProjectVideo | None = None
    ai: ProjectAi | None = None

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split()).strip()[:NAME_MAX]
        if not cleaned:
            raise ValueError("Project name cannot be empty.")
        return cleaned
