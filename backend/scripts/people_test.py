"""People-on-screen tests (Phase 8, stage 1).

The panel's value is entirely in the invariants, so that is what is tested here —
no video, no HTTP, no detector:

1. a tracker id becomes a person, with a letter that never moves afterwards
2. "ignore" drops a person from tracking *and* from the effective series
3. "merge" folds two ids into one person without losing either time range
4. "split" cuts one person in two from a time, keeping both halves
5. "expected people" limits the active list without deleting anything
6. a re-analysis that adds an id appends a person; one that loses an id prunes it
7. identity across a gap is *suggested*, never merged silently
8. the sidecar round-trips through disk with the same undo stack as shots

Run: ``python -m backend.scripts.people_test``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend import config  # noqa: E402
from backend.services import people, people_store  # noqa: E402

STEPS = 201  # 20 s at 10 Hz, the analysis grid
FAILED: list[str] = []
PROJECT = "prj_20261019_0001"


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        FAILED.append(name)


def series(
    label: str,
    spans: list[tuple[float, float]],
    *,
    size: float = 0.16,
    x: float = 0.5,
    y: float = 0.45,
) -> dict:
    """One tracker id: valid inside ``spans`` (seconds), a fixed face size/place."""
    times = np.arange(STEPS) / 10.0
    valid = np.zeros(STEPS, dtype=bool)
    for start, end in spans:
        valid |= (times >= start) & (times < end)
    cx = np.full(STEPS, x)
    cy = np.full(STEPS, y)
    w = np.where(valid, size, 0.0)
    h = np.where(valid, size * 1.8, 0.0)
    # Mouth movement only where the face exists: a crude but sufficient "talking".
    jaw = np.where(valid, 0.02 + 0.03 * (np.sin(times * 6.0) > 0.0), 0.0)
    return {
        "id": 0,
        "label": label,
        "cx": [round(float(v), 5) for v in cx],
        "cy": [round(float(v), 5) for v in cy],
        "w": [round(float(v), 5) for v in w],
        "h": [round(float(v), 5) for v in h],
        "jaw": [round(float(v), 4) for v in jaw],
        "valid": valid.astype(np.int8).tolist(),
        "detections": int(valid.sum()),
        "weight": 0.5,
        "coverage": round(float(valid.mean()), 3),
        "best": {"t": 1.0, "cx": 0.5, "cy": 0.45, "w": size, "h": size * 1.8},
    }


def grid() -> np.ndarray:
    return np.arange(STEPS) / 10.0


def main() -> int:
    times = grid()
    raw = [
        series("A", [(0.0, 8.0), (12.0, 20.0)], x=0.30),  # host, briefly leaves
        series("B", [(8.0, 14.0)], x=0.70),                # guest
        series("C", [(16.0, 20.0)], size=0.05, x=0.86),    # small face near the end
    ]

    print("default state")
    state = people.default_state(raw)
    letters = [group["letter"] for group in state["groups"]]
    colors = [group["color"] for group in state["groups"]]
    check("one person per tracker id", len(state["groups"]) == 3, str(len(state["groups"])))
    check("letters are A, B, C", letters == ["A", "B", "C"], str(letters))
    check("colours are distinct", len(set(colors)) == 3, str(colors))
    check("labels start as Person <letter>", state["groups"][0]["label"] == "Person A")
    check("revision starts at 1", state["revision"] == 1)

    print("duplicate tracker ids (one face, two ids)")
    dup_raw = [
        series("A", [(0.0, 12.0)], x=0.30),   # host
        series("B", [(2.0, 8.0)], x=0.70),    # guest, different seat
        series("C", [(11.0, 20.0)], x=0.30),  # same seat as A, overlaps 1 s
    ]
    dup_state = people.default_state(dup_raw)
    check("overlapping same-box ids fold into one person",
          len(dup_state["groups"]) == 2, str(len(dup_state["groups"])))
    check("the folded id is recorded as merged_from",
          "C" in (dup_state["groups"][0].get("merged_from") or []),
          str(dup_state["groups"][0].get("merged_from")))
    dup_view = people.derive(dup_raw, dup_state, times, {"min_face_ratio": 0.04})
    host = dup_view["people"][0]
    check("folded person covers both ids' spans",
          host["ranges"] and host["ranges"][0][1] >= 19.9, str(host["ranges"]))
    check("two people reported, not three", dup_view["detected"] == 2, str(dup_view["detected"]))

    seat_raw = [  # same seat, never together: ambiguous, so never folded silently
        series("A", [(0.0, 5.0)], x=0.30),
        series("B", [(10.0, 20.0)], x=0.30),
    ]
    check("same seat without time overlap is not folded",
          len(people.default_state(seat_raw)["groups"]) == 2)
    near_raw = [  # overlapping but clearly different boxes: two real people
        series("A", [(0.0, 12.0)], x=0.20),
        series("B", [(2.0, 12.0)], x=0.75),
    ]
    check("overlapping ids in different boxes stay separate",
          len(people.default_state(near_raw)["groups"]) == 2)

    print("ignore")
    ignored, _note = people.apply_action(state, "ignore", person=state["groups"][2]["id"])
    effective, labels = people.effective_series(raw, ignored, times)
    check("ignored person leaves the effective series", len(effective) == 2, str(len(effective)))
    check("the remaining letters do not shift", labels == ["A", "B"], str(labels))
    view = people.derive(raw, ignored, times, {"min_face_ratio": 0.04})
    check("panel still lists all three people", view["detected"] == 3, str(view["detected"]))
    check("panel counts two active", view["active"] == 2, str(view["active"]))
    check(
        "ignored person is flagged, not hidden",
        [item["ignored"] for item in view["people"]] == [False, False, True],
    )

    print("merge")
    merged, _note = people.apply_action(
        state, "merge", person=state["groups"][2]["id"], target=state["groups"][0]["id"]
    )
    effective, labels = people.effective_series(raw, merged, times)
    check("merge leaves two people", len(merged["groups"]) == 2, str(len(merged["groups"])))
    check("merge keeps the target letter", labels == ["A", "B"], str(labels))
    host = next(item for item in people.derive(raw, merged, times)["people"] if item["letter"] == "A")
    check("merged person keeps both ids", host["tracks"] == ["A", "C"], str(host["tracks"]))
    check("merged person records where it came from", host["merged_from"] == ["C"], str(host["merged_from"]))
    # A is present 0-8 and 12-20, C is present 16-20: the union is 16 s.
    check("merged presence is the union, not the sum", abs(host["presence_seconds"] - 16.0) < 0.4,
          str(host["presence_seconds"]))

    print("split")
    split, _note = people.apply_action(merged, "split", person=state["groups"][0]["id"], at=10.0)
    check("split adds a person", len(split["groups"]) == 3, str(len(split["groups"])))
    new = split["groups"][-1]
    check("split remembers its parent", new["split_from"] == state["groups"][0]["id"], str(new["split_from"]))
    check("split half starts at the cut", float(new["members"][0]["from"]) == 10.0, str(new["members"]))
    check("split half ends where the parent ended", new["members"][0]["to"] is None)
    check("split gets the next free letter", new["letter"] == "C", new["letter"])
    effective, labels = people.effective_series(raw, split, times)
    check("split keeps every half in the series", len(effective) == 3, str(len(effective)))
    # A is gone between 8 and 12, so the first half covers 0-8 and the second 12-20.
    first = next(item for item in people.derive(raw, split, times)["people"] if item["id"] == state["groups"][0]["id"])
    check("first half stops at the gap", first["presence_seconds"] < 9.0, str(first["presence_seconds"]))

    print("expected people")
    expected, _note = people.apply_action(state, "expected", expected=2)
    effective, labels = people.effective_series(raw, expected, times)
    check("expected count is stored", expected["expected"] == 2, str(expected["expected"]))
    check("expected count limits the active list", len(effective) == 2, str(len(effective)))
    check("expected count keeps the most present people", labels == ["A", "B"], str(labels))
    view = people.derive(raw, expected, times, {"min_face_ratio": 0.04})
    check("excluded people are listed, not deleted", view["detected"] == 3, str(view["detected"]))
    check("one person is marked excluded", sum(1 for item in view["people"] if item["excluded"]) == 1)
    cleared, _note = people.apply_action(expected, "expected", expected=None)
    check("expected can be cleared", cleared["expected"] is None)

    print("re-analysis reconciliation")
    grown = people.ensure_state(state, raw + [series("D", [(0.0, 3.0)])])
    check("a new id becomes a new person", len(grown["groups"]) == 4, str(len(grown["groups"])))
    check("a new id gets the next letter", grown["groups"][-1]["letter"] == "D", grown["groups"][-1]["letter"])
    shrunk = people.ensure_state(state, raw[:2])
    check("a vanished id is pruned", len(shrunk["groups"]) == 2, str(len(shrunk["groups"])))
    check("pruning keeps the surviving letters", [g["letter"] for g in shrunk["groups"]] == ["A", "B"])

    print("identity suggestions")
    lookalike = [
        series("A", [(0.0, 6.0)], size=0.16),
        series("B", [(7.0, 13.0)], size=0.161),  # 1 s gap, same size → suspect
    ]
    hints = people.merge_suggestions(lookalike, people.default_state(lookalike), grid())
    check("a lookalike on the same side is suggested", bool(hints), str(len(hints)))
    check("a suggestion is never applied by itself",
          len(people.default_state(lookalike)["groups"]) == 2)
    check("a suggestion carries a score", all("score" in item for item in hints), str(hints[:1]))
    separate = [
        series("A", [(0.0, 6.0)], size=0.16),
        series("B", [(20.0, 26.0)], size=0.16),  # long gap → not a suggestion
    ]
    check("a long gap suppresses the suggestion",
          not people.merge_suggestions(separate, people.default_state(separate), grid()))

    print("warnings")
    alone = [series("A", [(0.0, 6.0), (12.0, 20.0)])]
    view = people.derive(alone, people.default_state(alone), grid(), {"min_face_ratio": 0.04})
    kinds = [item["kind"] for item in view["warnings"]]
    check("a gap with nobody on screen is reported", "no_face" in kinds, str(kinds))
    gap = next(item for item in view["warnings"] if item["kind"] == "no_face")
    check("the gap points at the right time", abs(gap["start"] - 6.0) < 0.4, str(gap["start"]))
    tiny = people.derive([series("A", [(0.0, 20.0)], size=0.02)], people.default_state(
        [series("A", [(0.0, 20.0)], size=0.02)]), grid(), {"min_face_ratio": 0.04})
    check("a tiny face is reported", any(item["kind"] == "small_face" for item in tiny["warnings"]),
          str([item["kind"] for item in tiny["warnings"]]))

    print("sidecar")
    config.PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
    people_store.delete(PROJECT, 0)
    check("no state before the first correction", people_store.load_state(PROJECT, 0) is None)
    saved = people_store.save(PROJECT, 0, state, window=(0.0, 20.0), note="rename")
    check("state is written", people_store.load_state(PROJECT, 0) is not None)
    check("write records the window", saved["window"] == {"start": 0.0, "end": 20.0}, str(saved["window"]))
    check("revision is exposed for cache keys", people_store.revision(PROJECT, 0) == 1,
          str(people_store.revision(PROJECT, 0)))
    people_store.save(PROJECT, 0, ignored, window=(0.0, 20.0), note="ignore", previous=state)
    check("a second correction bumps the revision", people_store.revision(PROJECT, 0) == 2,
          str(people_store.revision(PROJECT, 0)))
    back = people_store.undo(PROJECT, 0)
    check("undo restores the previous state", back is not None and
          [g["ignored"] for g in back["state"]["groups"]] == [False, False, False], str(back and back["state"]))
    forward = people_store.redo(PROJECT, 0)
    check("redo re-applies it", forward is not None and
          forward["state"]["groups"][2]["ignored"] is True)
    check("the history stack is capped", people_store.HISTORY_MAX == 50, str(people_store.HISTORY_MAX))
    people_store.delete(PROJECT, 0)
    check("reset removes the sidecar", people_store.load_state(PROJECT, 0) is None)
    try:
        people_store.load("../../etc/passwd", 0)
        check("bad project ids are rejected", False)
    except people_store.PeopleStoreError:
        check("bad project ids are rejected", True)

    print("bad input")
    for action, kwargs in (
        ("nope", {}),
        ("rename", {"person": state["groups"][0]["id"], "label": "  "}),
        ("merge", {"person": state["groups"][0]["id"], "target": state["groups"][0]["id"]}),
        ("split", {"person": state["groups"][0]["id"], "at": None}),
        ("split", {"person": "p99", "at": 2.0}),
        ("expected", {"expected": 99}),
    ):
        try:
            people.apply_action(state, action, **kwargs)
            check(f"{action} {kwargs} is rejected", False)
        except people.PeopleError:
            check(f"{action} rejected: {list(kwargs)}", True)

    print()
    if FAILED:
        print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
        return 1
    print("all people tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
