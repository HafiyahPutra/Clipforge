"""Preview segment-timing regression tests (Phase 8, framing panel).

The bug this locks down: ``shot_plan.build`` emits its crop points in
**clip-relative** time (``shot.start - clip_start``), but ``preview_cache``
sliced them with the segment's **absolute source** bounds. Every point fell
outside the window, ``slice_points`` returned ``None``, and the renderer took
the letterbox-blur fallback — so the whole shot list, manual ``frameCx``
included, was silently ignored. The preview looked fine, which is exactly why
it went unnoticed.

These tests are pure data: no ffmpeg, no video, no project on disk.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services import preview_cache  # noqa: E402

FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
        return
    print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))
    FAILED.append(name)


def _points(count: int, *, t0: float = 0.0, t1: float = 30.0, x: float = 0.5) -> list[dict]:
    """A flat crop path over clip-relative ``[t0, t1]``."""
    step = (t1 - t0) / max(1, count - 1)
    return [
        {"t": round(t0 + i * step, 4), "x": x, "y": 0.45, "z": 1.0, "cut": i == 0}
        for i in range(count)
    ]


def case_relative_slice() -> None:
    print("case: clip-relative points sliced with clip-relative bounds")
    points = _points(301)
    # Clip starts at 600 s in the source; the whole clip is one segment.
    start, lo, hi = 600.0, 600.0, 630.0
    rel = preview_cache.slice_points(points, lo - start, hi - start)
    abs_ = preview_cache.slice_points(points, lo, hi)
    check("relative window keeps every point", rel is not None and len(rel) == 301, str(len(rel or [])))
    check("absolute window finds nothing (the old bug)", abs_ is None, str(len(abs_ or [])))
    check("relative slice is re-based to zero", rel is not None and abs(rel[0]["t"]) < 1e-9, str(rel[0]["t"] if rel else None))
    check("relative slice spans the clip", rel is not None and abs(rel[-1]["t"] - 30.0) < 1e-6, str(rel[-1]["t"] if rel else None))


def case_second_segment() -> None:
    print("case: the second segment of a split clip")
    points = _points(301)
    start = 600.0
    # A cut at 615 s: segment two covers 615..630 absolute.
    seg_points = preview_cache.slice_points(points, 615.0 - start, 630.0 - start)
    check("second segment has points", seg_points is not None, "None")
    if seg_points:
        check("second segment starts at its own zero", abs(seg_points[0]["t"]) < 1e-9, str(seg_points[0]["t"]))
        check("first point is a hard cut", seg_points[0].get("cut") is True)
        check("second segment ends at 15 s", abs(seg_points[-1]["t"] - 15.0) < 0.01, str(seg_points[-1]["t"]))


def case_manual_centre_reaches_points() -> None:
    print("case: a manual frameCx reaches the crop path")
    # Two flat paths, only the centre differs — exactly what the slider writes.
    left = _points(301, x=0.15833)
    right = _points(301, x=0.84167)
    check("paths differ", left[0]["x"] != right[0]["x"])
    start = 600.0
    l = preview_cache.slice_points(left, 0.0, 30.0)
    r = preview_cache.slice_points(right, 0.0, 30.0)
    check("left slice survives", l is not None and len(l) == 301)
    check("right slice survives", r is not None and len(r) == 301)
    if l and r:
        check("centres stay distinct after slicing", abs(l[0]["x"] - r[0]["x"]) > 0.6,
              f"{l[0]['x']} vs {r[0]['x']}")


def case_empty_and_edges() -> None:
    print("case: degenerate inputs")
    check("empty points -> None", preview_cache.slice_points([], 0.0, 30.0) is None)
    check("None points -> None", preview_cache.slice_points(None, 0.0, 30.0) is None)
    points = _points(3, t0=0.0, t1=2.0)
    outside = preview_cache.slice_points(points, 100.0, 130.0)
    check("window past the end -> None", outside is None, str(outside))
    # A window that only overlaps by a sliver still has to yield points.
    edge = preview_cache.slice_points(points, 1.9, 2.5)
    check("touching window yields points", edge is not None and len(edge) > 0, str(edge))


def main() -> int:
    for case in (case_relative_slice, case_second_segment, case_manual_centre_reaches_points, case_empty_and_edges):
        case()
    print()
    if FAILED:
        print(f"{len(FAILED)} check(s) failed: {', '.join(FAILED)}")
        return 1
    print("all preview timing checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
