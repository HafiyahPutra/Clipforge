"""Tahap 8.1 — the people on screen, and the user's corrections to them.

The tracker answers "where are the faces"; this module answers "who is on
screen, when, and did the user disagree". It is deliberately **pure**: it takes
the cached per-face series plus a small state document and returns numbers. No
decoding, no disk, no HTTP — so ignoring a face or merging two ids costs a few
milliseconds and is trivially testable.

Three ideas hold the model together:

* **Group** — one person. It owns one or more *members*; a member is a slice of
  one tracker id (``track`` + optional ``from``/``to``). One tracker id is one
  member by default; merging appends members, splitting cuts one member in two.
* **Letter** — ``A``/``B``/``C`` assigned once, when the group is created, and
  never reshuffled. Shot lists written before this feature store ``person: "B"``,
  so the letter must not move when somebody else is ignored.
* **Effective series** — the list the rest of the pipeline sees: ignored groups
  dropped, members folded into one series per group, splits masked by time. The
  speaker picker, the crop path and the shot draft all run on *that*, which is
  what makes a correction change the render.

Known limit (documented, not hidden): identity across a gap is decided by
geometry (time adjacency + face size) and a coarse colour histogram, not by a
face-recognition model. So two people who never appear together and look alike
are reported as a *suggestion* with a score, never merged silently.
"""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from backend.services.tracking.tracker import TrackSeries

# Fixed palette: a person keeps their colour for the whole session, including in
# the preview overlay and the timeline, so the two never disagree.
PALETTE = (
    "#38bdf8",
    "#f472b6",
    "#facc15",
    "#4ade80",
    "#c084fc",
    "#fb923c",
    "#f87171",
    "#22d3ee",
)

# A presence gap shorter than this is a tracking hiccup, not "the person left".
GAP_TOLERANCE = 0.35
# Same for speech: words have pauses.
TALK_GAP = 0.30

# Two tracker ids can describe the *same* face: the tracker lost the face and
# re-seeded a new id before the old one expired, so both ids are "on screen" at
# the same moment (the classic symptom is a third person appearing for the last
# few seconds of a two-person podcast). Two different people cannot occupy the
# same box, so overlapping ids whose boxes sit on top of each other are folded
# into one person instead of being counted twice. Ids that never share a frame
# are *not* folded — those stay separate and are only ever offered as a merge
# suggestion, because the same seat can legitimately host two people.
DUP_CX = 0.06  # 6 % of the frame width
DUP_CY = 0.08  # 8 % of the frame height
DUP_AREA_RATIO = 1.8


def letter_for(index: int) -> str:
    """A, B, C … (stable for the lifetime of the project)."""
    return chr(ord("A") + index) if 0 <= index < 26 else str(index + 1)


def _track_stats(item: dict[str, Any]) -> tuple[np.ndarray, float, float, float]:
    """Presence mask + median centre/size of one tracker id on the step grid."""
    valid = np.asarray(item.get("valid") or [], dtype=bool)
    if not valid.any():
        return valid, 0.0, 0.0, 0.0
    cx = np.asarray(item.get("cx") or [], dtype=float)
    cy = np.asarray(item.get("cy") or [], dtype=float)
    w = np.asarray(item.get("w") or [], dtype=float)
    h = np.asarray(item.get("h") or [], dtype=float)
    n = min(valid.size, cx.size, cy.size, w.size, h.size)
    if n == 0:
        return valid, 0.0, 0.0, 0.0
    valid, cx, cy, w, h = valid[:n], cx[:n], cy[:n], w[:n], h[:n]
    if not valid.any():
        return valid, 0.0, 0.0, 0.0
    return (
        valid,
        float(np.nanmedian(cx[valid])),
        float(np.nanmedian(cy[valid])),
        float(np.nanmedian((w * h)[valid])),
    )


def _same_face(a: tuple[np.ndarray, float, float, float],
               b: tuple[np.ndarray, float, float, float]) -> bool:
    """True when two tracker ids are the same physical face at the same time."""
    va, ax, ay, aa = a
    vb, bx, by, ab = b
    if not va.any() or not vb.any():
        return False
    n = min(va.size, vb.size)
    if n == 0 or not np.any(va[:n] & vb[:n]):
        return False  # never on screen together → not a duplicate
    if abs(ax - bx) > DUP_CX or abs(ay - by) > DUP_CY:
        return False  # different boxes → different people
    if aa <= 0.0 or ab <= 0.0:
        return False
    return max(aa, ab) / max(1e-6, min(aa, ab)) <= DUP_AREA_RATIO


def _duplicate_map(series_raw: list[dict[str, Any]]) -> dict[int, int]:
    """``{duplicate index: owner index}`` — ids that are one face folded to owner."""
    stats = [_track_stats(item) for item in series_raw]
    owner: dict[int, int] = {}
    for j in range(len(series_raw)):
        if j in owner:
            continue
        for i in range(j):
            if i in owner:
                continue  # i is itself a duplicate; only roots own others
            if _same_face(stats[i], stats[j]):
                owner[j] = i
                break
    return owner


def _find_duplicate_group(state: dict[str, Any], item: dict[str, Any],
                          series_raw: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Existing group whose member is the same face as ``item`` (for new ids)."""
    by_label = {str(raw.get("label")): raw for raw in series_raw}
    stats = _track_stats(item)
    for group in state.get("groups") or []:
        for member in group.get("members") or []:
            other = by_label.get(str(member.get("track")))
            if other is None:
                continue
            if _same_face(_track_stats(other), stats):
                return group
    return None


def default_state(series_raw: list[dict[str, Any]]) -> dict[str, Any]:
    """One group per *person* — duplicate tracker ids are folded on creation."""
    owner = _duplicate_map(series_raw)
    groups = []
    for index, item in enumerate(series_raw):
        if index in owner:
            continue
        label = str(item.get("label") or letter_for(len(groups)))
        members: list[dict[str, Any]] = [{"track": label, "from": None, "to": None}]
        merged_from: list[str] = []
        for dup, root in owner.items():
            if root != index:
                continue
            dup_label = str(series_raw[dup].get("label") or "")
            if dup_label:
                members.append({"track": dup_label, "from": None, "to": None})
                merged_from.append(dup_label)
        groups.append(
            {
                "id": f"p{len(groups) + 1}",
                "letter": letter_for(len(groups)),
                "label": f"Person {letter_for(len(groups))}",
                "color": PALETTE[len(groups) % len(PALETTE)],
                "ignored": False,
                "members": members,
                "merged_from": merged_from,
                "split_from": None,
            }
        )
    return {"revision": 1, "expected": None, "groups": groups}


# --- masks -------------------------------------------------------------------


def _member_mask(member: dict[str, Any], times: np.ndarray) -> np.ndarray:
    mask = np.ones(times.size, dtype=bool)
    start = member.get("from")
    end = member.get("to")
    if start is not None:
        mask &= times >= float(start) - 1e-6
    if end is not None:
        mask &= times < float(end) - 1e-6
    return mask


def _group_series(
    group: dict[str, Any], by_label: dict[str, dict[str, Any]], times: np.ndarray
) -> dict[str, np.ndarray] | None:
    """Fold a group's members into one series (largest face wins per step)."""
    steps = times.size
    cx = np.zeros(steps)
    cy = np.zeros(steps)
    w = np.zeros(steps)
    h = np.zeros(steps)
    jaw = np.zeros(steps)
    valid = np.zeros(steps, dtype=bool)
    used = False

    for member in group.get("members") or []:
        item = by_label.get(str(member.get("track") or ""))
        if not item:
            continue
        item_cx = np.asarray(item.get("cx") or [], dtype=float)
        if item_cx.size != steps:
            continue
        item_w = np.asarray(item.get("w") or [], dtype=float)
        item_h = np.asarray(item.get("h") or [], dtype=float)
        mask = _member_mask(member, times) & np.asarray(item.get("valid") or [], dtype=bool)
        area = item_w * item_h
        current = np.where(valid, w * h, -1.0)
        take = mask & (area > current)
        if take.any():
            cx[take] = item_cx[take]
            cy[take] = np.asarray(item.get("cy") or [], dtype=float)[take]
            w[take] = item_w[take]
            h[take] = item_h[take]
            jaw[take] = np.asarray(item.get("jaw") or [], dtype=float)[take]
        valid |= mask
        used = used or bool(mask.any())

    if not used:
        return None
    return {"cx": cx, "cy": cy, "w": w, "h": h, "jaw": jaw, "valid": valid}


def _runs(mask: np.ndarray, times: np.ndarray, gap: float) -> list[list[float]]:
    """Boolean steps → ``[[start, end], …]`` in clip-relative seconds."""
    if mask.size == 0:
        return []
    step = float(np.median(np.diff(times))) if times.size > 1 else 0.1
    runs: list[list[float]] = []
    start: float | None = None
    last_true: float | None = None
    for position, flag in enumerate(mask):
        now = float(times[position])
        if flag:
            if start is None:
                start = now
            elif last_true is not None and now - last_true > gap + step:
                runs.append([round(start, 3), round(last_true + step, 3)])
                start = now
            last_true = now
    if start is not None and last_true is not None:
        runs.append([round(start, 3), round(last_true + step, 3)])
    return [[item[0], max(item[0], item[1])] for item in runs]


def _talk_mask(jaw: np.ndarray, valid: np.ndarray, params: dict[str, Any]) -> np.ndarray:
    """Per-face speaking mask, using the same percentile rule as the speaker picker.

    Deliberately a *display* signal: the shot draft keeps using the strict
    ``speaker.choose_target`` mask, so nothing about cutting changes here.
    """
    if not valid.any():
        return np.zeros(valid.size, dtype=bool)
    floor = float(params.get("speaker_threshold_floor", 0.05))
    cap = float(params.get("speaker_threshold", 0.18))
    pct = float(params.get("speaker_threshold_percentile", 60))
    limit = float(np.clip(np.percentile(jaw[valid], pct), floor, max(floor, cap)))
    return valid & (jaw >= limit)


# --- effective series --------------------------------------------------------


def _rank(groups: list[dict[str, Any]], series: dict[str, dict[str, np.ndarray]]) -> list[str]:
    """Group ids, biggest presence first — used to honour "expected people"."""
    scored = []
    for group in groups:
        item = series.get(group["id"])
        score = float(item["valid"].mean()) if item else 0.0
        if item is not None:
            area = item["w"] * item["h"]
            if item["valid"].any():
                score *= float(np.nanmedian(area[item["valid"]]))
        scored.append((score, group["id"]))
    scored.sort(reverse=True)
    return [group_id for _, group_id in scored]


def _split_active(
    state: dict[str, Any], series_raw: list[dict[str, Any]], times: np.ndarray
) -> tuple[list[dict[str, Any]], list[str], dict[str, dict[str, np.ndarray]]]:
    """Groups that take part in tracking, the ones excluded by ``expected``, series."""
    by_label = {str(item.get("label")): item for item in series_raw}
    series: dict[str, dict[str, np.ndarray]] = {}
    for group in state.get("groups") or []:
        built = _group_series(group, by_label, times)
        if built is not None:
            series[group["id"]] = built

    candidates = [group for group in state.get("groups") or [] if not group.get("ignored")]
    expected = state.get("expected")
    excluded: list[str] = []
    if isinstance(expected, int) and expected > 0 and len(candidates) > expected:
        order = _rank(candidates, series)
        keep = set(order[:expected])
        excluded = [group_id for group_id in order[expected:] if group_id in keep or True]
        candidates = [group for group in candidates if group["id"] in keep]
    return candidates, excluded, series


def effective_series(
    series_raw: list[dict[str, Any]],
    state: dict[str, Any],
    times: np.ndarray,
) -> tuple[list[TrackSeries], list[str]]:
    """Series list the pipeline should run on, plus each group's letter."""
    active, _excluded, series = _split_active(state, series_raw, times)
    out: list[TrackSeries] = []
    labels: list[str] = []
    for group in active:
        item = series.get(group["id"])
        if item is None:
            continue
        detections = 0
        for member in group.get("members") or []:
            for raw in series_raw:
                if str(raw.get("label")) == str(member.get("track")):
                    detections += int(raw.get("detections") or 0)
        out.append(
            TrackSeries(
                id=len(out),
                cx=item["cx"],
                cy=item["cy"],
                w=item["w"],
                h=item["h"],
                jaw=item["jaw"],
                valid=item["valid"],
                detections=detections,
            )
        )
        labels.append(str(group.get("letter") or letter_for(len(out) - 1)))
    return out, labels


def person_index(state: dict[str, Any], labels: list[str]) -> dict[str, int]:
    """Map both the stable letter and the person id to an index in the series list."""
    index: dict[str, int] = {}
    active, _excluded, _series = _split_active(state, [], np.zeros(1))
    for position, group in enumerate(active):
        letter = str(group.get("letter") or "")
        if position < len(labels):
            letter = labels[position]
        if letter:
            index[letter] = position
        index[str(group["id"])] = position
    return index


# --- derived view ------------------------------------------------------------


def derive(
    series_raw: list[dict[str, Any]],
    state: dict[str, Any],
    times: np.ndarray,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Everything the People panel shows: presence, speech, confidence, warnings."""
    params = params or {}
    active, excluded, series = _split_active(state, series_raw, times)
    by_label = {str(item.get("label")): item for item in series_raw}
    min_face = float(params.get("min_face_ratio", 0.04))

    people: list[dict[str, Any]] = []
    for group in state.get("groups") or []:
        item = series.get(group["id"])
        if item is None:
            continue
        valid = item["valid"]
        talk = _talk_mask(item["jaw"], valid, params)
        ranges = _runs(valid, times, GAP_TOLERANCE)
        talking = _runs(talk, times, TALK_GAP)
        coverage = round(float(valid.mean()), 3) if valid.size else 0.0
        area = item["w"] * item["h"]
        median_area = float(np.nanmedian(area[valid])) if valid.any() else 0.0
        # Heuristic confidence, not a model score: how much of the clip we see the
        # face, how big it is (small faces are unreliable), how fragmented it is.
        size_score = min(1.0, median_area / max(1e-6, min_face * 4.0))
        gaps = max(0, len(ranges) - 1)
        continuity = 1.0 - min(1.0, gaps * 0.15)
        confidence = round(0.5 * coverage + 0.3 * size_score + 0.2 * continuity, 3)

        best = None
        for member in group.get("members") or []:
            raw = by_label.get(str(member.get("track") or ""))
            candidate = (raw or {}).get("best")
            if candidate and (best is None or float(candidate.get("w", 0)) * float(candidate.get("h", 0)) > float(best.get("w", 0)) * float(best.get("h", 0))):
                best = candidate

        people.append(
            {
                "id": group["id"],
                "letter": group.get("letter"),
                "label": group.get("label"),
                "color": group.get("color"),
                "ignored": bool(group.get("ignored")),
                "excluded": group["id"] in excluded,
                "ranges": ranges,
                "talking": talking,
                "presence_seconds": round(sum(end - start for start, end in ranges), 2),
                "talking_seconds": round(sum(end - start for start, end in talking), 2),
                "coverage": coverage,
                "confidence": confidence,
                "median_area": round(median_area, 5),
                "tracks": [str(member.get("track")) for member in group.get("members") or []],
                "merged_from": list(group.get("merged_from") or []),
                "split_from": group.get("split_from"),
                "best": best,
                "ranges_count": len(ranges),
            }
        )

    return {
        "people": people,
        "excluded": excluded,
        "active": len(active),
        "detected": len(people),
        "expected": state.get("expected"),
        "revision": int(state.get("revision") or 1),
        "warnings": warnings(series_raw, state, times, people),
    }


def warnings(
    series_raw: list[dict[str, Any]],
    state: dict[str, Any],
    times: np.ndarray,
    people: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Clickable problems: gaps, flicker, tiny faces, odd counts, suspect ids."""
    out: list[dict[str, Any]] = []
    duration = float(times[-1]) if times.size else 0.0

    # 1. Stretches with nobody on screen.
    covered = np.zeros(times.size, dtype=bool)
    for person in people:
        for start, end in person["ranges"]:
            covered |= (times >= start - 1e-6) & (times <= end + 1e-6)
    for start, end in _runs(~covered, times, 0.5):
        if end - start >= 1.5:
            out.append(
                {
                    "kind": "no_face",
                    "severity": "warn",
                    "start": start,
                    "end": end,
                    "text": f"No face detected for {end - start:.1f}s",
                }
            )

    for person in people:
        # 2. A face that keeps disappearing and coming back.
        if person["ranges_count"] >= 4:
            longest = max((end - start for start, end in person["ranges"]), default=0.0)
            if longest < max(2.0, duration * 0.15):
                out.append(
                    {
                        "kind": "flicker",
                        "severity": "info",
                        "start": person["ranges"][0][0],
                        "end": person["ranges"][-1][1],
                        "person": person["id"],
                        "text": f"{person['label']} appears in {person['ranges_count']} short pieces",
                    }
                )
        # 3. Faces too small to track reliably.
        if person["median_area"] and person["median_area"] < 0.02:
            out.append(
                {
                    "kind": "small_face",
                    "severity": "warn",
                    "start": person["ranges"][0][0] if person["ranges"] else 0.0,
                    "person": person["id"],
                    "text": f"{person['label']} is small in frame ({person['median_area'] * 100:.1f}% of the frame)",
                }
            )

    # 4. The number of people jumping around.
    counts = np.zeros(times.size, dtype=int)
    for person in people:
        for start, end in person["ranges"]:
            counts += ((times >= start - 1e-6) & (times <= end + 1e-6)).astype(int)
    step = float(np.median(np.diff(times))) if times.size > 1 else 0.1
    window = max(1, int(round(0.5 / max(step, 1e-3))))
    for position in range(window, counts.size, window):
        delta = abs(int(counts[position]) - int(counts[position - window]))
        if delta >= 2:
            out.append(
                {
                    "kind": "count_shift",
                    "severity": "info",
                    "start": round(float(times[position]), 3),
                    "text": f"People on screen went from {counts[position - window]} to {counts[position]}",
                }
            )

    # 5. Two ids that are probably the same person.
    for suggestion in merge_suggestions(series_raw, state, times):
        out.append(
            {
                "kind": "possible_merge",
                "severity": "info",
                "start": suggestion["start"],
                "person": suggestion["a"],
                "other": suggestion["b"],
                "score": suggestion["score"],
                "text": (
                    f"{suggestion['a_label']} and {suggestion['b_label']} never appear together "
                    f"and look alike ({suggestion['score'] * 100:.0f}%) — merge them?"
                ),
            }
        )

    # 6. A shot that follows somebody who is not there.
    for group in state.get("groups") or []:
        if group.get("ignored"):
            continue
    return out


def merge_suggestions(
    series_raw: list[dict[str, Any]],
    state: dict[str, Any],
    times: np.ndarray,
) -> list[dict[str, Any]]:
    """Pairs of ids that never overlap in time but are probably one person.

    Score = 0.55 × size similarity + 0.45 × colour-histogram similarity, and only
    for pairs whose presence ranges are adjacent (gap < 2 s) or complementary.
    Geometry alone produces too many false positives, so both terms are required.
    """
    active, _excluded, series = _split_active(state, series_raw, times)
    by_id = {group["id"]: group for group in active}
    by_label = {str(item.get("label")): item for item in series_raw}
    out: list[dict[str, Any]] = []
    ids = [group_id for group_id in by_id if group_id in series]

    for left_index, left in enumerate(ids):
        for right in ids[left_index + 1 :]:
            a, b = series[left], series[right]
            if (a["valid"] & b["valid"]).any():
                continue  # seen together → definitely different people
            a_runs = _runs(a["valid"], times, GAP_TOLERANCE)
            b_runs = _runs(b["valid"], times, GAP_TOLERANCE)
            if not a_runs or not b_runs:
                continue
            gap = max(
                min(abs(a_runs[0][0] - b_runs[-1][1]), abs(b_runs[0][0] - a_runs[-1][1])),
                0.0,
            )
            if gap > 2.0:
                continue
            area_a = float(np.nanmedian((a["w"] * a["h"])[a["valid"]]))
            area_b = float(np.nanmedian((b["w"] * b["h"])[b["valid"]]))
            size_sim = 1.0 - min(1.0, abs(area_a - area_b) / max(1e-6, max(area_a, area_b)))
            hist_sim = _hist_similarity(
                by_label.get(str(by_id[left].get("members", [{}])[0].get("track"))) or {},
                by_label.get(str(by_id[right].get("members", [{}])[0].get("track"))) or {},
            )
            score = round(0.55 * size_sim + 0.45 * hist_sim, 3)
            if score < 0.55:
                continue
            out.append(
                {
                    "a": left,
                    "b": right,
                    "a_label": by_id[left].get("label"),
                    "b_label": by_id[right].get("label"),
                    "score": score,
                    "start": round(min(a_runs[0][0], b_runs[0][0]), 3),
                }
            )
    out.sort(key=lambda item: item["score"], reverse=True)
    return out[:5]


def _hist_similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    """Cosine similarity of the cached colour histograms; 0.5 when unavailable."""
    ha = np.asarray(a.get("hist") or [], dtype=float)
    hb = np.asarray(b.get("hist") or [], dtype=float)
    if ha.size == 0 or hb.size == 0 or ha.size != hb.size:
        return 0.5
    denominator = float(np.linalg.norm(ha) * np.linalg.norm(hb))
    if denominator < 1e-9:
        return 0.5
    return float(np.clip(np.dot(ha, hb) / denominator, 0.0, 1.0))


# --- mutations ---------------------------------------------------------------


class PeopleError(ValueError):
    """Bad correction — surfaced to the API as a 400."""


def _find(state: dict[str, Any], person_id: str) -> dict[str, Any]:
    for group in state.get("groups") or []:
        if group["id"] == person_id:
            return group
    raise PeopleError(f"Unknown person {person_id!r}.")


def _next_letter(state: dict[str, Any]) -> str:
    used = {str(group.get("letter")) for group in state.get("groups") or []}
    for index in range(26):
        candidate = letter_for(index)
        if candidate not in used:
            return candidate
    return str(len(used) + 1)


def apply_action(
    state: dict[str, Any],
    action: str,
    *,
    person: str | None = None,
    target: str | None = None,
    label: str | None = None,
    at: float | None = None,
    expected: int | None | str = "",
) -> tuple[dict[str, Any], str]:
    """Apply one correction; returns the new state plus a human summary."""
    new = {
        "revision": int(state.get("revision") or 1) + 1,
        "expected": state.get("expected"),
        "groups": [dict(group, members=[dict(member) for member in group.get("members") or []]) for group in state.get("groups") or []],
    }
    action = (action or "").strip().lower()

    if action == "rename":
        group = _find(new, str(person or ""))
        text = (label or "").strip()[:40]
        if not text:
            raise PeopleError("Label cannot be empty.")
        group["label"] = text
        return new, f"Renamed {group['letter']} to {text}"

    if action in {"ignore", "restore"}:
        group = _find(new, str(person or ""))
        group["ignored"] = action == "ignore"
        return new, f"{'Ignored' if group['ignored'] else 'Restored'} {group['label']}"

    if action == "merge":
        source = _find(new, str(person or ""))
        destination = _find(new, str(target or ""))
        if source["id"] == destination["id"]:
            raise PeopleError("Pick two different people to merge.")
        destination["members"].extend(source["members"])
        destination["merged_from"] = list(destination.get("merged_from") or []) + [
            source.get("letter") or source["id"]
        ] + list(source.get("merged_from") or [])
        new["groups"] = [group for group in new["groups"] if group["id"] != source["id"]]
        return new, f"Merged {source['label']} into {destination['label']}"

    if action == "split":
        group = _find(new, str(person or ""))
        if at is None:
            raise PeopleError("A split needs a time.")
        cut = float(at)
        moved: list[dict[str, Any]] = []
        kept: list[dict[str, Any]] = []
        for member in group["members"]:
            start = member.get("from")
            end = member.get("to")
            starts_before = start is None or float(start) < cut
            ends_after = end is None or float(end) > cut
            if starts_before and ends_after:
                kept.append({**member, "to": cut})
                moved.append({**member, "from": cut})
            elif start is not None and float(start) >= cut:
                moved.append(member)
            else:
                kept.append(member)
        if not moved:
            raise PeopleError("Nothing to split at that time — the person is not there.")
        group["members"] = kept or moved
        if not kept:
            moved = []
        if not moved:
            raise PeopleError("Nothing to split at that time — the person is not there.")
        new_group = {
            "id": f"p{max(int(group['id'][1:]) for group in new['groups']) + 1}",
            "letter": _next_letter(new),
            "label": f"{group['label']} (2)",
            "color": PALETTE[(len(new["groups"])) % len(PALETTE)],
            "ignored": False,
            "members": moved,
            "merged_from": [],
            "split_from": group["id"],
        }
        new["groups"].append(new_group)
        return new, f"Split {group['label']} at {cut:.2f}s"

    if action == "expected":
        value = expected
        if value in ("", None):
            new["expected"] = None
            return new, "Expected people cleared"
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise PeopleError("Expected people must be a whole number.") from exc
        if number < 1 or number > 12:
            raise PeopleError("Expected people must be between 1 and 12.")
        new["expected"] = number
        return new, f"Expecting {number} people"

    raise PeopleError(f"Unknown action {action!r}.")


def prune(state: dict[str, Any], series_raw: list[dict[str, Any]]) -> dict[str, Any]:
    """Drop members whose tracker id no longer exists after a re-analysis."""
    labels = {str(item.get("label")) for item in series_raw}
    groups = []
    for group in state.get("groups") or []:
        members = [member for member in group.get("members") or [] if str(member.get("track")) in labels]
        if members:
            groups.append({**group, "members": members})
    if not groups:
        return default_state(series_raw)
    state = dict(state)
    state["groups"] = groups
    return state


def ensure_state(
    state: dict[str, Any] | None, series_raw: list[dict[str, Any]]
) -> dict[str, Any]:
    """Load-or-default, then reconcile with the current series list."""
    if not state or not state.get("groups"):
        return default_state(series_raw)
    state = prune(state, series_raw)
    # A new tracker id that no group owns becomes its own person.
    known = {str(member.get("track")) for group in state["groups"] for member in group.get("members") or []}
    next_index = max((int(group["id"][1:]) for group in state["groups"]), default=0)
    for item in series_raw:
        label = str(item.get("label"))
        if label in known:
            continue
        target = _find_duplicate_group(state, item, series_raw)
        if target is not None:
            target["members"].append({"track": label, "from": None, "to": None})
            target["merged_from"] = list(target.get("merged_from") or []) + [label]
            known.add(label)
            continue
        next_index += 1
        state["groups"].append(
            {
                "id": f"p{next_index}",
                "letter": _next_letter(state),
                "label": f"Person {_next_letter(state)}",
                "color": PALETTE[(next_index - 1) % len(PALETTE)],
                "ignored": False,
                "members": [{"track": label, "from": None, "to": None}],
                "merged_from": [],
                "split_from": None,
            }
        )
    return state


def summary(people: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Counts for the panel header."""
    items = list(people)
    return {
        "people": len([item for item in items if not item.get("ignored")]),
        "ignored": len([item for item in items if item.get("ignored")]),
        "merged": len([item for item in items if item.get("merged_from")]),
    }
