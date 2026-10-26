"""Shot list — the single source of truth for what the renderer cuts to.

A *shot* is a time range plus how to frame it. The renderer never invents
framing of its own: it walks this list, builds one crop path per shot, smooths
each path independently, and hard-cuts between them.

Time base
---------
``start``/``end`` are **absolute seconds in the source video**, not frames and
not clip-relative seconds. That way a shot list stays pinned to the footage when
the clip window is trimmed: only the visible part changes, the shots do not
slide. ``plan.py`` converts to clip-relative time for the renderer.

Why a list and not "just the tracker"
-------------------------------------
Stage 6 produced one continuous path. With hard cuts the path is no longer
continuous, and with a manual editor the tracker is only a *suggestion*. Keeping
the list as data (a JSON sidecar per clip) is what lets the user edit shots,
lock them against re-detection, and re-render without re-analysing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable

# Shot kinds. `split_screen` is deliberately absent: the spec marks it
# [Perlu Konfirmasi] and it needs a different compositor, so it is not accepted
# by the model yet (validated away instead of silently rendering wrong).
KINDS = ("follow_speaker", "follow_person", "whole_frame")
# Only hard cuts exist. The field stays so stored projects keep a place for a
# future transition without a schema change.
TRANSITIONS = ("cut",)
ZOOM_ENTRIES = ("normal", "instant")
SOURCES = ("auto", "manual")
MIN_SHOT_SECONDS = 0.2


def _round(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


@dataclass
class Shot:
    """One uninterrupted framing decision."""

    index: int = 1
    start: float = 0.0                      # absolute source seconds
    end: float = 0.0                        # absolute source seconds
    kind: str = "follow_speaker"
    person: str | None = None               # "A"/"B"/"C" for follow_person
    person_id: str | None = None            # "p2" — stable group id, survives renames
    frame_cx: float | None = None          # 0..1 manual crop centre; None = auto
    zoom: str | float = "auto"              # "auto" or a fixed 1.0–1.4 factor
    zoom_entry: str = "normal"              # normal | instant
    transition: str = "cut"
    source: str = "auto"                    # auto | manual
    locked: bool = False                    # manual + locked survives re-detection

    # --- derived ---------------------------------------------------------
    @property
    def duration(self) -> float:
        return max(0.0, float(self.end) - float(self.start))

    @property
    def zoom_value(self) -> float | None:
        """Fixed zoom factor, or ``None`` when the shot zooms dynamically."""
        if isinstance(self.zoom, str):
            return None
        try:
            value = float(self.zoom)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    # --- serialisation ---------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "index": int(self.index),
            "start": _round(self.start),
            "end": _round(self.end),
            "duration": _round(self.duration),
            "kind": self.kind,
            "person": self.person,
            "personId": self.person_id,
            "frameCx": round(float(self.frame_cx), 5) if self.frame_cx is not None else None,
            "zoom": self.zoom if isinstance(self.zoom, str) else _round(float(self.zoom), 3),
            "zoomEntry": self.zoom_entry,
            "transition": self.transition,
            "source": self.source,
            "locked": bool(self.locked),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any], index: int | None = None) -> "Shot":
        kind = str(raw.get("kind") or "follow_speaker").strip().lower()
        if kind not in KINDS:
            kind = "follow_speaker"
        zoom_raw = raw.get("zoom", "auto")
        zoom: str | float
        if isinstance(zoom_raw, (int, float)):
            zoom = float(zoom_raw)
        else:
            text = str(zoom_raw or "auto").strip().lower()
            zoom = text if text in {"auto", ""} else "auto"
            if zoom == "":
                zoom = "auto"
        entry = str(raw.get("zoomEntry") or raw.get("zoom_entry") or "normal").strip().lower()
        source = str(raw.get("source") or "auto").strip().lower()
        transition = str(raw.get("transition") or "cut").strip().lower()
        return cls(
            index=int(index if index is not None else raw.get("index") or 1),
            start=float(raw.get("start") or 0.0),
            end=float(raw.get("end") or 0.0),
            kind=kind,
            person=(str(raw["person"]) if raw.get("person") else None),
            person_id=(str(raw.get("personId") or raw.get("person_id") or "").strip() or None),
            frame_cx=(
                float(raw["frameCx"]) if raw.get("frameCx") is not None
                else float(raw["frame_cx"]) if raw.get("frame_cx") is not None
                else None
            ),
            zoom=zoom,
            zoom_entry=entry if entry in ZOOM_ENTRIES else "normal",
            transition=transition if transition in TRANSITIONS else "cut",
            source=source if source in SOURCES else "auto",
            locked=bool(raw.get("locked")),
        )


def backfill_person_ids(
    shots: Iterable[Shot | dict[str, Any]], id_by_letter: dict[str, str]
) -> list[Shot]:
    """Give shots written before ``personId`` existed a stable id.

    Old sidecars store only the letter (``"person": "B"``); the people payload
    knows which group that letter belongs to. Nothing is cleared or reordered: a
    shot whose letter is unknown keeps ``person_id = None`` and still renders from
    ``person`` exactly as before, so a pre-migration shot list stays valid.
    """
    out: list[Shot] = []
    for raw in shots:
        shot = raw if isinstance(raw, Shot) else Shot.from_dict(dict(raw))
        if not shot.person_id and shot.person:
            mapped = id_by_letter.get(str(shot.person).strip().upper())
            if mapped:
                # Copy instead of mutating: the caller's list may be the stored one.
                shot = replace(shot, person_id=str(mapped))
        out.append(shot)
    return out


def normalize(
    shots: Iterable[Shot | dict[str, Any]],
    start: float,
    end: float,
    *,
    default_kind: str = "follow_speaker",
) -> list[Shot]:
    """Force a usable list: sorted, gapless, non-overlapping, covering [start, end].

    The renderer builds a path per shot, so a gap would mean "no framing at all"
    and an overlap would mean two conflicting paths for the same frames. Both are
    repaired here instead of failing at render time.
    """
    start = float(start)
    end = float(end)
    if end - start < MIN_SHOT_SECONDS:
        end = start + MIN_SHOT_SECONDS

    items: list[Shot] = []
    for raw in shots:
        shot = raw if isinstance(raw, Shot) else Shot.from_dict(dict(raw))
        shot.start = max(start, min(end, float(shot.start)))
        shot.end = max(start, min(end, float(shot.end)))
        if shot.end - shot.start < MIN_SHOT_SECONDS:
            continue
        items.append(shot)

    items.sort(key=lambda item: (item.start, item.end))
    if not items:
        items = [Shot(index=1, start=start, end=end, kind=default_kind)]

    # Snap the ends together: shot n ends where shot n+1 begins (midpoint of any
    # overlap), and the list always covers the whole window.
    items[0].start = start
    for left, right in zip(items, items[1:]):
        if right.start < left.end:
            boundary = (right.start + left.end) / 2.0
            left.end = boundary
            right.start = boundary
    items[-1].end = end
    # Drop anything the snapping collapsed, then re-snap.
    items = [item for item in items if item.end - item.start >= MIN_SHOT_SECONDS]
    if not items:
        return [Shot(index=1, start=start, end=end, kind=default_kind)]
    items[0].start = start
    for position in range(len(items) - 1):
        items[position].end = items[position + 1].start
    items[-1].end = end

    for position, item in enumerate(items, start=1):
        item.index = position
        if item.kind not in KINDS:
            item.kind = default_kind
    return items


def summary(shots: list[Shot]) -> dict[str, Any]:
    """Small stats block for the API/debug output."""
    counts: dict[str, int] = {}
    for shot in shots:
        counts[shot.kind] = counts.get(shot.kind, 0) + 1
    total = sum(shot.duration for shot in shots)
    return {
        "shots": len(shots),
        "cuts": max(0, len(shots) - 1),
        "kinds": counts,
        "manual": sum(1 for shot in shots if shot.source == "manual"),
        "locked": sum(1 for shot in shots if shot.locked),
        "duration": _round(total),
        "avg_shot_seconds": _round(total / len(shots)) if shots else 0.0,
        "min_shot_seconds": _round(min((shot.duration for shot in shots), default=0.0)),
    }
