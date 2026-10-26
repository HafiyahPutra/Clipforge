"""Shared Pydantic v2 schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = Field(examples=["ok"])
    app: str = Field(examples=["clipforge"])
    version: str = Field(examples=["0.1.0"])


class VersionResponse(BaseModel):
    name: str = Field(examples=["ClipForge"])
    version: str = Field(examples=["0.1.0"])
    phase: str = Field(examples=["0 — scaffold"])
    paths: dict[str, str] = Field(
        default_factory=dict,
        description="Resolved storage roots, handy when running in Docker.",
    )


class ModelItem(BaseModel):
    id: str
    owned_by: str = ""
    created: int | None = None


class ModelsResponse(BaseModel):
    models: list[ModelItem]
    count: int
    base_url: str

