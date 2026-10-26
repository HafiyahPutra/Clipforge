"""Shot-list editor regression tests (Phase 7, stage 3).

Covers the five behaviours the spec asks for, without touching a video:

1. retarget a shot to ``whole_frame`` by hand
2. drag a boundary
3. split and merge
4. re-detect keeps manual shots exactly where they are
5. the stored list round-trips through disk and changes the preview cache key

Plus the degenerate cases (split too close to a boundary, merging the last shot,
boundaries that would collapse a shot) because those are what a UI hits first.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services import shot_edit, shot_store  # noqa: E402
from backend.services.shots import model as shot_model  # noqa: E402

START, END = 0.0, 20.0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def draft() -> list[shot_model.Shot]:
    return shot_model.normalize(
        [
            {"start": 0.0, "end": 6.0},
            {"start": 6.0, "end": 14.0},
            {"start": 14.0, "end": 20.0},
        ],
        START,
        END,
    )


def spans(shots) -> list[tuple[float, float]]:
    return [(round(s.start, 3), round(s.end, 3)) for s in shots]


def gapless(shots) -> bool:
    if not shots:
        return False
    if abs(shots[0].start - START) > 1e-6 or abs(shots[-1].end - END) > 1e-6:
        return False
    return all(abs(a.end - b.start) < 1e-6 for a, b in zip(shots, shots[1:]))


def case_retarget() -> None:
    print("case: retarget to whole_frame")
    shots = shot_edit.update(draft(), 2, {"kind": "whole_frame"}, start=START, end=END)
    check("kind applied", shots[1].kind == "whole_frame")
    check("marked manual", shots[1].source == "manual")
    check("neighbours untouched", shots[0].source == "auto" and shots[2].source == "auto")
    check("gapless", gapless(shots), str(spans(shots)))

    back = shot_edit.update(shots, 2, {"kind": "follow_speaker"}, start=START, end=END)
    check("can go back to follow_speaker", back[1].kind == "follow_speaker")

    zoomed = shot_edit.update(draft(), 1, {"zoom": 1.25}, start=START, end=END)
    check("fixed zoom stored", abs((zoomed[0].zoom_value or 0) - 1.25) < 1e-6)
    auto = shot_edit.update(zoomed, 1, {"zoom": "auto"}, start=START, end=END)
    check("zoom back to auto", auto[0].zoom_value is None)

    locked = shot_edit.update(draft(), 1, {"locked": True}, start=START, end=END)
    check("lock keeps auto source", locked[0].locked and locked[0].source == "auto")

    # Framing: the slider writes a centre, marks the shot manual and locks it so
    # re-detect leaves it alone. "Restore auto" has to reverse all three.
    framed = shot_edit.update(draft(), 1, {"frameCx": 0.30}, start=START, end=END)
    check("frameCx stored", abs((framed[0].frame_cx or 0) - 0.30) < 1e-6, str(framed[0].frame_cx))
    check("framing marks manual", framed[0].source == "manual")
    check("framing locks the shot", framed[0].locked)
    restored = shot_edit.update(framed, 1, {"frameCx": None}, start=START, end=END)
    check("restore clears frameCx", restored[0].frame_cx is None)
    check("restore clears manual flag", restored[0].source == "auto", restored[0].source)
    check("restore releases the slider lock", not restored[0].locked)

    # A lock the user toggled by hand is a separate decision: it must survive a
    # restore, and an explicit `locked` in the same patch wins over the slider.
    hand_locked = shot_edit.update(draft(), 1, {"locked": True}, start=START, end=END)
    hand_locked = shot_edit.update(hand_locked, 1, {"frameCx": 0.30}, start=START, end=END)
    keep = shot_edit.update(hand_locked, 1, {"frameCx": None, "locked": True}, start=START, end=END)
    check("explicit lock survives restore", keep[0].locked, str(keep[0].locked))

    bad = False
    try:
        shot_edit.update(draft(), 9, {"kind": "whole_frame"}, start=START, end=END)
    except shot_edit.ShotEditError:
        bad = True
    check("unknown shot index rejected", bad)


def case_move() -> None:
    print("case: move a boundary")
    shots = shot_edit.move_boundary(draft(), 1, 4.25, start=START, end=END)
    check("boundary moved", abs(shots[0].end - 4.25) < 1e-6, str(spans(shots)))
    check("both sides manual", shots[0].source == "manual" and shots[1].source == "manual")
    check("gapless", gapless(shots), str(spans(shots)))
    check("durations preserved total", abs(sum(s.duration for s in shots) - 20.0) < 1e-6)

    # Dragging past the neighbour's end must clamp, not collapse the shot.
    far = shot_edit.move_boundary(draft(), 1, 999.0, start=START, end=END)
    check("clamped at the right edge", far[1].end - far[1].start >= shot_model.MIN_SHOT_SECONDS - 1e-9)
    check("gapless after clamp", gapless(far), str(spans(far)))

    # Snapping: 4.2345 lands on 4.23.
    snapped = shot_edit.move_boundary(draft(), 1, 4.2345, start=START, end=END)
    check("snaps to 10 ms", abs(snapped[0].end - 4.23) < 1e-6, str(snapped[0].end))


def case_split_merge() -> None:
    print("case: split and merge")
    shots = shot_edit.split(draft(), 3.0, start=START, end=END)
    check("one more shot", len(shots) == 4)
    check("split point exact", abs(shots[0].end - 3.0) < 1e-6 and abs(shots[1].start - 3.0) < 1e-6)
    check("halves are manual", shots[0].source == "manual" and shots[1].source == "manual")
    check("framing inherited", shots[0].kind == shots[1].kind == "follow_speaker")
    check("gapless", gapless(shots), str(spans(shots)))

    merged = shot_edit.merge(shots, 1, start=START, end=END)
    check("merge restores the span", abs(merged[0].start - START) < 1e-6 and abs(merged[0].end - 6.0) < 1e-6)
    check("merge is manual", merged[0].source == "manual")

    # Merging across a kind change keeps the left framing and says so.
    mixed = shot_edit.update(draft(), 2, {"kind": "whole_frame"}, start=START, end=END)
    merged_mixed = shot_edit.merge(mixed, 1, start=START, end=END)
    notes = shot_edit.merge_warnings(mixed[0], mixed[1])
    check("left framing wins", merged_mixed[0].kind == "follow_speaker")
    check("kind change reported", any("whole_frame" in note for note in notes), str(notes))

    for label, fn in (
        ("split too close to a boundary", lambda: shot_edit.split(draft(), 0.05, start=START, end=END)),
        ("split outside every shot", lambda: shot_edit.split(draft(), 25.0, start=START, end=END)),
        ("merge the last shot", lambda: shot_edit.merge(draft(), 3, start=START, end=END)),
    ):
        failed = False
        try:
            fn()
        except shot_edit.ShotEditError:
            failed = True
        check(f"{label} rejected", failed)


def case_redetect() -> None:
    print("case: re-detect keeps manual shots")
    edited = draft()
    edited = shot_edit.update(edited, 1, {"kind": "whole_frame"}, start=START, end=END)
    edited = shot_edit.split(edited, 3.0, start=START, end=END)
    edited = shot_edit.update(edited, 4, {"locked": True}, start=START, end=END)
    manual_spans = [
        (round(s.start, 3), round(s.end, 3), s.kind) for s in edited if s.source == "manual" or s.locked
    ]

    # A fresh draft with completely different cuts.
    fresh = shot_model.normalize(
        [
            {"start": 0.0, "end": 4.0},
            {"start": 4.0, "end": 9.5},
            {"start": 9.5, "end": 15.0},
            {"start": 15.0, "end": 20.0},
        ],
        START,
        END,
    )
    result = shot_edit.redetect(edited, fresh, start=START, end=END)
    kept = [(round(s.start, 3), round(s.end, 3), s.kind) for s in result if s.source == "manual" or s.locked]
    check("manual shots byte-identical", kept == manual_spans, f"{kept} != {manual_spans}")
    check("gapless after merge", gapless(result), str(spans(result)))
    check("auto area took the new cuts", any(abs(s.end - 9.5) < 1e-6 for s in result), str(spans(result)))

    # Nothing manual → the fresh draft wins outright.
    plain = shot_edit.redetect(draft(), fresh, start=START, end=END)
    check("no manual shots → pure draft", spans(plain) == spans(fresh), str(spans(plain)))

    reset = shot_edit.reset(fresh, start=START, end=END)
    check("reset == draft", spans(reset) == spans(fresh))


def case_store() -> None:
    print("case: persistence round-trip")
    project_id = "prj_20260101_abc123"
    shots = shot_edit.update(draft(), 2, {"kind": "whole_frame"}, start=START, end=END)
    stored = shot_store.save(project_id, 0, shots, window=(START, END), note="test")
    check("saved summary", stored["summary"]["shots"] == 3 and stored["summary"]["manual"] == 1)

    loaded = shot_store.load(project_id, 0)
    check("loads back", loaded is not None and len(loaded["shots"]) == 3)
    check("kind survived", loaded["shots"][1]["kind"] == "whole_frame")
    check("window survived", loaded["window"] == {"start": 0.0, "end": 20.0})

    from backend.services import edit_service

    rev_a = edit_service.shots_rev(project_id, 0)
    shot_store.save(project_id, 0, shot_edit.split(shots, 10.0, start=START, end=END), window=(START, END))
    rev_b = edit_service.shots_rev(project_id, 0)
    check("cache key changes with the list", rev_a != rev_b, f"{rev_a} == {rev_b}")
    check("missing clip has no rev", edit_service.shots_rev(project_id, 7) == "none")

    check("delete reports removal", shot_store.delete(project_id, 0) is True)
    check("delete is idempotent", shot_store.delete(project_id, 0) is False)
    check("load after delete is None", shot_store.load(project_id, 0) is None)
    check("rev after delete is none", edit_service.shots_rev(project_id, 0) == "none")

    rejected = False
    try:
        shot_store.save("../../etc/passwd", 0, shots, window=(START, END))
    except shot_store.ShotStoreError:
        rejected = True
    check("bad project id rejected", rejected)


def case_undo_redo() -> None:
    print("case: server-side undo/redo")
    project_id = "prj_20260101_hist01"
    shot_store.delete(project_id, 0)

    base = draft()
    # First edit of a *draft* is undoable: the draft itself is the first entry.
    one = shot_edit.update(base, 2, {"kind": "whole_frame"}, start=START, end=END)
    stored = shot_store.save(project_id, 0, one, window=(START, END), note="patch", previous=base)
    check("history recorded from the draft", len(stored["past"]) == 1, str(len(stored["past"])))
    check("nothing to redo yet", stored["future"] == [])

    two = shot_edit.split(one, 10.0, start=START, end=END)
    stored = shot_store.save(project_id, 0, two, window=(START, END), note="split", previous=one)
    check("two steps deep", len(stored["past"]) == 2)
    check("split applied", len(stored["shots"]) == 4)

    back = shot_store.undo(project_id, 0)
    check("undo returns a payload", back is not None)
    check("undo undoes the split", len(back["shots"]) == 3, str(len(back["shots"])))
    check("undo restores the whole_frame shot", back["shots"][1]["kind"] == "whole_frame")
    check("undo fills the redo stack", len(back["future"]) == 1)

    again = shot_store.undo(project_id, 0)
    check("undo reaches the draft", again is not None and [s["kind"] for s in again["shots"]] == [s.kind for s in base])
    check("nothing left to undo", shot_store.undo(project_id, 0) is None)

    fwd = shot_store.redo(project_id, 0)
    check("redo returns a payload", fwd is not None)
    check("redo re-applies the patch", fwd["shots"][1]["kind"] == "whole_frame")
    fwd = shot_store.redo(project_id, 0)
    check("redo re-applies the split", len(fwd["shots"]) == 4)
    check("nothing left to redo", shot_store.redo(project_id, 0) is None)

    # A fresh edit after an undo must throw the redo branch away.
    shot_store.undo(project_id, 0)
    branched = shot_edit.update(shot_store.load(project_id, 0)["shots"], 1, {"locked": True}, start=START, end=END)
    shot_store.save(project_id, 0, branched, window=(START, END), note="patch")
    reloaded = shot_store.load(project_id, 0)
    check("new edit clears redo", reloaded["future"] == [])

    # History is capped so a long drag session cannot grow the file forever.
    for i in range(shot_store.HISTORY_MAX + 5):
        shot_store.save(project_id, 0, shot_edit.update(
            shot_store.load(project_id, 0)["shots"], 1, {"zoom": 1.0 + i * 0.001}, start=START, end=END
        ), window=(START, END))
    capped = shot_store.load(project_id, 0)
    check("history is capped", len(capped["past"]) == shot_store.HISTORY_MAX, str(len(capped["past"])))

    shot_store.delete(project_id, 0)
    check("undo on a missing sidecar is None", shot_store.undo(project_id, 0) is None)


def case_normalize_repairs() -> None:
    print("case: normalize repairs bad input")
    messy = [
        {"start": 0.0, "end": 10.0},
        {"start": 8.0, "end": 15.0},   # overlaps the first
        {"start": 16.0, "end": 22.0},  # runs past the window
    ]
    fixed = shot_model.normalize(messy, START, END)
    check("gapless", gapless(fixed), str(spans(fixed)))
    check("no overlap", all(a.end <= b.start + 1e-6 for a, b in zip(fixed, fixed[1:])))
    check("clamped to the window", fixed[-1].end <= END + 1e-6)
    empty = shot_model.normalize([], START, END)
    check("empty list still covers the window", len(empty) == 1 and empty[0].start == START and empty[0].end == END)


def case_person_id() -> None:
    """``personId`` survives a round trip and old lists stay valid (stage 8.3)."""
    print("case: person_id migration")
    # A sidecar written before the field existed: letter only, no personId.
    legacy = [
        {"index": 1, "start": 0.0, "end": 10.0, "kind": "follow_person", "person": "B"},
        {"index": 2, "start": 10.0, "end": 20.0, "kind": "whole_frame"},
    ]
    shots = shot_model.normalize(legacy, START, END)
    check("legacy shot loads without personId", shots[0].person_id is None, str(shots[0].person_id))
    check("legacy letter is kept", shots[0].person == "B", str(shots[0].person))

    filled = shot_model.backfill_person_ids(shots, {"A": "p1", "B": "p2"})
    check("backfill maps the letter to the group id", filled[0].person_id == "p2", str(filled[0].person_id))
    check("backfill leaves whole_frame alone", filled[1].person_id is None)
    check("backfill keeps the letter for old readers", filled[0].person == "B")

    unknown = shot_model.backfill_person_ids(shots, {"A": "p1"})
    check("unknown letter stays unmapped, never guessed", unknown[0].person_id is None)

    again = shot_model.normalize([s.to_dict() for s in filled], START, END)
    check("personId round-trips through to_dict/from_dict",
          again[0].person_id == "p2" and again[0].person == "B", str(again[0].to_dict()))
    check("snake_case person_id is accepted on read",
          shot_model.Shot.from_dict({"start": 0.0, "end": 1.0, "person_id": "p9"}).person_id == "p9")

    patched = shot_edit.update(filled, 2, {"personId": "p3"}, start=START, end=END)
    check("patching personId retargets the shot",
          patched[1].person_id == "p3" and patched[1].kind == "follow_person", str(patched[1]))
    check("patching personId marks it manual", patched[1].source == "manual", patched[1].source)
    cleared = shot_edit.update(patched, 2, {"kind": "whole_frame"}, start=START, end=END)
    check("leaving follow_person clears personId", cleared[1].person_id is None, str(cleared[1].person_id))
    split = shot_edit.split(filled, 5.0, start=START, end=END)
    check("split inherits personId", all(s.person_id == "p2" for s in split[:2]),
          str([s.person_id for s in split]))


def main() -> int:
    print("shot list editor tests")
    for case in (
        case_retarget,
        case_move,
        case_split_merge,
        case_redetect,
        case_store,
        case_undo_redo,
        case_normalize_repairs,
        case_person_id,
    ):
        case()
    print()
    if FAILED:
        print(f"FAILED ({len(FAILED)}): {json.dumps(FAILED)}")
        return 1
    print("all shot-list editor tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
