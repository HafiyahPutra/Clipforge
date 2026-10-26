"""Analyze orchestration: download → local Whisper → heatmap → AI highlight scoring.

Runs in a worker thread and reports through an `emit(event, payload)` callback so
the router can forward everything over SSE without knowing the pipeline.
"""

from __future__ import annotations

import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from backend import config
from backend.schemas.analyze import AnalyzeRequest
from backend.services import (
    ai_service,
    download_service,
    heatmap_service,
    media_service,
    whisper_service,
)

EmitFn = Callable[[str, dict[str, Any]], None]

TARGET_SECONDS = {"15s": 15, "30s": 30, "60s": 60, "auto": 0}


class AnalyzeCancelled(RuntimeError):
    """Raised internally when the client disconnects mid-run."""


def new_job_dir() -> tuple[str, Path]:
    job_id = uuid.uuid4().hex[:12]
    path = config.JOBS_DIR / job_id
    path.mkdir(parents=True, exist_ok=True)
    return job_id, path


def run(
    request: AnalyzeRequest,
    *,
    emit: EmitFn,
    cancel: threading.Event,
    uploaded_path: str | None = None,
    uploaded_name: str | None = None,
) -> None:
    """Execute the full pipeline, emitting SSE-friendly events along the way."""
    job_id, job_dir = new_job_dir()
    stage = "starting"
    try:
        stage = "preparing"
        emit("progress", {"stage": stage, "percent": 0, "message": "Preparing job"})

        if uploaded_path:
            media = _prepare_upload(uploaded_path, uploaded_name, job_dir)
        else:
            if not request.url:
                raise download_service.DownloadError("Provide a video URL or upload a file.")
            stage = "downloading"
            media = download_service.download(
                request.url,
                job_dir,
                cookies_file=request.cookies,
                on_progress=_progress(emit, "downloading"),
                cancel=cancel,
            )
        _check_cancel(cancel)

        # Keep a copy in data/media so the saved project can be rendered later,
        # even after the temp cleanup worker wipes temp/jobs/<job>.
        media_service.store(str(media.get("local_path") or ""), str(media.get("video_id") or job_id))

        emit(
            "meta",
            {
                "video_id": media.get("video_id") or job_id,
                "title": media.get("title") or "Untitled",
                "channel": media.get("channel"),
                "duration": float(media.get("duration") or 0.0),
                "thumbnail": media.get("thumbnail"),
                "video_url": media.get("webpage_url") or request.url,
                "source_type": media.get("source_type") or "upload",
            },
        )

        video_path = Path(str(media["local_path"]))
        stage = "probing"
        emit("progress", {"stage": stage, "percent": 10, "message": "Reading media info"})
        info = download_service.probe(video_path)
        duration = float(info["duration"] or media.get("duration") or 0.0)
        if duration <= 0:
            raise download_service.MediaError(
                "This file has no readable duration — it may be audio-only or corrupted."
            )
        if not info["has_audio"]:
            raise download_service.MediaError(
                "This video has no audio track, so there is nothing to transcribe."
            )
        emit(
            "meta",
            {
                "video_id": media.get("video_id") or job_id,
                "title": media.get("title") or info.get("title") or "Untitled",
                "channel": media.get("channel"),
                "duration": duration,
                "thumbnail": media.get("thumbnail"),
                "video_url": media.get("webpage_url") or request.url,
                "source_type": media.get("source_type") or "upload",
            },
        )
        _check_cancel(cancel)

        stage = "extracting"
        emit("progress", {"stage": stage, "percent": 20, "message": "Extracting audio"})
        wav_path = download_service.extract_audio(video_path, job_dir / "audio.wav")
        _check_cancel(cancel)

        stage = "heatmap"
        emit("progress", {"stage": stage, "percent": 30, "message": "Measuring audio energy"})
        points = heatmap_service.audio_energy(wav_path)
        emit("heatmap", {"points": points})
        _check_cancel(cancel)

        stage = "transcribing"
        emit("progress", {"stage": stage, "percent": 35, "message": "Transcribing locally"})
        cache_key = _cache_key(media, video_path, request.language)
        transcript = whisper_service.transcribe(
            wav_path,
            language=request.language if request.language in {"id", "en"} else None,
            cache_key=cache_key,
            on_progress=_progress(emit, "transcribing", floor=35, ceiling=85),
            cancel=cancel,
        )
        _check_cancel(cancel)

        segments = [
            {"start": float(item["start"]), "end": float(item["end"]), "text": item["text"]}
            for item in transcript.get("segments", [])
        ]
        if not segments:
            raise ai_service.AIProviderError(
                "No speech was detected in this video, so there is nothing to score.",
                status_code=422,
            )

        lines = heatmap_service.attach_engagement(segments, points)
        emit(
            "transcript",
            {
                "language": transcript.get("language"),
                "cached": bool(transcript.get("cached")),
                "lines": [
                    {
                        "start": line["start"],
                        "end": line["end"],
                        "text": line["text"],
                        "engagement": line.get("engagement"),
                    }
                    for line in lines
                ],
            },
        )

        window = _apply_range(segments, request.range_start, request.range_end)
        target_seconds = TARGET_SECONDS.get(request.duration, 30)
        clip_count = request.target_clip_count or (6 if target_seconds <= 0 else 8)
        clip_count = max(1, min(int(clip_count), config.ANALYZE_MAX_CLIPS))

        stage = "analyzing"
        emit(
            "progress",
            {"stage": stage, "percent": 86, "message": "Scoring highlights with AI"},
        )
        analysis = ai_service.analyze_transcript(
            window,
            duration=duration,
            target_seconds=target_seconds,
            clip_count=clip_count,
            custom_prompt=request.custom_prompt,
            language=_output_language(request.language, transcript.get("language")),
            model=request.model,
            base_url=request.base_url,
            api_key=request.api_key,
            on_progress=_progress(emit, "analyzing", floor=86, ceiling=98),
            cancel=cancel,
        )
        _check_cancel(cancel)

        result = {
            "video_id": media.get("video_id") or job_id,
            "title": media.get("title") or info.get("title") or "Untitled",
            "channel": media.get("channel"),
            "duration": duration,
            "heatmap": points,
            "summary": analysis["summary"],
            "clips": analysis["clips"],
            "transcript": lines,
            "model": analysis["model"],
            "language": transcript.get("language"),
            "video_url": media.get("webpage_url") or request.url,
            "source_type": media.get("source_type") or "upload",
            "fallback": bool(analysis.get("fallback")),
            "warnings": analysis.get("warnings") or [],
        }
        emit(
            "clips",
            {
                "summary": analysis["summary"],
                "clips": analysis["clips"],
                "model": analysis["model"],
                "fallback": bool(analysis.get("fallback")),
                "warnings": analysis.get("warnings") or [],
            },
        )
        if analysis.get("fallback"):
            emit(
                "warning",
                {
                    "stage": stage,
                    "message": (
                        "Skor dihitung lokal (fallback heuristik) karena model AI gagal: "
                        + "; ".join(analysis.get("warnings") or [])[:400]
                    ),
                    "fallback": True,
                },
            )
        emit("progress", {"stage": "done", "percent": 100, "message": "Analysis complete"})
        emit("complete", result)

        # Keep the source video (needed for previews in Phase 3), drop the big WAV.
        try:
            wav_path.unlink(missing_ok=True)
        except OSError:
            pass

    except AnalyzeCancelled:
        emit("cancelled", {"stage": stage, "message": "Analysis cancelled"})
    except ai_service.AIProviderError as exc:
        if "cancel" in str(exc).lower():
            emit("cancelled", {"stage": stage, "message": "Analysis cancelled"})
        else:
            config.logger.error("Analyze failed at stage=%s: %s", stage, exc.message)
            emit(
                "error",
                {
                    "stage": stage,
                    "message": exc.message,
                    "status": exc.status_code,
                    "detail": exc.detail,
                },
            )
    except (download_service.DownloadError, download_service.MediaError, whisper_service.TranscriptionError) as exc:
        config.logger.error("Analyze failed at stage=%s: %s: %s", stage, type(exc).__name__, exc)
        emit("error", {"stage": stage, "message": str(exc), "detail": {"type": type(exc).__name__}})
    except Exception as exc:  # noqa: BLE001 - never leave the stream hanging
        config.logger.exception("Analyze job failed at stage=%s", stage)
        emit(
            "error",
            {
                "stage": stage,
                "message": f"{type(exc).__name__} at stage '{stage}': {exc}",
                "detail": {"type": type(exc).__name__},
            },
        )


def _prepare_upload(
    uploaded_path: str, uploaded_name: str | None, job_dir: Path
) -> dict[str, Any]:
    """Move an uploaded file into the job dir and describe it like a download."""
    source = Path(uploaded_path)
    if not source.exists():
        raise download_service.MediaError("The uploaded file disappeared before processing.")
    suffix = Path(uploaded_name or source.name).suffix or ".mp4"
    target = job_dir / f"source{suffix}"
    shutil.move(str(source), target)
    return {
        "local_path": str(target),
        "video_id": None,
        "title": Path(uploaded_name or target.name).stem,
        "channel": None,
        "duration": 0.0,
        "thumbnail": None,
        "webpage_url": None,
        "source_type": "upload",
    }


def _cache_key(media: dict[str, Any], video_path: Path, language: str | None) -> str:
    identity = media.get("video_id")
    if not identity:
        stat = video_path.stat()
        identity = f"{video_path.name}:{stat.st_size}:{int(stat.st_mtime)}"
    lang = language if language in {"id", "en"} else "auto"
    return f"{identity}|{config.WHISPER_MODEL}|{lang}"


def _apply_range(
    segments: list[dict[str, Any]], start: float | None, end: float | None
) -> list[dict[str, Any]]:
    if start is None and end is None:
        return segments
    low = float(start or 0.0)
    high = float(end) if end is not None else float("inf")
    window = [
        segment
        for segment in segments
        if float(segment["end"]) > low and float(segment["start"]) < high
    ]
    return window or segments


def _output_language(requested: str | None, detected: str | None) -> str | None:
    """Pick the language the AI should answer in.

    A forced language wins; otherwise the language Whisper detected is used, so the
    model is never asked to "guess" and cannot drift into an unrelated language.
    """
    if requested in {"id", "en"}:
        return requested
    return (detected or "").strip().lower() or None


def _progress(
    emit: EmitFn, stage: str, *, floor: float = 0.0, ceiling: float = 100.0
) -> Callable[[float, str], None]:
    """Map a 0–100 sub-progress into the stage's slice of the global bar."""

    def report(percent: float, message: str) -> None:
        span = ceiling - floor
        emit(
            "progress",
            {
                "stage": stage,
                "percent": round(floor + max(0.0, min(100.0, percent)) / 100.0 * span, 1),
                "message": message,
            },
        )

    return report


def _check_cancel(cancel: threading.Event) -> None:
    if cancel.is_set():
        raise AnalyzeCancelled("Analysis cancelled")
