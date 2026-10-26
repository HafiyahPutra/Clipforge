"""System helpers: temp storage accounting and cleanup (foundation).

The background cleanup worker is wired in `main.py` but disabled by default in
Phase 0 — the functions below are already safe to call manually.
"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from backend import config

_worker_started = threading.Event()
_stop_worker = threading.Event()


def _directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def storage_info() -> dict[str, object]:
    """Summarise the temp directory usage against the configured quota."""
    total_bytes = _directory_size(config.TEMP_DIR)
    quota_bytes = int(config.TEMP_STORAGE_QUOTA_GB * 1024**3)
    return {
        "path": str(config.TEMP_DIR),
        "files": sum(1 for item in config.TEMP_DIR.rglob("*") if item.is_file()),
        "size_bytes": total_bytes,
        "quota_bytes": quota_bytes,
        "usage_percent": round((total_bytes / quota_bytes) * 100, 2) if quota_bytes else 0.0,
        "max_age_seconds": config.TEMP_MAX_AGE_SECONDS,
    }


def cleanup_expired(max_age_seconds: int | None = None) -> dict[str, object]:
    """Delete temp entries older than the TTL. Never touches project data."""
    ttl = max_age_seconds or config.TEMP_MAX_AGE_SECONDS
    cutoff = time.time() - ttl
    removed = 0
    for item in config.TEMP_DIR.iterdir():
        try:
            if item.name.startswith("."):
                continue
            if item.stat().st_mtime >= cutoff:
                continue
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    if removed:
        config.logger.info("Temp cleanup removed %s expired entrie(s)", removed)
    return {"removed": removed, "ttl_seconds": ttl}


def start_cleanup_worker() -> None:
    """Start a daemon thread that prunes expired temp files on an interval."""
    if _worker_started.is_set():
        return
    _worker_started.set()

    def _loop() -> None:
        interval = max(30, config.CLEANUP_INTERVAL_SECONDS)
        while not _stop_worker.wait(interval):
            try:
                cleanup_expired()
            except Exception:  # noqa: BLE001 - a failing sweep must not kill the thread
                config.logger.exception("Temp cleanup sweep failed")

    thread = threading.Thread(target=_loop, name="clipforge-temp-cleanup", daemon=True)
    thread.start()
    config.logger.info(
        "Temp cleanup worker started (TTL=%ss, interval=%ss)",
        config.TEMP_MAX_AGE_SECONDS,
        config.CLEANUP_INTERVAL_SECONDS,
    )


def stop_cleanup_worker() -> None:
    """Signal the cleanup thread to exit (called on application shutdown)."""
    _stop_worker.set()
