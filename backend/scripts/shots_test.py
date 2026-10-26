"""Offline checks for Tahap 7 (shot list + hard cut + dynamic zoom).

Two synthetic clips with *known* geometry, so the assertions are numeric rather
than "looks about right":

A. hard cut — left half red, right half blue. The path jumps from x=0.25 to
   x=0.75 at t=2.0 s. Every frame before the cut must be pure red, every frame
   after must be pure blue, and no frame may be a red/blue blend (a blend would
   mean the crop slid between the two subjects instead of snapping).

B. zoom — one 20 px white stripe on black at the centre. The stripe's width in
   the output is a direct measurement of the zoom factor: 20 * 1080/608 at 1.0x,
   1.4x wider at 1.4x.

Run: backend/.venv/bin/python -m backend.scripts.shots_test
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from backend.services import edit_service, video_engine
from backend.services.shots import plan as shot_plan
from backend.services.tracking import config as tracking_config

TMP = Path(tempfile.mkdtemp(prefix="shots_test_"))


def _ffmpeg(*args: str) -> None:
    binary = video_engine.capabilities()["path"]
    result = subprocess.run([binary, "-y", "-loglevel", "error", *args], capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode()[-800:])


def _payload(*, duration: float, cx: list[float], shots: list[dict]) -> dict:
    steps = len(cx)
    times = np.linspace(0.0, duration, steps)
    jaw = [0.4 if 0.3 < t < duration - 0.3 else 0.0 for t in times]
    return {
        "start": 0.0,
        "end": duration,
        "duration": duration,
        "clip_type": "podcast",
        "shots": shots,
        "raw": {
            "cx": [round(v, 5) for v in cx],
            "cy": [0.45] * steps,
            "h": [0.30] * steps,
        },
        "activity": {
            "target": [0] * steps,
            "talking": [1 if value > 0 else 0 for value in jaw],
            "talk": [[1 if value > 0 else 0 for value in jaw]],
            "overlap": [1] * steps,
            "jaw": jaw,
        },
        "metrics": {"src_size": [1920, 1080]},
        "params": {},
    }


def _frame(path: Path, at: float, out: Path) -> np.ndarray:
    _ffmpeg("-ss", f"{at:.3f}", "-i", str(path), "-frames:v", "1", str(out))
    raw = out.read_bytes()
    import av  # noqa: PLC0415 - local import keeps the module import cheap

    with av.open(str(out)) as container:
        frame = next(container.decode(video=0))
        return frame.to_ndarray(format="rgb24")


def _colors(frame: np.ndarray) -> dict[str, float]:
    height, width, _ = frame.shape
    left = frame[:, : width // 3, :].reshape(-1, 3).mean(axis=0)
    right = frame[:, 2 * width // 3 :, :].reshape(-1, 3).mean(axis=0)
    return {
        "left_r": round(float(left[0]), 1),
        "left_b": round(float(left[2]), 1),
        "right_r": round(float(right[0]), 1),
        "right_b": round(float(right[2]), 1),
    }


def case_draft() -> dict:
    """Speaker-turn rules, checked against a scripted activity timeline.

    Two real cuts (A→B at ~3.05, B→A at ~7.05) and one that must be suppressed:
    a 0.3 s interjection is too short to satisfy `cut_min_talk`, so no cut lands
    there even though the tracker briefly switched faces.
    """
    import numpy as np  # noqa: PLC0415

    from backend.services.shots import draft as shot_draft
    from backend.services.tracking import config as tracking_config

    times = np.arange(0.0, 20.0, 0.1)
    target = np.zeros(times.size, dtype=int)
    talk = np.zeros(times.size, dtype=bool)

    # `target` = who the tracker follows (it holds the old face for a moment),
    # `talk` = whether the followed face's mouth is moving.
    def turn(face: int, lo: float, hi: float, *, hold: float | None = None) -> None:
        mask = (times >= lo) & (times < (hi if hold is None else hold))
        target[mask] = face
        speaking = (times >= lo) & (times < hi)
        talk[speaking] = True

    turn(0, 0.0, 3.0, hold=3.2)        # A opens
    turn(1, 3.2, 7.0, hold=7.2)        # B takes over   -> cut near 3.05
    turn(0, 7.2, 10.5, hold=11.0)      # A again        -> cut near 7.05
    turn(1, 11.0, 11.3, hold=11.3)     # B interjects 0.3 s -> must NOT cut
    turn(0, 11.5, 20.0, hold=20.0)     # A resumes (tracker already on A)

    params = tracking_config.resolve("podcast")
    shots, diag = shot_draft.build_draft(
        times, target, talk, params, start=100.0, end=120.0,
        person_labels=["A", "B"],
    )
    cuts_abs = [round(shot.start - 100.0, 3) for shot in shots[1:]]
    ok = (
        len(shots) == 3
        and abs(cuts_abs[0] - 3.05) <= 0.2
        and abs(cuts_abs[1] - 7.05) <= 0.2
        and all(shot.duration >= 1.8 for shot in shots)
        and shots[0].start == 100.0
        and shots[-1].end == 120.0
    )
    return {
        "case": "draft",
        "shots": [{"i": shot.index, "start": shot.start, "end": shot.end,
                   "kind": shot.kind, "source": shot.source} for shot in shots],
        "cuts_relative": cuts_abs,
        "skipped": diag["skipped"],
        "cuts_per_minute": diag["cuts_per_minute"],
        "ok": ok,
    }


def case_whole_frame() -> dict:
    """Simultaneous speech must switch to whole_frame and back, with hard cuts.

    A talks alone, both talk for 3.5 s, then B alone. The overlap has to become
    exactly one whole_frame shot, the two cuts must land on the overlap edges,
    and a sub-threshold overlap (shorter than `overlap_min_seconds`) must be
    ignored instead of producing a 0.5 s whole_frame flash.
    """
    import numpy as np  # noqa: PLC0415

    from backend.services.shots import draft as shot_draft
    from backend.services.tracking import config as tracking_config

    times = np.arange(0.0, 20.0, 0.1)
    talk = np.zeros((2, times.size), dtype=bool)
    talk[0, : int(5.0 / 0.1)] = True                 # A alone
    talk[0, int(6.0 / 0.1) : int(9.5 / 0.1)] = True  # both 6.0-9.5
    talk[1, int(6.0 / 0.1) : int(9.5 / 0.1)] = True
    talk[1, int(10.0 / 0.1) :] = True                # B alone
    target = np.where(times < 9.8, 0, 1)
    speech = talk.any(axis=0)
    overlap = talk.sum(axis=0)

    params = tracking_config.resolve("podcast")
    shots, diag = shot_draft.build_draft(
        times, target, talk, params, start=0.0, end=20.0,
        person_labels=["A", "B"], speech=speech, overlap=overlap,
    )
    kinds = [shot.kind for shot in shots]
    whole = [shot for shot in shots if shot.kind == "whole_frame"]
    ok = (
        kinds == ["follow_speaker", "whole_frame", "follow_speaker"]
        and len(whole) == 1
        and abs(whole[0].start - 6.0) <= 0.3
        and abs(whole[0].end - 9.5) <= 0.5
        and all(shot.duration >= 1.8 for shot in shots)
    )

    # Short overlap: two people talk together for 0.6 s (< overlap_min_seconds).
    brief = np.zeros((2, times.size), dtype=bool)
    brief[0, : int(8.0 / 0.1)] = True
    brief[1, int(3.0 / 0.1) : int(3.6 / 0.1)] = True
    brief[1, int(8.5 / 0.1) :] = True
    brief_shots, _ = shot_draft.build_draft(
        times, np.where(times < 8.2, 0, 1), brief, params, start=0.0, end=20.0,
        person_labels=["A", "B"], speech=brief.any(axis=0), overlap=brief.sum(axis=0),
    )
    ok = ok and all(shot.kind != "whole_frame" for shot in brief_shots)

    return {
        "case": "whole_frame",
        "shots": [{"i": shot.index, "start": shot.start, "end": shot.end,
                   "kind": shot.kind, "zoom": shot.zoom} for shot in shots],
        "overlap_spans": diag.get("overlap_spans"),
        "brief_overlap_kinds": [shot.kind for shot in brief_shots],
        "ok": ok,
    }


def case_cut() -> dict:
    video = TMP / "cut_src.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=red:s=1920x1080:r=30:d=4",
        "-f", "lavfi", "-i", "color=c=blue:s=960x1080:r=30:d=4",
        "-filter_complex", "[0][1]overlay=960:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(video),
    )
    steps = 41
    cx = [0.25 if t < 2.0 else 0.75 for t in np.linspace(0.0, 4.0, steps)]
    payload = _payload(
        duration=4.0,
        cx=cx,
        shots=[
            {"index": 1, "start": 0.0, "end": 2.0, "kind": "follow_speaker", "zoom": 1.0},
            {"index": 2, "start": 2.0, "end": 4.0, "kind": "follow_speaker", "zoom": 1.0},
        ],
    )
    plan = shot_plan.build(payload, tracking_config.resolve("podcast"),
                           src_w=1920, src_h=1080, out_w=1080, out_h=1920)
    out = TMP / "cut_out.mp4"
    video_engine.render_clip(
        video, out, start=0.0, end=4.0, width=1080, height=1920,
        encoder="libx264", has_audio=False,
        src_size=(1920, 1080), face_keyframes=plan["points"], fps=30,
    )
    # Decode every frame: a hard cut means each frame is entirely one colour and
    # there is exactly one transition. Any frame that is part red and part blue
    # would be the crop sliding between subjects.
    import av  # noqa: PLC0415

    labels: list[str] = []
    with av.open(str(out)) as container:
        for frame in container.decode(video=0):
            data = frame.to_ndarray(format="rgb24")
            red = float(data[:, :, 0].mean())
            blue = float(data[:, :, 2].mean())
            if red > 120 and blue < 40:
                labels.append("red")
            elif blue > 120 and red < 40:
                labels.append("blue")
            else:
                labels.append(f"mixed({red:.0f}/{blue:.0f})")
    transitions = [i for i in range(1, len(labels)) if labels[i] != labels[i - 1]]
    mixed = [label for label in labels if label.startswith("mixed")]
    return {
        "case": "hard_cut",
        "frames": len(labels),
        "first": labels[0],
        "last": labels[-1],
        "transitions": transitions,
        "transition_seconds": [round(index / 30.0, 3) for index in transitions],
        "mixed_frames": mixed,
        "cuts": plan["cuts"],
        "cut_jumps_pct": plan["metrics"]["cut_jumps_pct"],
        "ok": (
            not mixed
            and len(transitions) == 1
            and labels[0] == "red"
            and labels[-1] == "blue"
            and abs(transitions[0] / 30.0 - 2.0) <= 0.05
        ),
    }


def case_zoom() -> dict:
    video = TMP / "zoom_src.mp4"
    _ffmpeg(
        "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=30:d=3",
        "-f", "lavfi", "-i", "color=c=white:s=20x1080:r=30:d=3",
        "-filter_complex", "[0][1]overlay=950:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(video),
    )
    results = {}
    for zoom in (1.0, 1.2, 1.4):
        payload = _payload(
            duration=3.0,
            cx=[0.5] * 31,
            shots=[{"index": 1, "start": 0.0, "end": 3.0, "kind": "follow_speaker", "zoom": zoom}],
        )
        plan = shot_plan.build(payload, tracking_config.resolve("podcast"),
                               src_w=1920, src_h=1080, out_w=1080, out_h=1920)
        out = TMP / f"zoom_{zoom}.mp4"
        video_engine.render_clip(
            video, out, start=0.0, end=3.0, width=1080, height=1920,
            encoder="libx264", has_audio=False,
            src_size=(1920, 1080), face_keyframes=plan["points"], fps=30,
        )
        frame = _frame(out, 1.5, TMP / f"zoom_{zoom}.png")
        column = frame[:, :, 0].mean(axis=0)  # brightness per column
        bright = np.flatnonzero(column > 128)
        width_px = int(bright.size)
        results[str(zoom)] = {
            "stripe_px": width_px,
            "expected_px": round(20 * 1080 / 608 * zoom, 1),
            "zoom_reported": plan["metrics"]["zoom_max"],
        }
    errors = [
        abs(value["stripe_px"] - value["expected_px"]) / value["expected_px"]
        for value in results.values()
    ]
    return {"case": "zoom", "results": results, "max_rel_error": round(max(errors), 4),
            "ok": max(errors) < 0.06}


def case_framing_report() -> dict:
    """The reported frameCx must be the centre the renderer will actually use.

    A stored centre outside the crop window is silently clamped at render time.
    If the API echoed the raw value the slider would claim a framing the clip
    never got, so the plan has to report the clamped one — and report it inside
    the [cxMin, cxMax] window it publishes alongside.
    """
    payload = _payload(
        duration=4.0,
        cx=[0.5] * 24,
        shots=[
            # Far outside any 9:16 crop window: 0.99 cannot be centred.
            {"index": 1, "start": 0.0, "end": 2.0, "kind": "follow_speaker",
             "zoom": 1.0, "frameCx": 0.99},
            # A centre that is already legal must survive untouched.
            {"index": 2, "start": 2.0, "end": 4.0, "kind": "follow_speaker",
             "zoom": 1.0, "frameCx": 0.5},
        ],
    )
    plan = shot_plan.build(payload, tracking_config.resolve("podcast"),
                           src_w=1920, src_h=1080, out_w=1080, out_h=1920)
    by_index = {shot["index"]: shot for shot in plan["shots"]}
    wild, legal = by_index[1], by_index[2]
    checks = {
        "wild centre clamped to cxMax": abs(wild["frameCx"] - wild["cxMax"]) < 1e-6,
        "clamped centre inside window": wild["cxMin"] - 1e-9 <= wild["frameCx"] <= wild["cxMax"] + 1e-9,
        "legal centre untouched": abs(legal["frameCx"] - 0.5) < 1e-6,
        "window narrower than half the frame": wild["cxMax"] < 0.95,
    }
    return {"case": "framing_report", "shots": {"1": wild, "2": legal},
            "checks": checks, "ok": all(checks.values())}


def main() -> int:
    report = {"tmp": str(TMP), "cases": []}
    for case in (case_draft, case_whole_frame, case_cut, case_zoom, case_framing_report):
        try:
            report["cases"].append(case())
        except Exception as exc:  # noqa: BLE001
            report["cases"].append({"case": case.__name__, "ok": False, "error": str(exc)[:600]})
    report["ok"] = all(case.get("ok") for case in report["cases"])
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
