"""Central configuration for ClipForge.

Everything is driven by environment variables (see `.env.example`); every value
has a safe local-first default so the app boots with zero configuration.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

# `.env` in the repo root wins over the one next to the backend package.
load_dotenv(ROOT_DIR / ".env")
load_dotenv(BASE_DIR / ".env")

APP_NAME = "clipforge"
APP_TITLE = "ClipForge API"
VERSION = "0.1.0"


def _path(env_key: str, default: Path) -> Path:
    raw = os.getenv(env_key)
    if not raw:
        return default
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_absolute() else (ROOT_DIR / candidate)


DATA_DIR = _path("DATA_DIR", ROOT_DIR / "data")
PROJECTS_DIR = DATA_DIR / "projects"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
# Cached source videos so a saved project can be rendered without re-uploading.
MEDIA_DIR = _path("MEDIA_DIR", DATA_DIR / "media")
TEMP_DIR = _path("TEMP_DIR", ROOT_DIR / "temp")
UPLOADS_DIR = TEMP_DIR / "uploads"
JOBS_DIR = TEMP_DIR / "jobs"
EXPORTS_DIR = _path("EXPORTS_DIR", ROOT_DIR / "exports")
FONTS_DIR = BASE_DIR / "fonts"
CASCADES_DIR = BASE_DIR / "cascades"
# User-supplied render assets (BGM tracks, watermark logos) — Phase 4.
ASSETS_DIR = _path("ASSETS_DIR", DATA_DIR / "assets")
BGM_DIR = ASSETS_DIR / "bgm"
WATERMARK_DIR = ASSETS_DIR / "watermarks"
# Face-tracking cache (Phase 5) and low-res editor previews.
FACES_DIR = _path("FACES_DIR", DATA_DIR / "faces")
# Per-clip shot lists the user edited (Phase 7, stage 3). Kept out of the
# project JSON so dragging a boundary never rewrites project state.
SHOTS_DIR = _path("SHOTS_DIR", DATA_DIR / "shots")
# People sidecar (Tahap 8.1): the user's corrections to the detected people.
PEOPLE_DIR = _path("PEOPLE_DIR", DATA_DIR / "people")
PEOPLE_THUMBS_DIR = PEOPLE_DIR / "thumbs"
PREVIEWS_DIR = _path("PREVIEWS_DIR", TEMP_DIR / "previews")
PREVIEW_SEG_DIR = _path("PREVIEW_SEG_DIR", PREVIEWS_DIR / "segments")
DIST_DIR = ROOT_DIR / "dist"

for _directory in (
    PROJECTS_DIR,
    TRANSCRIPTS_DIR,
    MEDIA_DIR,
    TEMP_DIR,
    UPLOADS_DIR,
    JOBS_DIR,
    EXPORTS_DIR,
    FONTS_DIR,
    CASCADES_DIR,
    ASSETS_DIR,
    BGM_DIR,
    WATERMARK_DIR,
    FACES_DIR,
    SHOTS_DIR,
    PEOPLE_DIR,
    PEOPLE_THUMBS_DIR,
    PREVIEWS_DIR,
    PREVIEW_SEG_DIR,
):
    _directory.mkdir(parents=True, exist_ok=True)

# --- AI provider (OpenAI-compatible) -----------------------------------------
AI_BASE_URL = os.getenv("AI_BASE_URL", "http://localhost:20128/v1").rstrip("/")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "")
AI_FALLBACK_MODELS = [
    model.strip()
    for model in os.getenv("AI_FALLBACK_MODELS", "").split(",")
    if model.strip()
]
AI_TIMEOUT_SECONDS = float(os.getenv("AI_TIMEOUT_SECONDS", "180"))
AI_TEMPERATURE = float(os.getenv("AI_TEMPERATURE", 0.4))
# Reasoning models (e.g. free-stack) consume output tokens before emitting any
# text; without a floor the response comes back truncated and unparseable.
AI_MIN_OUTPUT_TOKENS = int(os.getenv("AI_MIN_OUTPUT_TOKENS", 4096))
# Separate, short budget for the TCP/connect phase so a dead endpoint fails fast
# instead of eating the whole (deliberately generous) read timeout.
AI_CONNECT_TIMEOUT_SECONDS = float(os.getenv("AI_CONNECT_TIMEOUT_SECONDS", "15"))
# Retries for 429 / 5xx / timeouts, with exponential backoff. 0 disables.
AI_MAX_RETRIES = int(os.getenv("AI_MAX_RETRIES", "3"))
AI_RETRY_BASE_SECONDS = float(os.getenv("AI_RETRY_BASE_SECONDS", "2"))
# With no AI_MODEL set, ask the provider which models it exposes before scoring.
AI_AUTO_DISCOVER_MODEL = os.getenv("AI_AUTO_DISCOVER_MODEL", "true").lower() not in {
    "0",
    "false",
    "no",
}
# Some OpenAI-compatible proxies gate `GET /v1/models` but expose /v1/combos.
AI_DISCOVER_COMBOS = os.getenv("AI_DISCOVER_COMBOS", "true").lower() not in {"0", "false", "no"}
# How many auto-discovered models to keep as candidates.
AI_DISCOVER_LIMIT = int(os.getenv("AI_DISCOVER_LIMIT", "3"))
# Score locally (word density / pause / topic shift) when the model cannot.
# The UI is always told the result is a fallback — it never passes silently.
AI_FALLBACK_HEURISTIC = os.getenv("AI_FALLBACK_HEURISTIC", "true").lower() not in {
    "0",
    "false",
    "no",
}

# --- Local transcription (faster-whisper, CPU) --------------------------------
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_BEAM_SIZE = int(os.getenv("WHISPER_BEAM_SIZE", "5"))
WHISPER_VAD = os.getenv("WHISPER_VAD", "true").lower() not in {"0", "false", "no"}
# Empty = auto-detect the spoken language.
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "").strip()

# --- Download / analysis pipeline --------------------------------------------
YTDLP_MAX_HEIGHT = int(os.getenv("YTDLP_MAX_HEIGHT", "1080"))
YTDLP_SOCKET_TIMEOUT = int(os.getenv("YTDLP_SOCKET_TIMEOUT", "30"))
YTDLP_COOKIES_FILE = os.getenv("YTDLP_COOKIES_FILE", "").strip()
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "2048"))
# Transcript characters sent to the model per request; longer videos are chunked.
ANALYZE_CHUNK_CHARS = int(os.getenv("ANALYZE_CHUNK_CHARS", "6000"))
ANALYZE_MAX_CLIPS = int(os.getenv("ANALYZE_MAX_CLIPS", "20"))
ANALYZE_MIN_CLIP_SECONDS = float(os.getenv("ANALYZE_MIN_CLIP_SECONDS", "8"))

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "ffprobe")
# Builds without libass cannot burn subtitles in; these are probed as fallbacks.
FFMPEG_FALLBACK_BINS = [
    item.strip()
    for item in os.getenv(
        "FFMPEG_FALLBACK_BINS",
        "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg,/usr/local/opt/ffmpeg-full/bin/ffmpeg",
    ).split(",")
    if item.strip()
]

PHASE = "7 — shot list + hard cut + dynamic zoom"

# --- Video encoding ----------------------------------------------------------
FFMPEG_ENCODER = os.getenv("FFMPEG_ENCODER", "auto")
RENDER_FPS = int(os.getenv("RENDER_FPS", "30"))
RENDER_CRF = int(os.getenv("RENDER_CRF", "22"))
RENDER_PRESET = os.getenv("RENDER_PRESET", "veryfast")
RENDER_VIDEO_BITRATE = os.getenv("RENDER_VIDEO_BITRATE", "8M")
RENDER_AUDIO_BITRATE = os.getenv("RENDER_AUDIO_BITRATE", "160k")
RENDER_MAX_CLIPS = int(os.getenv("RENDER_MAX_CLIPS", "20"))
# Phase 4 — assets & audio
ASSET_MAX_MB = float(os.getenv("ASSET_MAX_MB", "64"))
BGM_DEFAULT_VOLUME = float(os.getenv("BGM_DEFAULT_VOLUME", "0.25"))
BGM_FADE_SECONDS = float(os.getenv("BGM_FADE_SECONDS", "0.8"))
VOICE_BOOST_MAX = float(os.getenv("VOICE_BOOST_MAX", "2.0"))
# Phase 5 — face tracking + editor preview
FACE_SAMPLE_FPS = float(os.getenv("FACE_SAMPLE_FPS", "2.0"))
FACE_MIN_HIT_RATIO = float(os.getenv("FACE_MIN_HIT_RATIO", "0.2"))
PREVIEW_WIDTH = int(os.getenv("PREVIEW_WIDTH", "540"))
PREVIEW_CRF = int(os.getenv("PREVIEW_CRF", "30"))
PREVIEW_PRESET = os.getenv("PREVIEW_PRESET", "ultrafast")
PREVIEW_MAX_SECONDS = float(os.getenv("PREVIEW_MAX_SECONDS", "20"))
# Tahap 8.2 — full-duration preview with per-shot segment cache.
PREVIEW_FULL_MAX_SECONDS = float(os.getenv("PREVIEW_FULL_MAX_SECONDS", "180"))
PREVIEW_FULL_WIDTH_SHORT = int(os.getenv("PREVIEW_FULL_WIDTH_SHORT", "540"))
PREVIEW_FULL_WIDTH_LONG = int(os.getenv("PREVIEW_FULL_WIDTH_LONG", "360"))
PREVIEW_FULL_LONG_THRESHOLD = float(os.getenv("PREVIEW_FULL_LONG_THRESHOLD", "60.0"))

# --- Server ------------------------------------------------------------------
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000",
    ).split(",")
    if origin.strip()
]

# --- Optional protection for destructive endpoints ---------------------------
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "") or os.getenv("CHEAT_CLIP_API_KEY", "")

# --- Temp storage housekeeping ----------------------------------------------
TEMP_MAX_AGE_SECONDS = int(os.getenv("TEMP_MAX_AGE_SECONDS", "3600"))
TEMP_STORAGE_QUOTA_GB = float(os.getenv("TEMP_STORAGE_QUOTA_GB", "5.0"))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "1800"))

# --- Logging -----------------------------------------------------------------
logger = logging.getLogger("clipforge")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    )
    logger.addHandler(_handler)
logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
logger.propagate = False


def mask_secret(value: str | None) -> str:
    """Return a log-safe representation of a secret."""
    if not value:
        return "<unset>"
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}…{value[-2:]}"


def public_config() -> dict[str, object]:
    """Configuration that is safe to expose through the API/UI."""
    return {
        "app": APP_NAME,
        "version": VERSION,
        "ai": {
            "base_url": AI_BASE_URL,
            "model": AI_MODEL,
            "has_api_key": bool(AI_API_KEY),
            "fallback_models": AI_FALLBACK_MODELS,
            "timeout_seconds": AI_TIMEOUT_SECONDS,
            "max_retries": AI_MAX_RETRIES,
        },
        "whisper": {
            "model": WHISPER_MODEL,
            "device": WHISPER_DEVICE,
            "compute_type": WHISPER_COMPUTE_TYPE,
        },
        "encoder": FFMPEG_ENCODER,
        "paths": {
            "data": str(DATA_DIR),
            "media": str(MEDIA_DIR),
            "temp": str(TEMP_DIR),
            "exports": str(EXPORTS_DIR),
        },
    }
