"""Render queue + worker (Phase 3, extended in Phase 4).

One worker thread per batch. Each clip is cut, reframed, subtitled, watermarked
and mixed with optional BGM by `video_engine.render_clip`; in `merged` mode the
parts are then concatenated into a single compilation. Progress is broadcast to
any number of SSE subscribers, and finished files are recorded on the project
(`exports`) so the history survives a restart.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import re
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend import config
from backend.schemas.project import ProjectExport, utc_now
from backend.schemas.render import (
    TARGET_SIZES,
    RenderBatchState,
    RenderClipState,
    RenderSettingsModel,
)
from backend.services import (
    asset_service,
    edit_service,
    media_service,
    project_store,
    video_engine,
)

logger = logging.getLogger("clipforge.render")

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_BATCHES: dict[str, _Batch] = {}
_LOCK = threading.Lock()


class RenderInvalid(RuntimeError):
    """Bad render request (no clips, unknown index, …)."""


@dataclass
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: "asyncio.Queue[tuple[str, dict[str, Any]]]"


@dataclass
class _Batch:
    state: RenderBatchState
    settings: RenderSettingsModel
    indices: list[int]
    subscribers: list[_Subscriber] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    finished_at: float = 0.0

    # -- events ---------------------------------------------------------------
    def emit(self, event: str, payload: dict[str, Any]) -> None:
        for subscriber in list(self.subscribers):
            try:
                subscriber.loop.call_soon_threadsafe(subscriber.queue.put_nowait, (event, payload))
            except RuntimeError:  # loop already closed
                continue

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return self.state.model_dump()


def _slug(value: str, fallback: str = "clip", limit: int = 48) -> str:
    cleaned = _SLUG_RE.sub("-", (value or "").lower()).strip("-")
    return (cleaned[:limit].strip("-") or fallback)


def _prune(now: float) -> None:
    stale = [
        batch_id
        for batch_id, batch in _BATCHES.items()
        if batch.finished_at and now - batch.finished_at > 3600
    ]
    for batch_id in stale:
        _BATCHES.pop(batch_id, None)


def _new_batch_id() -> str:
    return f"rnd_{time.strftime('%Y%m%d')}_{int(time.time() * 1000) % 1_000_000:06d}"


def _settings_for(project: Any, override: RenderSettingsModel | None) -> RenderSettingsModel:
    if override is not None:
        return override
    preset = project.render_preset or {}
    try:
        return RenderSettingsModel.model_validate(preset)
    except Exception:  # noqa: BLE001 - a broken preset must not block rendering
        logger.warning("invalid render_preset on %s, using defaults", project.id)
        return RenderSettingsModel()


def start(
    project_id: str,
    indices: list[int] | None,
    settings: RenderSettingsModel | None = None,
) -> RenderBatchState:
    project = project_store.load(project_id)  # raises ProjectNotFound
    if not project.clips:
        raise RenderInvalid("This project has no clips to render. Run an analysis first.")

    total = len(project.clips)
    if indices is None:
        selected = list(range(total))
    else:
        selected = [index for index in indices if 0 <= index < total]
        if not selected:
            raise RenderInvalid("None of the selected clips exist in this project.")
    if len(selected) > config.RENDER_MAX_CLIPS:
        raise RenderInvalid(
            f"Too many clips in one batch ({len(selected)} > {config.RENDER_MAX_CLIPS})."
        )

    resolved = _settings_for(project, settings)
    width, height = TARGET_SIZES.get(resolved.aspectRatio, TARGET_SIZES["9:16"])
    caps = video_engine.capabilities()
    encoder = video_engine.pick_encoder(resolved.encoder)
    wants_subs = resolved.subtitles and resolved.subtitleStyle != "none"

    now = utc_now()
    clips = []
    for index in selected:
        clip = project.clips[index]
        clips.append(
            RenderClipState(
                index=index,
                title=clip.title or f"Clip {index + 1}",
                start_time=float(clip.start_time),
                end_time=float(clip.end_time),
            )
        )

    state = RenderBatchState(
        batch_id=_new_batch_id(),
        project_id=project.id,
        project_name=project.name,
        status="queued",
        encoder=encoder,
        width=width,
        height=height,
        subtitles=bool(wants_subs and caps["libass"]),
        clips=clips,
        merged=resolved.renderMode == "merged",
        created_at=now,
        updated_at=now,
    )
    batch = _Batch(state=state, settings=resolved, indices=selected)

    with _LOCK:
        _prune(time.time())
        _BATCHES[state.batch_id] = batch

    thread = threading.Thread(
        target=_run_batch,
        args=(batch, project.id, project.name),
        name=f"render-{state.batch_id}",
        daemon=True,
    )
    batch.thread = thread
    thread.start()
    logger.info("batch %s queued (%d clips, %s)", state.batch_id, len(selected), encoder)
    return state


def get(batch_id: str) -> RenderBatchState:
    batch = _BATCHES.get(batch_id)
    if batch is None:
        raise RenderInvalid("Unknown render batch.")
    with batch.lock:
        return batch.state.model_copy(deep=True)


def subscribe(batch_id: str) -> tuple[_Subscriber, dict[str, Any]]:
    batch = _BATCHES.get(batch_id)
    if batch is None:
        raise RenderInvalid("Unknown render batch.")
    subscriber = _Subscriber(loop=asyncio.get_running_loop(), queue=asyncio.Queue())
    with batch.lock:
        batch.subscribers.append(subscriber)
        snapshot = batch.state.model_dump()
    return subscriber, snapshot


def unsubscribe(batch_id: str, subscriber: _Subscriber) -> None:
    batch = _BATCHES.get(batch_id)
    if batch is None:
        return
    with batch.lock:
        if subscriber in batch.subscribers:
            batch.subscribers.remove(subscriber)


def cancel(batch_id: str) -> RenderBatchState:
    batch = _BATCHES.get(batch_id)
    if batch is None:
        raise RenderInvalid("Unknown render batch.")
    batch.cancel.set()
    return get(batch_id)


def retry(batch_id: str, indices: list[int] | None = None) -> RenderBatchState:
    batch = _BATCHES.get(batch_id)
    if batch is None:
        raise RenderInvalid("Unknown render batch.")
    merged = batch.settings.renderMode == "merged"
    with batch.lock:
        if merged:
            # A compilation is all-or-nothing: redo every clip and merge again.
            failed = list(batch.state.clips)
        else:
            failed = [
                clip
                for clip in batch.state.clips
                if clip.status == "error" and (indices is None or clip.index in indices)
            ]
        if not failed:
            raise RenderInvalid("Nothing to retry — no failed clips in this batch.")
        if merged:
            batch.state.merged = False
            batch.state.output_file = None
            batch.state.output_url = None
        for clip in failed:
            clip.status = "pending"
            clip.percent = 0.0
            clip.error = None
        batch.state.status = "queued"
        batch.state.error = None
        batch.indices = [clip.index for clip in failed]
    batch.cancel.clear()
    thread = threading.Thread(
        target=_run_batch,
        args=(batch, batch.state.project_id, batch.state.project_name),
        name=f"render-{batch_id}-retry",
        daemon=True,
    )
    batch.thread = thread
    thread.start()
    return get(batch_id)


# --- worker ------------------------------------------------------------------


def _update_clip(batch: _Batch, index: int, **changes: Any) -> RenderClipState:
    with batch.lock:
        target = next((clip for clip in batch.state.clips if clip.index == index), None)
        if target is None:
            raise RenderInvalid("Clip vanished from the batch.")
        for key, value in changes.items():
            setattr(target, key, value)
        batch.state.updated_at = utc_now()
        batch.state.percent = round(
            sum(clip.percent for clip in batch.state.clips) / max(1, len(batch.state.clips)), 2
        )
        return target.model_copy()


def _set_batch(batch: _Batch, **changes: Any) -> None:
    with batch.lock:
        for key, value in changes.items():
            setattr(batch.state, key, value)
        batch.state.updated_at = utc_now()


def _asset(kind: str, name: str | None) -> Path | None:
    """Resolve a preset asset name to a file, tolerating missing files."""
    if not name:
        return None
    try:
        return asset_service.resolve(kind, name)
    except asset_service.AssetInvalid:
        logger.warning("%s asset '%s' is missing — skipping it", kind, name)
        return None


def _run_batch(batch: _Batch, project_id: str, project_name: str) -> None:
    state = batch.state
    try:
        project = project_store.load(project_id)
        project_payload = project.model_dump()
        settings = batch.settings
        merged = settings.renderMode == "merged"

        _set_batch(batch, status="rendering")
        batch.emit("batch", batch.snapshot())

        source = media_service.ensure_source(
            project_payload,
            on_progress=lambda pct, label: batch.emit("stage", {"label": label, "percent": pct}),
            cancel=batch.cancel,
        )
        info = video_engine.probe(source)
        has_audio = bool(info.get("has_audio"))
        transcript = [line.model_dump() for line in project.transcript]

        bgm_path = _asset("bgm", settings.bgmFile)
        logo_path = _asset("watermarks", settings.watermarkImage)

        out_dir = config.EXPORTS_DIR / project.id
        out_dir.mkdir(parents=True, exist_ok=True)
        base_slug = _slug(project_name, fallback=project.id, limit=40)
        parts_dir = out_dir / f".parts-{state.batch_id}"
        if merged:
            parts_dir.mkdir(parents=True, exist_ok=True)

        rendered: list[tuple[Path, float]] = []
        failures = 0
        for index in batch.indices:
            if batch.cancel.is_set():
                _update_clip(batch, index, status="error", error="Cancelled.")
                batch.emit("clip", _update_clip(batch, index))
                failures += 1
                continue

            clip = project.clips[index]
            start, end = edit_service.clip_window(clip)
            duration = max(0.0, end - start)

            current = _update_clip(batch, index, status="rendering", percent=0.0, error=None)
            batch.emit("clip", current.model_dump())

            file_name = f"{base_slug}-{index + 1:02d}-{_slug(clip.title, fallback='clip', limit=32)}.mp4"
            out_path = (parts_dir / f"part-{index:02d}.mp4") if merged else (out_dir / file_name)

            ass_text = None
            if state.subtitles and transcript:
                ass_text = video_engine.build_ass(
                    transcript,
                    clip_start=start,
                    clip_end=end,
                    width=state.width,
                    height=state.height,
                    style=settings.subtitleStyle,
                )

            last_emit = 0.0

            faces = edit_service.face_path(
                source, start, end, settings, project_id=project.id, clip_index=index
            )
            hook = edit_service.hook_line(settings)
            # Whole_frame shots are composited by the engine on top of the
            # face-follow crop, so the renderer needs their clip-relative spans.
            # The stored (user-edited) shot list wins over the draft, otherwise
            # the editor would be decoration and the export would ignore it.
            plan = (
                edit_service.face_plan(
                    faces,
                    width=state.width,
                    height=state.height,
                    shots=edit_service.stored_shots(project.id, index),
                )
                if faces
                else {}
            )
            whole_spans = edit_service.whole_spans(plan, start, end) if plan else []
            if whole_spans and edit_service.effective_streamer(settings) in {"split_top_cam", "pip_corner"}:
                # The streamer layouts build their own frame; a whole-frame band
                # would cover the cam/PiP, so the shot falls back to the crop.
                whole_spans = []
                plan.setdefault("metrics", {}).setdefault("warnings", []).append(
                    "whole_frame shots are ignored while a streamer layout is active"
                )
            if faces:
                batch.emit(
                    "stage",
                    {
                        "label": f"Face tracking clip {index + 1} "
                        f"({faces.get('detector')}, {float(faces.get('coverage') or 0) * 100:.0f}%)",
                        "percent": 0.0,
                    },
                )

            def on_progress(ratio: float, _index: int = index) -> None:
                nonlocal last_emit
                now = time.time()
                if ratio < 1.0 and now - last_emit < 0.35:
                    return
                last_emit = now
                updated = _update_clip(batch, _index, percent=round(ratio * 100, 1))
                batch.emit("clip", updated.model_dump())

            try:
                size = video_engine.render_clip(
                    source,
                    out_path,
                    start=start,
                    end=end,
                    width=state.width,
                    height=state.height,
                    background=settings.background,
                    encoder=settings.encoder,
                    ass_text=ass_text,
                    has_audio=has_audio,
                    watermark_text=settings.watermark,
                    watermark_image=logo_path,
                    watermark_position=settings.watermarkPosition,
                    watermark_opacity=settings.watermarkOpacity,
                    watermark_size=settings.watermarkSize,
                    bgm_path=bgm_path,
                    bgm_volume=settings.bgmVolume,
                    bgm_offset=settings.bgmOffset,
                    bgm_loop=settings.bgmLoop,
                    voice_boost=settings.voiceBoost,
                    src_size=(
                        int(info.get("width") or 0),
                        int(info.get("height") or 0),
                    ),
                    face_keyframes=(
                        plan.get("points")
                        or edit_service.face_keyframes(faces, width=state.width, height=state.height)
                    ),
                    whole_spans=whole_spans,
                    facecam_face=edit_service.facecam_face(faces),
                    streamer_preset=edit_service.effective_streamer(settings),
                    facecam_position=settings.facecamPosition,
                    hook_text=hook,
                    hook_position=settings.hookPosition,
                    hook_duration=settings.hookDuration,
                    hook_size=settings.hookSize,
                    hook_case=settings.hookCase,
                    on_progress=on_progress,
                    cancel=batch.cancel,
                )
            except Exception as exc:  # noqa: BLE001 - report per clip, keep the batch going
                logger.warning("clip %s failed: %s", index, exc)
                failures += 1
                failed = _update_clip(batch, index, status="error", percent=0.0, error=str(exc)[:400])
                batch.emit("clip", failed.model_dump())
                try:
                    project_store.record_export(
                        project_id,
                        ProjectExport(
                            file=file_name,
                            status="failed",
                            clip_index=index,
                            title=clip.title,
                            batch_id=state.batch_id,
                        ),
                    )
                except Exception:  # noqa: BLE001
                    pass
                continue

            if merged:
                rendered.append((out_path, duration))
                done = _update_clip(
                    batch, index, status="completed", percent=100.0, size_bytes=size, error=None
                )
            else:
                done = _update_clip(
                    batch,
                    index,
                    status="completed",
                    percent=100.0,
                    file=file_name,
                    download_url=f"/api/download-rendered/{file_name}?project={project_id}",
                    size_bytes=size,
                    error=None,
                )
            batch.emit("clip", done.model_dump())

            if not merged:
                try:
                    project_store.record_export(
                        project_id,
                        ProjectExport(
                            file=file_name,
                            size_bytes=size,
                            status="completed",
                            clip_index=index,
                            title=clip.title,
                            batch_id=state.batch_id,
                            width=state.width,
                            height=state.height,
                            duration=round(duration, 2),
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("could not record export for %s: %s", project_id, exc)

        # -- merged compilation -------------------------------------------------
        if merged and rendered:
            batch.emit("stage", {"label": "Merging clips into one file…", "percent": 0.0})
            title = settings.compilationTitle or f"{project_name} · compilation"
            stem = _slug(settings.compilationTitle or f"{project_name} compilation", limit=48)
            file_name = f"{stem}.mp4"
            out_path = out_dir / file_name
            try:
                size = video_engine.concat_clips(
                    [part for part, _ in rendered], out_path, cancel=batch.cancel
                )
            except Exception as exc:  # noqa: BLE001 - whole batch fails, parts stay
                logger.warning("merge failed for %s: %s", state.batch_id, exc)
                _set_batch(batch, status="failed", error=f"Could not merge the clips: {str(exc)[:300]}")
                batch.finished_at = time.time()
                batch.emit("error", {"message": str(exc)[:400], "batch": batch.snapshot()})
                return

            total_duration = sum(duration for _, duration in rendered)
            with batch.lock:
                batch.state.merged = True
                batch.state.output_file = file_name
                batch.state.output_url = (
                    f"/api/download-rendered/{file_name}?project={project_id}"
                )
                batch.state.updated_at = utc_now()
            batch.emit("stage", {"label": "Compilation ready", "percent": 100.0})

            try:
                project_store.record_export(
                    project_id,
                    ProjectExport(
                        file=file_name,
                        size_bytes=size,
                        status="completed",
                        title=title[:120],
                        batch_id=state.batch_id,
                        width=state.width,
                        height=state.height,
                        duration=round(total_duration, 2),
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not record merged export for %s: %s", project_id, exc)

        if merged:
            shutil.rmtree(parts_dir, ignore_errors=True)

        succeeded = len(batch.indices) - failures
        if failures and not succeeded:
            _set_batch(batch, status="failed", error="Every clip in this batch failed.")
        else:
            _set_batch(
                batch,
                status="completed",
                error=(f"{failures} clip(s) failed." if failures else None),
            )
        with batch.lock:
            has_output = batch.state.merged and bool(batch.state.output_file)
            batch.state.zip_url = (
                f"/api/download-batch-zip/{state.batch_id}" if (succeeded or has_output) else None
            )
        batch.finished_at = time.time()
        batch.emit("complete", batch.snapshot())
        logger.info("batch %s done (%d ok, %d failed)", state.batch_id, succeeded, failures)

    except Exception as exc:  # noqa: BLE001 - whole batch failed (download, probe, …)
        logger.exception("batch %s failed", state.batch_id)
        with batch.lock:
            for clip in batch.state.clips:
                if clip.status in {"pending", "rendering"}:
                    clip.status = "error"
                    clip.error = str(exc)[:400]
            batch.state.status = "failed"
            batch.state.error = str(exc)[:400]
            batch.state.updated_at = utc_now()
        batch.finished_at = time.time()
        batch.emit("error", {"message": str(exc)[:400], "batch": batch.snapshot()})


# --- files -------------------------------------------------------------------


def clip_path(batch_id: str, file_name: str) -> Path:
    """Resolve a rendered file, refusing anything outside the exports tree."""
    batch = _BATCHES.get(batch_id) if batch_id else None
    project_id = batch.state.project_id if batch else None
    if project_id is None:
        raise RenderInvalid("Unknown render batch.")
    return safe_export_path(project_id, file_name)


def safe_export_path(project_id: str, file_name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", file_name or ""):
        raise RenderInvalid("Invalid file name.")
    if not re.fullmatch(r"prj_\d{8}_[a-z0-9]{4,12}", project_id or ""):
        raise RenderInvalid("Invalid project id.")
    path = (config.EXPORTS_DIR / project_id / file_name).resolve()
    root = (config.EXPORTS_DIR / project_id).resolve()
    if root not in path.parents or not path.is_file():
        raise RenderInvalid("Rendered file not found.")
    return path


def zip_path(batch_id: str) -> Path:
    batch = _BATCHES.get(batch_id)
    if batch is None:
        raise RenderInvalid("Unknown render batch.")
    with batch.lock:
        if batch.state.merged and batch.state.output_file:
            files = [batch.state.output_file]
        else:
            files = [
                clip.file
                for clip in batch.state.clips
                if clip.status == "completed" and clip.file
            ]
        project_id = batch.state.project_id
    if not files:
        raise RenderInvalid("This batch has no completed clips yet.")

    out_dir = config.EXPORTS_DIR / project_id
    archive = out_dir / f"clipforge-{batch_id}.zip"
    tmp = archive.with_suffix(".zip.part")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as bundle:
        for name in files:
            path = out_dir / name
            if path.is_file():
                bundle.write(path, arcname=name)
    shutil.move(str(tmp), archive)
    return archive
