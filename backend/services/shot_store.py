"""Shot lists on disk — one JSON sidecar per (project, clip). No database.

Why a file per clip instead of a field on the project JSON
----------------------------------------------------------
A shot list is *derived* data the user then edits. Keeping it beside the face
cache means:

* the project file stays small and is never rewritten while the user drags a
  boundary (a 200-shot list would make every drag rewrite the whole project);
* deleting the face cache does not lose the edits, and vice versa;
* re-detection can compare "what is stored" against "what a fresh draft says"
  without touching project state.

The file is written atomically (temp + ``os.replace``) under a process lock, the
same pattern the project store uses, so a crash mid-write can never leave a
half-parsed list behind.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from backend import config
from backend.services.shots import model as shot_model

_LOCK = threading.Lock()
_ID_RE = re.compile(r"^prj_\d{8}_[a-z0-9]{4,12}$")
VERSION = 1
# Undo/redo lives *beside* the list, in the same sidecar: `past` is a stack of
# previous shot lists, `future` the stack undo has already walked back over.
# Both are plain lists of shot dicts, so the file stays readable by hand and an
# old sidecar written before this feature simply loads with empty stacks.
HISTORY_MAX = 50


class ShotStoreError(RuntimeError):
    """Unusable project id / clip index, or an unreadable sidecar."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path(project_id: str, clip_index: int) -> Path:
    if not _ID_RE.match(project_id or ""):
        raise ShotStoreError(f"Invalid project id: {project_id!r}")
    try:
        index = int(clip_index)
    except (TypeError, ValueError) as exc:
        raise ShotStoreError(f"Invalid clip index: {clip_index!r}") from exc
    if index < 0 or index > 999:
        raise ShotStoreError(f"Invalid clip index: {clip_index!r}")
    return config.SHOTS_DIR / f"{project_id}_{index}.json"


def load(project_id: str, clip_index: int) -> dict[str, Any] | None:
    """Stored shot list, or ``None`` when the clip has never been edited."""
    path = _path(project_id, clip_index)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShotStoreError(f"Shot list for {project_id}/{clip_index} is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise ShotStoreError(f"Shot list for {project_id}/{clip_index} is not an object.")
    shots = raw.get("shots")
    if not isinstance(shots, list):
        return None
    return raw


def save(
    project_id: str,
    clip_index: int,
    shots: Iterable[Any],
    *,
    window: tuple[float, float] | dict[str, float],
    note: str | None = None,
    previous: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Normalise, then persist one shot list. Returns the stored payload.

    ``previous`` is the list the caller edited *from*. Passing it makes the edit
    undoable even when the clip had no sidecar yet (the very first edit of a
    draft): the draft becomes the first entry of ``past``. When omitted, the
    list currently on disk is used, which is what plain callers want.
    """
    if isinstance(window, dict):
        start = float(window.get("start") or 0.0)
        end = float(window.get("end") or 0.0)
    else:
        start, end = float(window[0]), float(window[1])
    items = shot_model.normalize(shots, start, end)
    path = _path(project_id, clip_index)

    existing = load(project_id, clip_index) or {}
    past = [list(item) for item in existing.get("past") or []]
    before = previous if previous is not None else existing.get("shots")
    if before is not None:
        snapshot = [shot.to_dict() for shot in shot_model.normalize(before, start, end)]
        if snapshot and snapshot != [shot.to_dict() for shot in items]:
            past = (past + [snapshot])[-HISTORY_MAX:]

    payload = {
        "version": VERSION,
        "projectId": project_id,
        "clipIndex": int(clip_index),
        "updatedAt": _now(),
        "window": {"start": round(start, 3), "end": round(end, 3)},
        "shots": [shot.to_dict() for shot in items],
        "summary": shot_model.summary(items),
        "note": note,
        "past": past,
        "future": [],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(".json.tmp")
    with _LOCK:
        config.SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    return payload


def _rewind(project_id: str, clip_index: int, *, direction: str) -> dict[str, Any] | None:
    """Move one step through the history. ``None`` when there is nothing to move to."""
    path = _path(project_id, clip_index)
    if not path.is_file():
        return None
    with _LOCK:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ShotStoreError(f"Shot list for {project_id}/{clip_index} is unreadable: {exc}") from exc
        if not isinstance(payload, dict):
            return None
        current = payload.get("shots")
        if not isinstance(current, list):
            return None
        past = [list(item) for item in payload.get("past") or []]
        future = [list(item) for item in payload.get("future") or []]
        if direction == "undo":
            if not past:
                return None
            target = past.pop()
            future = ([current] + future)[:HISTORY_MAX]
        else:
            if not future:
                return None
            target = future.pop(0)
            past = (past + [current])[-HISTORY_MAX:]
        window = payload.get("window") or {}
        items = shot_model.normalize(
            target, float(window.get("start") or 0.0), float(window.get("end") or 0.0)
        )
        payload.update(
            {
                "updatedAt": _now(),
                "shots": [shot.to_dict() for shot in items],
                "summary": shot_model.summary(items),
                "note": direction,
                "past": past,
                "future": future,
            }
        )
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    return payload


def undo(project_id: str, clip_index: int) -> dict[str, Any] | None:
    """Step back one edit. Returns the new payload, or ``None`` at the start."""
    return _rewind(project_id, clip_index, direction="undo")


def redo(project_id: str, clip_index: int) -> dict[str, Any] | None:
    """Step forward again after an undo. ``None`` when there is nothing to redo."""
    return _rewind(project_id, clip_index, direction="redo")


def delete(project_id: str, clip_index: int) -> bool:
    path = _path(project_id, clip_index)
    with _LOCK:
        if path.is_file():
            path.unlink()
            return True
    return False


def counts(project_id: str) -> dict[int, int]:
    """``{clip_index: shot_count}`` for one project — used by the UI badges."""
    prefix = f"{project_id}_"
    found: dict[int, int] = {}
    if not config.SHOTS_DIR.is_dir():
        return found
    for path in config.SHOTS_DIR.glob(f"{prefix}*.json"):
        stem = path.stem[len(prefix):]
        if not stem.isdigit():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        shots = raw.get("shots") if isinstance(raw, dict) else None
        if isinstance(shots, list):
            found[int(stem)] = len(shots)
    return found
