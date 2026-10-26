"""Render/export schemas (Phase 3).

`RenderSettingsModel` mirrors the frontend `RenderSettings` object (camelCase on
purpose — the same JSON is stored in `project.render_preset`, so keeping one
shape avoids translation layers).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AspectRatio = Literal["9:16", "1:1", "4:3", "16:9"]
EncoderChoice = Literal["auto", "videotoolbox", "cpu", "browser_wasm"]
RenderMode = Literal["separate", "merged"]
WatermarkPosition = Literal["top_left", "top_right", "bottom_left", "bottom_right", "center"]
# Phase 5 — streamer layouts + hook text.
StreamerPreset = Literal["none", "split_top_cam", "pip_corner"]
FacecamPosition = Literal["top_left", "top_right", "bottom_left", "bottom_right"]
HookPosition = Literal["top", "center", "bottom"]
HookCase = Literal["none", "upper"]
# Phase 6 — clip types (tracking presets). "auto" lets the backend decide.
ClipType = Literal[
    "auto",
    "podcast",
    "talking_head",
    "gaming",
    "tutorial",
    "webinar",
    "reaction",
    "event",
]
ClipStatus = Literal["pending", "preparing", "rendering", "completed", "error"]
BatchStatus = Literal["queued", "rendering", "completed", "failed"]

# Output resolution per aspect ratio (short side 1080 keeps files phone-friendly).
TARGET_SIZES: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:3": (1080, 1440),
    "16:9": (1920, 1080),
}

SUBTITLE_STYLES = (
    "viral_pop",
    "beast_punch",
    "cyber_violet",
    "fire_red",
    "electric_cyan",
    "golden_aura",
    "clean_minimal",
    "none",
)


class RenderSettingsModel(BaseModel):
    """Render settings — every field optional so old presets keep working."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    aspectRatio: AspectRatio = "9:16"
    background: Literal["black", "blurred"] = "blurred"
    faceTracking: bool = False  # wired up in Phase 5
    subtitles: bool = True
    subtitleStyle: str = "viral_pop"
    encoder: EncoderChoice = "auto"
    renderMode: RenderMode = "separate"

    # -- Phase 4: overlays & audio -------------------------------------------
    watermark: str | None = None  # burned-in text (channel handle, …)
    watermarkImage: str | None = None  # logo file name inside data/assets/watermarks
    watermarkPosition: WatermarkPosition = "bottom_right"
    watermarkOpacity: float = 0.75
    watermarkSize: int = 0  # % of output width, 0 = automatic
    bgmFile: str | None = None  # track name inside data/assets/bgm
    bgmVolume: float = 0.25
    bgmOffset: float = 0.0
    bgmLoop: bool = True
    voiceBoost: float = 1.0  # 1.0 = untouched, 1.5 = +50 %
    compilationTitle: str | None = None  # used for merged exports

    # -- Phase 5: face tracking, streamer layouts, hook text ------------------
    streamerPreset: StreamerPreset = "none"
    facecamPosition: FacecamPosition = "top_right"
    hookText: str | None = None  # big opening line burned into the clip
    hookPrefix: str | None = None  # emoji / label before the hook
    hookSuffix: str | None = None
    hookPosition: HookPosition = "top"
    hookDuration: float = 4.0  # seconds from clip start, 0 = whole clip
    hookSize: float = 7.0  # % of output width
    hookCase: HookCase = "upper"

    # -- Phase 6: clip type (tracking preset) --------------------------------
    clipType: ClipType = "auto"

    @field_validator("hookText", "hookPrefix", "hookSuffix")
    @classmethod
    def _clean_hook(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())[:80]
        return cleaned or None

    @field_validator("hookSize")
    @classmethod
    def _clamp_hook_size(cls, value: float) -> float:
        return round(min(16.0, max(3.0, float(value))), 1)

    @field_validator("hookDuration")
    @classmethod
    def _clamp_hook_duration(cls, value: float) -> float:
        return round(min(30.0, max(0.0, float(value))), 1)

    @field_validator("subtitleStyle")
    @classmethod
    def _known_style(cls, value: str) -> str:
        cleaned = (value or "").strip().lower()
        return cleaned if cleaned in SUBTITLE_STYLES else "viral_pop"

    @field_validator("watermark", "compilationTitle")
    @classmethod
    def _clean_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = " ".join(value.split())[:120]
        return cleaned or None

    @field_validator("watermarkImage", "bgmFile")
    @classmethod
    def _clean_file(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().replace("\\", "/").split("/")[-1]
        return cleaned or None

    @field_validator("watermarkOpacity")
    @classmethod
    def _clamp_opacity(cls, value: float) -> float:
        return round(min(1.0, max(0.0, float(value))), 2)

    @field_validator("watermarkSize")
    @classmethod
    def _clamp_size(cls, value: int) -> int:
        return min(60, max(0, int(value)))

    @field_validator("bgmVolume")
    @classmethod
    def _clamp_volume(cls, value: float) -> float:
        return round(min(1.0, max(0.0, float(value))), 2)

    @field_validator("bgmOffset")
    @classmethod
    def _clamp_offset(cls, value: float) -> float:
        return round(max(0.0, min(600.0, float(value))), 2)

    @field_validator("voiceBoost")
    @classmethod
    def _clamp_boost(cls, value: float) -> float:
        return round(min(2.0, max(0.0, float(value))), 2)


class RenderBatchRequest(BaseModel):
    project_id: str
    # None = every clip in the project.
    clip_indices: list[int] | None = None
    # None = the project's saved `render_preset`.
    settings: RenderSettingsModel | None = None

    @field_validator("clip_indices")
    @classmethod
    def _clean_indices(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        return sorted({int(item) for item in value if int(item) >= 0})


class RenderClipState(BaseModel):
    index: int
    title: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    status: ClipStatus = "pending"
    percent: float = 0.0
    file: str | None = None
    download_url: str | None = None
    size_bytes: int = 0
    error: str | None = None


class RenderBatchState(BaseModel):
    batch_id: str
    project_id: str
    project_name: str = ""
    status: BatchStatus = "queued"
    encoder: str = "libx264"
    width: int = 1080
    height: int = 1920
    subtitles: bool = True
    clips: list[RenderClipState] = Field(default_factory=list)
    percent: float = 0.0
    created_at: str = ""
    updated_at: str = ""
    error: str | None = None
    zip_url: str | None = None
    # Phase 4: merged compilations produce one file for the whole batch.
    merged: bool = False
    output_file: str | None = None
    output_url: str | None = None


class RenderBatchResponse(BaseModel):
    batch_id: str
    project_id: str
    clip_count: int
    progress_url: str


class HardwareAccelResponse(BaseModel):
    encoder: str
    platform: str
    ffmpeg: str
    ffmpeg_path: str
    videotoolbox: bool
    libx264: bool
    subtitles: bool
    overlays: bool = False
    audio_filters: bool = False
    face_tracking: bool = False
    note: str | None = None


# --- Phase 5: editor preview -------------------------------------------------


class PreviewRequest(BaseModel):
    """Render one clip at preview resolution with the current editor settings."""

    project_id: str
    clip_index: int = 0
    settings: RenderSettingsModel | None = None

    @field_validator("clip_index")
    @classmethod
    def _non_negative(cls, value: int) -> int:
        return max(0, int(value))


class PreviewResponse(BaseModel):
    file: str
    url: str
    cached: bool = False
    elapsed_ms: int = 0
    width: int = 0
    height: int = 0
    duration: float = 0.0
    faces: dict[str, Any] | None = None
    settings_hash: str = ""


class PreviewSegmentInfo(BaseModel):
    """One cached shot-sized slice of the full preview."""

    index: int
    start: float
    end: float
    key: str = ""
    cached: bool = False
    file: str = ""
    size: int = 0
    frames: int = 0


class PreviewFullResponse(BaseModel):
    """Tahap 8.2 — full-clip preview assembled from per-shot segments."""

    file: str
    url: str
    cached: bool = False
    elapsed_ms: int = 0
    width: int = 0
    height: int = 0
    duration: float = 0.0
    truncated: bool = False
    full_duration: float = 0.0
    segments: list[PreviewSegmentInfo] = []
    rendered_segments: int = 0
    cached_segments: int = 0
    audio_elapsed_ms: int = 0
    cache_bytes: int = 0
    faces: dict[str, Any] | None = None
    settings_hash: str = ""


class PreviewFullStatus(BaseModel):
    """Manifest of the cached full preview for one clip."""

    project_id: str
    clip_index: int
    ready: bool = False
    file: str = ""
    url: str = ""
    duration: float = 0.0
    full_duration: float = 0.0
    truncated: bool = False
    width: int = 0
    height: int = 0
    segments: list[PreviewSegmentInfo] = []
    cache_bytes: int = 0
    elapsed_ms: int = 0
    rendered_segments: int = 0
    cached_segments: int = 0


class PreviewFullRequest(BaseModel):
    """Body of `POST /api/preview/full/{project_id}`."""

    clip_index: int = 0
    settings: RenderSettingsModel | None = None


class FacePathResponse(BaseModel):
    project_id: str
    clip_index: int
    start: float
    end: float
    duration: float
    samples: int = 0
    hits: int = 0
    coverage: float = 0.0
    detector: str = "none"
    available: bool = False
    keyframes: list[dict[str, float]] = Field(default_factory=list)
    cached: bool = False
    # Phase 6: dense path, identity count and smoothness metrics.
    trajectory: list[dict[str, float]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    faces: int = 0
    hit_ratio: float = 0.0
    clip_type: str = "auto"
    params: dict[str, Any] = Field(default_factory=dict)


class TrackingParamsInfo(BaseModel):
    detect_fps: float = 10.0
    detect_interval: int = 1
    dead_zone: float = 0.0
    hold_frames: int = 0
    max_speed: float = 0.0
    speaker_min_switch: float = 0.0
    use_speaker: bool = True
    zoom: float = 1.0


class ClipTypeInfo(BaseModel):
    id: str
    label: str
    description: str = ""
    status: str = "preview"
    layout: str = "follow"
    params: TrackingParamsInfo


class AutoDetectResponse(BaseModel):
    project_id: str = ""
    clip_index: int = 0
    suggested: str = "talking_head"
    max_faces: int = 0
    samples: int = 0
    aspect: float = 0.0
    reason: str = ""


class StreamerPresetInfo(BaseModel):
    id: str
    label: str
    facecam: bool = False
    split: bool = False


STREAMER_PRESETS: tuple[StreamerPresetInfo, ...] = (
    StreamerPresetInfo(id="none", label="Single frame", facecam=False, split=False),
    StreamerPresetInfo(id="split_top_cam", label="Split (facecam on top)", facecam=True, split=True),
    StreamerPresetInfo(id="pip_corner", label="Picture-in-picture facecam", facecam=True, split=False),
)


# --- Phase 7, stage 3: manual shot-list editor -------------------------------
class ShotInfo(BaseModel):
    """One framing decision, as the browser sees it."""

    index: int = 1
    start: float = 0.0
    end: float = 0.0
    duration: float = 0.0
    kind: str = "follow_speaker"
    person: str | None = None
    personId: str | None = None
    frameCx: float | None = None
    zoom: str | float = "auto"
    zoomEntry: str = "normal"
    transition: str = "cut"
    source: str = "auto"
    locked: bool = False
    # Per-shot framing hints returned by plan.build() — not stored on disk.
    autoCx: float | None = None
    cxMin: float | None = None
    cxMax: float | None = None


class ShotListResponse(BaseModel):
    project_id: str
    clip_index: int
    start: float
    end: float
    duration: float
    # "stored" = the user has an edited list, "draft" = a fresh detection that
    # has not been persisted yet (the UI saves on the first edit).
    origin: str = "draft"
    updated_at: str | None = None
    shots: list[ShotInfo] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    # Server-side undo/redo (Phase 7, stage 3): the shot list is the source of
    # truth, so its edit history lives beside it and survives a reload. The UI
    # uses these two booleans to enable the buttons; the depths are for display.
    can_undo: bool = False
    can_redo: bool = False
    undo_depth: int = 0
    redo_depth: int = 0


class ShotSaveRequest(BaseModel):
    clip_index: int = Field(default=0, ge=0)
    shots: list[ShotInfo] = Field(default_factory=list)


class ShotHistoryRequest(BaseModel):
    """Undo/redo need nothing but which clip to walk."""

    clip_index: int = Field(default=0, ge=0)


class ShotSplitRequest(BaseModel):
    clip_index: int = Field(default=0, ge=0)
    at: float = Field(ge=0.0)


class ShotMergeRequest(BaseModel):
    clip_index: int = Field(default=0, ge=0)
    index: int = Field(ge=1)


class ShotMoveRequest(BaseModel):
    clip_index: int = Field(default=0, ge=0)
    index: int = Field(ge=1)
    at: float = Field(ge=0.0)


class ShotPatchRequest(BaseModel):
    clip_index: int = Field(default=0, ge=0)
    index: int = Field(ge=1)
    kind: str | None = None
    person: str | None = None
    personId: str | None = None
    frameCx: float | None = None
    zoom: str | float | None = None
    zoomEntry: str | None = None
    locked: bool | None = None


class ShotRedetectRequest(BaseModel):
    clip_index: int = Field(default=0, ge=0)
    # Re-run face/VAD detection instead of reusing the cache.
    fresh: bool = False


# --- Tahap 8, stage 1: the "People on screen" panel --------------------------
class PersonInfo(BaseModel):
    """One person the tracker found, after the user's corrections."""

    id: str
    letter: str = ""
    label: str = ""
    color: str = "#38bdf8"
    ignored: bool = False
    # Excluded because "expected people" is smaller than the number detected.
    excluded: bool = False
    ranges: list[list[float]] = Field(default_factory=list)
    talking: list[list[float]] = Field(default_factory=list)
    presence_seconds: float = 0.0
    talking_seconds: float = 0.0
    coverage: float = 0.0
    confidence: float = 0.0
    median_area: float = 0.0
    tracks: list[str] = Field(default_factory=list)
    merged_from: list[str] = Field(default_factory=list)
    split_from: str | None = None
    best: dict[str, float] | None = None
    ranges_count: int = 0


class PeopleWarning(BaseModel):
    """A clickable problem: jump the playhead to ``start`` to see it."""

    kind: str
    severity: str = "info"
    text: str = ""
    start: float = 0.0
    end: float | None = None
    person: str | None = None
    other: str | None = None
    score: float | None = None


class PeopleImpact(BaseModel):
    """What a correction would change, shown *before* the user applies it."""

    draft_shots_before: int = 0
    draft_shots_after: int = 0
    changed_boundaries: int = 0
    stored_shots: int = 0
    manual_shots: int = 0
    shots_with_missing_person: int = 0
    summary: str = ""


class PeopleResponse(BaseModel):
    project_id: str
    clip_index: int
    start: float
    end: float
    duration: float
    detected: int = 0
    active: int = 0
    expected: int | None = None
    revision: int = 1
    people: list[PersonInfo] = Field(default_factory=list)
    warnings: list[PeopleWarning] = Field(default_factory=list)
    # letter / person id → row in the activity matrix, so the shot editor can
    # send "follow person B" and have it mean the same face everywhere.
    person_index: dict[str, int] = Field(default_factory=dict)
    # merge candidates the tracker is *not* confident about
    suggestions: list[dict[str, Any]] = Field(default_factory=list)
    impact: PeopleImpact | None = None
    can_undo: bool = False
    can_redo: bool = False
    undo_depth: int = 0
    redo_depth: int = 0
    elapsed_ms: int = 0


class PeopleActionRequest(BaseModel):
    """One correction. ``action`` decides which of the other fields matter."""

    clip_index: int = Field(default=0, ge=0)
    action: str
    person: str | None = None
    target: str | None = None
    label: str | None = None
    at: float | None = None
    expected: int | str | None = None


class PeopleHistoryRequest(BaseModel):
    clip_index: int = Field(default=0, ge=0)
