"""Pydantic models for the analyze pipeline (Phase 1).

These mirror the contract in PRD §6.2:
- `AnalyzeRequest`     — the POST /api/analyze body.
- `ViralClip`          — a single highlighted moment.
- `VideoAnalysis`      — structured output expected from the AI.
- `HeatmapPoint` / `TranscriptLine` — intermediate UI payloads.
- `AnalyzeResponse`    — final result emitted on the SSE `complete` event.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

SourceType = Literal["youtube", "gdrive", "url", "upload"]


class AnalyzeRequest(BaseModel):
    """Options the client posts to start an analysis."""

    url: str | None = None
    source_type: SourceType | None = None
    duration: Literal["auto", "15s", "30s", "60s"] = "30s"
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    custom_prompt: str | None = None
    range_start: float | None = None
    range_end: float | None = None
    target_clip_count: int | None = None
    cookies: str | None = None
    language: Literal["id", "en", "auto"] | None = None


class ViralClip(BaseModel):
    title: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    hook_time: float | None = None
    virality_score: int = Field(ge=1, le=100)
    key_quotes: list[str] = Field(default_factory=list)
    transcript: str = ""
    title_suggestion: str = ""
    caption_suggestion: str = ""
    hashtag_suggestion: str = ""
    # Phase 5 editor trim: optional in/out overrides (absolute seconds).
    # ``None`` means "use start_time / end_time".
    trim_start: float | None = None
    trim_end: float | None = None

    @field_validator("trim_start", "trim_end")
    @classmethod
    def _non_negative(cls, value: float | None) -> float | None:
        if value is None:
            return None
        return round(max(0.0, float(value)), 3)


class VideoAnalysis(BaseModel):
    summary: str = ""
    clips: list[ViralClip] = Field(default_factory=list)


class HeatmapPoint(BaseModel):
    start_time: float
    end_time: float
    value: float


class TranscriptWord(BaseModel):
    start: float
    end: float
    word: str
    probability: float | None = None


class TranscriptLine(BaseModel):
    start: float
    end: float
    text: str
    engagement: float | None = None
    words: list[TranscriptWord] = Field(default_factory=list)


class MediaMeta(BaseModel):
    """Resolvable metadata about the analyzed media."""

    video_id: str | None = None
    title: str = ""
    channel: str | None = None
    duration: float = 0.0
    thumbnail: str | None = None
    webpage_url: str | None = None
    source_type: SourceType = "url"
    local_path: str | None = None


class AnalyzeResponse(BaseModel):
    video_id: str | None = None
    title: str = ""
    channel: str | None = None
    duration: float = 0.0
    heatmap: list[HeatmapPoint] = Field(default_factory=list)
    summary: str = ""
    clips: list[ViralClip] = Field(default_factory=list)
    transcript: list[TranscriptLine] = Field(default_factory=list)
    model: str | None = None
    video_url: str | None = None
    source_type: SourceType | None = None


class UploadStartResponse(BaseModel):
    """Returned if an upload is *accepted* but not streamed (unused in Phase 1)."""

    message: str
