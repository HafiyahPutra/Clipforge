"""Local transcription with faster-whisper (CTranslate2) — no cloud, no torch.

Design notes
------------
* The model is loaded lazily on the first transcription and kept in memory so a
  second clip/video does not pay the load cost again (`unload_model()` frees it).
* `word_timestamps=True` gives the word-level data the karaoke subtitles in
  Phase 3 need; nothing here calls the network except the *first* model download
  from Hugging Face, which faster-whisper does itself.
* Transcripts are cached as JSON under `data/transcripts/<key>.json` (F-10) so
  re-analyzing the same video skips the slowest stage.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import threading
from pathlib import Path
from typing import Any, Callable

from backend import config

ProgressFn = Callable[[float, str], None]


class TranscriptionError(RuntimeError):
    """Raised when the local Whisper stack is missing or fails."""


_model_lock = threading.Lock()
_model: Any | None = None
_model_key: str | None = None


def status() -> dict[str, object]:
    """Report whether the local Whisper stack is installed and configured."""
    installed = importlib.util.find_spec("faster_whisper") is not None
    return {
        "available": installed,
        "engine": "faster-whisper",
        "model": config.WHISPER_MODEL,
        "device": config.WHISPER_DEVICE,
        "compute_type": config.WHISPER_COMPUTE_TYPE,
        "loaded": _model is not None,
        "phase": config.PHASE,
    }


def is_model_loaded() -> bool:
    return _model is not None


def unload_model() -> None:
    """Drop the model so the RAM goes back to the OS (F-06 AC1)."""
    global _model, _model_key
    with _model_lock:
        _model = None
        _model_key = None


def _get_model(model_size: str | None = None) -> Any:
    """Load (once) and return the WhisperModel for the configured size."""
    global _model, _model_key

    size = (model_size or config.WHISPER_MODEL or "base").strip()
    key = f"{size}|{config.WHISPER_DEVICE}|{config.WHISPER_COMPUTE_TYPE}"
    with _model_lock:
        if _model is not None and _model_key == key:
            return _model
        if importlib.util.find_spec("faster_whisper") is None:
            raise TranscriptionError(
                "faster-whisper is not installed. Run `npm run dev` once (it bootstraps "
                "the Python environment) or `pip install -r backend/requirements.txt`."
            )
        from faster_whisper import WhisperModel  # imported lazily: keeps boot fast

        config.logger.info(
            "Loading Whisper model '%s' (device=%s, compute_type=%s)",
            size,
            config.WHISPER_DEVICE,
            config.WHISPER_COMPUTE_TYPE,
        )
        _model = WhisperModel(
            size,
            device=config.WHISPER_DEVICE,
            compute_type=config.WHISPER_COMPUTE_TYPE,
        )
        _model_key = key
        return _model


def cache_path(key: str) -> Path:
    safe = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return config.TRANSCRIPTS_DIR / f"{safe}.json"


def load_cached(key: str) -> dict[str, Any] | None:
    path = cache_path(key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _store_cache(key: str, payload: dict[str, Any]) -> None:
    try:
        cache_path(key).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:  # cache is an optimisation, never fatal
        config.logger.warning("Could not write transcript cache: %s", exc)


def transcribe(
    audio_path: str | Path,
    *,
    language: str | None = None,
    model_size: str | None = None,
    cache_key: str | None = None,
    on_progress: ProgressFn | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    """Transcribe `audio_path` and return a JSON-serialisable transcript.

    Shape::

        {"language": "id", "language_probability": 0.98, "duration": 63.4,
         "text": "…", "cached": false,
         "segments": [{"start", "end", "text",
                       "words": [{"start", "end", "word", "probability"}]}]}
    """
    if cache_key:
        cached = load_cached(cache_key)
        if cached is not None:
            cached["cached"] = True
            if on_progress:
                on_progress(100.0, "Transcript loaded from cache")
            return cached

    model = _get_model(model_size)
    lang = language or config.WHISPER_LANGUAGE or None
    if lang == "auto":
        lang = None

    if on_progress:
        on_progress(0.0, "Transcribing audio locally")

    try:
        segments_iter, info = model.transcribe(
            str(audio_path),
            language=lang,
            beam_size=config.WHISPER_BEAM_SIZE,
            word_timestamps=True,
            vad_filter=config.WHISPER_VAD,
            condition_on_previous_text=False,
        )
    except Exception as exc:  # noqa: BLE001 - normalised for the SSE stream
        raise TranscriptionError(f"Whisper could not read the audio: {exc}") from exc

    total = float(getattr(info, "duration", 0.0) or 0.0)
    segments: list[dict[str, Any]] = []
    try:
        for segment in segments_iter:
            if cancel is not None and cancel.is_set():
                raise TranscriptionError("Transcription cancelled")
            words = [
                {
                    "start": round(float(word.start), 3),
                    "end": round(float(word.end), 3),
                    "word": word.word,
                    "probability": round(float(word.probability), 4),
                }
                for word in (segment.words or [])
            ]
            segments.append(
                {
                    "start": round(float(segment.start), 3),
                    "end": round(float(segment.end), 3),
                    "text": (segment.text or "").strip(),
                    "words": words,
                }
            )
            if on_progress and total > 0:
                pct = min(99.0, float(segment.end) / total * 100.0)
                on_progress(pct, "Transcribing audio locally")
    except TranscriptionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TranscriptionError(f"Whisper failed while decoding: {exc}") from exc

    payload: dict[str, Any] = {
        "language": getattr(info, "language", None) or lang or "unknown",
        "language_probability": round(
            float(getattr(info, "language_probability", 0.0) or 0.0), 4
        ),
        "duration": round(total or (segments[-1]["end"] if segments else 0.0), 3),
        "text": " ".join(item["text"] for item in segments).strip(),
        "segments": segments,
        "cached": False,
        "model": model_size or config.WHISPER_MODEL,
    }
    if cache_key:
        _store_cache(cache_key, payload)
    if on_progress:
        on_progress(100.0, "Transcription finished")
    return payload
