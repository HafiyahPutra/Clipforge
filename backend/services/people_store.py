"""People sidecars — one JSON per (project, clip), like the shot store.

Mirrors :mod:`shot_store` on purpose: same lock, same atomic write, same
``past``/``future`` undo stacks, so a user who learned one understands the
other. The only difference is that the *body* is a people ``state`` (groups +
expected + revision) instead of a shot list, and the derived view
(presence, speech, warnings) is recomputed on read rather than stored — it
is a pure function of the cached analysis series and the state.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend import config
from backend.services import people as people_mod

_LOCK = threading.Lock()
_ID_RE = re.compile(r"^prj_\d{8}_[a-z0-9]{4,12}$")
VERSION = 1
HISTORY_MAX = 50


class PeopleStoreError(RuntimeError):
    """Bad ids, or an unreadable sidecar."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _path(project_id: str, clip_index: int) -> Path:
    if not _ID_RE.match(project_id or ""):
        raise PeopleStoreError(f"Invalid project id: {project_id!r}")
    try:
        index = int(clip_index)
    except (TypeError, ValueError) as exc:
        raise PeopleStoreError(f"Invalid clip index: {clip_index!r}") from exc
    if index < 0 or index > 999:
        raise PeopleStoreError(f"Invalid clip index: {clip_index!r}")
    return config.PEOPLE_DIR / f"{project_id}_{index}.json"


def load(project_id: str, clip_index: int) -> dict[str, Any] | None:
    path = _path(project_id, clip_index)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PeopleStoreError(
            f"People state for {project_id}/{clip_index} is unreadable: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise PeopleStoreError(f"People state for {project_id}/{clip_index} is not an object.")
    return raw


def load_state(project_id: str, clip_index: int) -> dict[str, Any] | None:
    payload = load(project_id, clip_index)
    if payload is None:
        return None
    state = payload.get("state")
    return state if isinstance(state, dict) else None


def save(
    project_id: str,
    clip_index: int,
    state: dict[str, Any],
    *,
    window: tuple[float, float] | dict[str, float],
    note: str | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a people state, pushing the prior one onto the undo stack."""
    if isinstance(window, dict):
        start = float(window.get("start") or 0.0)
        end = float(window.get("end") or 0.0)
    else:
        start, end = float(window[0]), float(window[1])

    path = _path(project_id, clip_index)
    existing = load(project_id, clip_index) or {}
    past = [dict(item) for item in existing.get("past") or []]
    before = previous if previous is not None else existing.get("state")
    if before is not None and isinstance(before, dict):
        if before != state:
            past = (past + [dict(before)])[-HISTORY_MAX:]

    payload = {
        "version": VERSION,
        "projectId": project_id,
        "clipIndex": int(clip_index),
        "updatedAt": _now(),
        "window": {"start": round(start, 3), "end": round(end, 3)},
        "state": state,
        "summary": people_mod.summary(state.get("groups") or []),
        "note": note,
        "past": past,
        "future": [],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = path.with_suffix(".json.tmp")
    with _LOCK:
        config.PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    return payload


def _rewind(project_id: str, clip_index: int, *, direction: str) -> dict[str, Any] | None:
    path = _path(project_id, clip_index)
    if not path.is_file():
        return None
    with _LOCK:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PeopleStoreError(
                f"People state for {project_id}/{clip_index} is unreadable: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            return None
        current = payload.get("state")
        if not isinstance(current, dict):
            return None
        past = [dict(item) for item in payload.get("past") or []]
        future = [dict(item) for item in payload.get("future") or []]
        if direction == "undo":
            if not past:
                return None
            target = past.pop()
            future = ([dict(current)] + future)[:HISTORY_MAX]
        else:
            if not future:
                return None
            target = future.pop(0)
            past = (past + [dict(current)])[-HISTORY_MAX:]
        payload.update(
            {
                "updatedAt": _now(),
                "state": target,
                "summary": people_mod.summary(target.get("groups") or []),
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
    return _rewind(project_id, clip_index, direction="undo")


def redo(project_id: str, clip_index: int) -> dict[str, Any] | None:
    return _rewind(project_id, clip_index, direction="redo")


def delete(project_id: str, clip_index: int) -> bool:
    path = _path(project_id, clip_index)
    with _LOCK:
        if path.is_file():
            path.unlink()
            return True
    return False


def thumb_path(project_id: str, clip_index: int, person_id: str) -> Path:
    return config.PEOPLE_THUMBS_DIR / f"{project_id}_{clip_index}_{person_id}.jpg"


def revision(project_id: str, clip_index: int) -> int:
    """Correction counter — part of every cache key that depends on people."""
    try:
        state = load_state(project_id, clip_index)
    except PeopleStoreError:
        return 0
    return int((state or {}).get("revision") or 0)
