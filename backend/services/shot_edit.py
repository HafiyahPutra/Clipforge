"""Shot-list editing: split, merge, drag a boundary, retarget, re-detect.

Every function here is pure — it takes a list of shots plus the clip window and
returns a *new*, normalised list. No I/O, no face data, no FFmpeg. That is what
makes the editor testable without a video, and what keeps the renderer honest:
whatever the UI sends is repaired into a gapless list covering the window before
it can reach a filtergraph.

The one rule that matters: **a manual decision survives re-detection.** A shot is
protected when ``source == "manual"`` (the user changed its framing or its
boundary) or ``locked`` (the user pinned it). Re-detection refills only the
unprotected gaps with fresh draft cuts.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable

from backend.services.shots import model as shot_model

MIN = shot_model.MIN_SHOT_SECONDS
# Boundaries snap to 10 ms. Tighter than a frame at 60 fps, so dragging never
# quantises visibly, but it keeps the JSON readable and stops float noise from
# creating 0.2000000001 s shots that normalise would drop.
SNAP = 0.01


class ShotEditError(ValueError):
    """The edit is not applicable to this list (bad index, degenerate result)."""


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _snap(value: float) -> float:
    return round(round(float(value) / SNAP) * SNAP, 3)


def _norm(shots: Iterable[Any], start: float, end: float) -> list[shot_model.Shot]:
    return shot_model.normalize(shots, start, end)


def _manual(shot: shot_model.Shot) -> shot_model.Shot:
    return replace(shot, source="manual")


def split(
    shots: Iterable[Any],
    at: float,
    *,
    start: float,
    end: float,
) -> list[shot_model.Shot]:
    """Cut the shot that contains ``at`` into two at that exact time.

    Both halves inherit the original framing (kind, person, zoom) and become
    manual, so a split is never undone by a later re-detection.
    """
    items = _norm(shots, start, end)
    point = _clamp(_snap(at), start, end)
    for position, shot in enumerate(items):
        if shot.start + MIN <= point <= shot.end - MIN:
            left = replace(shot, end=point, source="manual")
            right = replace(shot, start=point, source="manual")
            items[position:position + 1] = [left, right]
            return _norm(items, start, end)
    raise ShotEditError(
        "Split point is outside the shots or too close to a boundary "
        f"(needs {MIN:.1f}s on both sides)."
    )


def merge(
    shots: Iterable[Any],
    index: int,
    *,
    start: float,
    end: float,
) -> list[shot_model.Shot]:
    """Merge shot ``index`` (1-based) with the one after it.

    The merged shot keeps the *left* framing. When the two sides disagree the
    caller gets that as a warning instead of a silent flip of half the range.
    """
    items = _norm(shots, start, end)
    position = int(index) - 1
    if position < 0 or position >= len(items) - 1:
        raise ShotEditError(f"Cannot merge shot {index}: no shot after it.")
    left, right = items[position], items[position + 1]
    merged = replace(
        left,
        end=right.end,
        source="manual",
        locked=bool(left.locked or right.locked),
    )
    items[position:position + 2] = [merged]
    return _norm(items, start, end)


def merge_warnings(left: shot_model.Shot, right: shot_model.Shot) -> list[str]:
    """What the UI should tell the user after a merge of two different shots."""
    notes: list[str] = []
    if left.kind != right.kind:
        notes.append(f"kept the left framing ({left.kind}) and dropped {right.kind}")
    if str(left.zoom) != str(right.zoom):
        notes.append("kept the left zoom")
    return notes


def move_boundary(
    shots: Iterable[Any],
    index: int,
    at: float,
    *,
    start: float,
    end: float,
) -> list[shot_model.Shot]:
    """Drag the boundary *after* shot ``index`` (1-based) to ``at``.

    Both neighbours become manual: moving a boundary is a decision about two
    shots, and a re-detection that undid half of it would be surprising.
    """
    items = _norm(shots, start, end)
    position = int(index) - 1
    if position < 0 or position >= len(items) - 1:
        raise ShotEditError(f"Cannot move the boundary after shot {index}.")
    left, right = items[position], items[position + 1]
    point = _clamp(_snap(at), left.start + MIN, right.end - MIN)
    if point <= left.start + MIN - 1e-6 or point >= right.end - MIN + 1e-6:
        raise ShotEditError("Both shots must stay at least one frame long.")
    items[position] = replace(left, end=point, source="manual")
    items[position + 1] = replace(right, start=point, source="manual")
    return _norm(items, start, end)


def update(
    shots: Iterable[Any],
    index: int,
    patch: dict[str, Any],
    *,
    start: float,
    end: float,
) -> list[shot_model.Shot]:
    """Retarget one shot: kind, person, zoom, zoom entry, lock.

    Changing framing marks the shot manual (it is now a user decision); toggling
    the lock alone does not, so the user can pin an auto shot and still let the
    draft own its framing.
    """
    items = _norm(shots, start, end)
    position = int(index) - 1
    if position < 0 or position >= len(items):
        raise ShotEditError(f"Shot {index} does not exist.")
    shot = items[position]
    changed = dict(shot.to_dict())
    framing_changed = False

    if "kind" in patch and patch["kind"] is not None:
        kind = str(patch["kind"]).strip().lower()
        if kind not in shot_model.KINDS:
            raise ShotEditError(f"Unknown shot kind: {patch['kind']!r}")
        if kind != shot.kind:
            framing_changed = True
        changed["kind"] = kind
        if kind != "follow_person":
            changed["person"] = None
            changed["personId"] = None
        if kind == "whole_frame":
            changed["frameCx"] = None  # slider disabled on whole_frame

    if "personId" in patch:
        pid = patch["personId"]
        pid = str(pid).strip() if pid else None
        if pid and pid != shot.person_id:
            framing_changed = True
            changed["personId"] = pid
            changed["kind"] = "follow_person"
        elif not pid:
            changed["personId"] = None

    # Tracked separately: restoring auto framing must undo the *slider's* lock,
    # but a lock the user toggled by hand has to survive.
    cleared_manual_cx = False
    if "frameCx" in patch:
        raw_cx = patch["frameCx"]
        if raw_cx is None:
            # Explicit null → clear the manual override, go back to auto.
            if shot.frame_cx is not None:
                framing_changed = True
                cleared_manual_cx = True
            changed["frameCx"] = None
            # Slider-moved framing marks the shot manual and locks it (see the
            # `framing_changed` block below). Restoring auto has to reverse
            # *both*, otherwise the shot stays pinned to a centre the user just
            # asked to forget — and re-detect would keep ignoring it.
            if shot.frame_cx is not None:
                changed["source"] = "auto"
                changed["locked"] = False
        else:
            try:
                cx = float(raw_cx)
            except (TypeError, ValueError) as exc:
                raise ShotEditError(f"Invalid frameCx: {raw_cx!r}") from exc
            cx = round(_clamp(cx, 0.0, 1.0), 5)
            if shot.frame_cx is None or abs(shot.frame_cx - cx) > 1e-6:
                framing_changed = True
            changed["frameCx"] = cx

    if "person" in patch:
        person = patch["person"]
        person = str(person).strip().upper()[:1] if person else None
        if person and person != shot.person:
            framing_changed = True
            changed["person"] = person
            changed["kind"] = "follow_person"
        elif not person:
            changed["person"] = None
            changed["personId"] = None

    if "zoom" in patch and patch["zoom"] is not None:
        zoom = patch["zoom"]
        if isinstance(zoom, str) and zoom.strip().lower() in {"auto", ""}:
            if not isinstance(shot.zoom, str):
                framing_changed = True
            changed["zoom"] = "auto"
        else:
            try:
                value = float(zoom)
            except (TypeError, ValueError) as exc:
                raise ShotEditError(f"Invalid zoom: {zoom!r}") from exc
            value = round(_clamp(value, 1.0, 4.0), 3)
            if shot.zoom_value is None or abs(shot.zoom_value - value) > 1e-6:
                framing_changed = True
            changed["zoom"] = value

    if "zoomEntry" in patch and patch["zoomEntry"] is not None:
        entry = str(patch["zoomEntry"]).strip().lower()
        if entry not in shot_model.ZOOM_ENTRIES:
            raise ShotEditError(f"Unknown zoom entry: {patch['zoomEntry']!r}")
        if entry != shot.zoom_entry:
            framing_changed = True
        changed["zoomEntry"] = entry

    if "locked" in patch and patch["locked"] is not None:
        changed["locked"] = bool(patch["locked"])

    if framing_changed and not cleared_manual_cx:
        changed["source"] = "manual"
        # Slider-moved framing locks the shot so re-detection never overwrites it.
        if "frameCx" in patch and patch.get("frameCx") is not None:
            changed["locked"] = True

    items[position] = shot_model.Shot.from_dict(changed, index=shot.index)
    return _norm(items, start, end)


def _fill(fresh: list[shot_model.Shot], lo: float, hi: float) -> list[shot_model.Shot]:
    """Cover ``[lo, hi]`` with the fresh draft's cuts, clipped to the gap."""
    picked: list[shot_model.Shot] = []
    for shot in fresh:
        if shot.end <= lo + 1e-6 or shot.start >= hi - 1e-6:
            continue
        piece = replace(
            shot,
            start=max(lo, float(shot.start)),
            end=min(hi, float(shot.end)),
            source="auto",
            locked=False,
        )
        if piece.end - piece.start >= MIN:
            picked.append(piece)
    if picked:
        return picked
    # No fresh cut touches this gap: keep the framing that was there before the
    # re-detect rather than inventing one.
    fallback = "follow_speaker"
    for shot in fresh:
        if shot.start <= lo + 1e-6 <= shot.end:
            fallback = shot.kind
            break
    return [shot_model.Shot(start=lo, end=hi, kind=fallback, source="auto")]


def redetect(
    shots: Iterable[Any],
    fresh: Iterable[Any],
    *,
    start: float,
    end: float,
) -> list[shot_model.Shot]:
    """Re-run detection, but keep every manual/locked shot exactly as it is."""
    stored = _norm(shots, start, end)
    draft = _norm(fresh, start, end)
    keep = sorted(
        (shot for shot in stored if shot.source == "manual" or shot.locked),
        key=lambda item: (item.start, item.end),
    )
    if not keep:
        return draft

    out: list[shot_model.Shot] = []
    cursor = float(start)
    for shot in keep:
        lo = max(float(start), float(shot.start))
        if lo > cursor + MIN:
            out.extend(_fill(draft, cursor, lo))
        out.append(replace(shot, start=lo, end=min(float(end), float(shot.end))))
        cursor = max(cursor, min(float(end), float(shot.end)))
    if float(end) > cursor + MIN:
        out.extend(_fill(draft, cursor, float(end)))
    return _norm(out, start, end)


def reset(fresh: Iterable[Any], *, start: float, end: float) -> list[shot_model.Shot]:
    """Throw away manual edits and go back to the pure draft."""
    return _norm(fresh, start, end)
