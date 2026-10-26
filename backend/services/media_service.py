"""Source-video cache.

Analysis works inside `temp/jobs/<job>/` and the cleanup worker eventually
deletes that folder, so a saved project could not be re-rendered later. This
module keeps a copy of every analysed source in `data/media/<key>.<ext>` and
re-downloads it on demand when the copy is missing.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Callable

from backend import config
from backend.services import download_service

logger = logging.getLogger("clipforge.media")

VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".flv", ".ts"}
_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class MediaUnavailable(RuntimeError):
    """The project has no local copy and no URL to fetch it from."""


def _lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def source_key(project: dict[str, Any]) -> str:
    """Stable cache key: video id, else URL hash, else the project id."""
    source = project.get("source") or {}
    video_id = str(source.get("video_id") or "").strip()
    if video_id:
        cleaned = _SAFE_RE.sub("", video_id)[:48]
        if cleaned:
            return cleaned
    url = str(source.get("url") or "").strip()
    if url:
        return "url_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return str(project.get("id") or "unknown")


def cached_path(key: str) -> Path | None:
    for candidate in config.MEDIA_DIR.glob(f"{key}.*"):
        if candidate.suffix.lower() in VIDEO_SUFFIXES and candidate.stat().st_size > 0:
            return candidate
    return None


def store(local_path: str | Path, key: str) -> Path | None:
    """Copy an analysed source into the media cache (best effort)."""
    source = Path(local_path)
    if not source.is_file():
        return None
    existing = cached_path(key)
    if existing is not None:
        return existing

    suffix = source.suffix.lower()
    if suffix not in VIDEO_SUFFIXES:
        suffix = ".mp4"
    target = config.MEDIA_DIR / f"{key}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)

    with _lock(key):
        if target.exists():
            return target
        try:
            try:
                import os

                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)
        except OSError as exc:
            logger.warning("could not cache %s: %s", source.name, exc)
            return None
    logger.info("cached source %s (%.1f MB)", target.name, target.stat().st_size / 1e6)
    return target


def ensure_source(
    project: dict[str, Any],
    *,
    on_progress: Callable[[float, str], None] | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """Return a local video file for `project`, downloading it if necessary."""
    key = source_key(project)

    existing = cached_path(key)
    if existing is not None:
        return existing

    source = project.get("source") or {}
    local_path = source.get("local_path")
    if local_path:
        stored = store(local_path, key)
        if stored is not None:
            return stored

    url = str(source.get("url") or "").strip()
    if not url:
        raise MediaUnavailable(
            "The source video is no longer on disk. Re-run the analysis for this "
            "project so ClipForge can cache it again."
        )

    with _lock(key):
        existing = cached_path(key)
        if existing is not None:
            return existing

        work_dir = config.TEMP_DIR / "media" / key
        shutil.rmtree(work_dir, ignore_errors=True)
        work_dir.mkdir(parents=True, exist_ok=True)
        logger.info("re-downloading source for %s", project.get("id"))

        media = download_service.download(
            url,
            work_dir,
            on_progress=(lambda pct, label: on_progress(pct / 100.0, label)) if on_progress else None,
            cancel=cancel,
        )
        downloaded = Path(str(media.get("local_path") or ""))
        if not downloaded.is_file():
            raise MediaUnavailable("The source video could not be downloaded.")

        suffix = downloaded.suffix.lower() if downloaded.suffix.lower() in VIDEO_SUFFIXES else ".mp4"
        target = config.MEDIA_DIR / f"{key}{suffix}"
        try:
            shutil.move(str(downloaded), target)
        except OSError:
            shutil.copy2(downloaded, target)
        shutil.rmtree(work_dir, ignore_errors=True)
        return target
