"""Tracking benchmark (Phase 6).

Runs the OpenCV + MediaPipe pipeline on a set of videos and prints a table of
smoothness metrics so the effect of the smoothing stage can be judged:

    jitter_raw      mean |Δx| per step of the *raw* detected centre (% of width)
    jitter_smoothed the same after dead zone + One Euro + Savitzky-Golay
    jitter_cut      relative reduction in %
    jumps_*         steps that move more than 2% of the width (visible snaps)

Usage (from the repo root):

    backend/.venv/bin/python -m backend.scripts.tracking_bench VIDEO [VIDEO...]
    backend/.venv/bin/python -m backend.scripts.tracking_bench --types talking_head,podcast \\
        /tmp/track_tests/t2_two.mp4

Every clip type resolves its own preset (smoothing / dead zone / hold frames /
speaker switch delay); the values used are printed with the results.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from backend.services import edit_service
from backend.services.tracking import analyze
from backend.services.tracking import config as tracking_config


def _fmt(value: object, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def run(videos: list[Path], types: list[str], start: float, end: float | None) -> list[dict]:
    rows: list[dict] = []
    for video in videos:
        for clip_type in types:
            span_end = end if end is not None else _duration(video)
            result = analyze(video, start, span_end, clip_type, use_cache=False)
            metrics = result.get("metrics") or {}
            row = {
                "video": video.name,
                "clip_type": clip_type,
                "faces": result.get("faces"),
                "coverage": result.get("coverage"),
                "hit_ratio": result.get("hit_ratio"),
                "points": len(result.get("trajectory") or []),
                "jitter_raw_pct": metrics.get("jitter_raw_pct"),
                "jitter_smoothed_pct": metrics.get("jitter_smoothed_pct"),
                "jitter_cut_pct": metrics.get("jitter_cut_pct"),
                "peak_raw_pct": metrics.get("peak_raw_pct"),
                "peak_smoothed_pct": metrics.get("peak_smoothed_pct"),
                "jumps_raw": metrics.get("jumps_raw"),
                "jumps_smoothed": metrics.get("jumps_smoothed"),
                "speaker_switches": metrics.get("speaker_switches"),
                "elapsed_ms": result.get("elapsed_ms"),
                "params": {
                    key: result.get("params", {}).get(key)
                    for key in (
                        "detect_fps",
                        "detect_interval",
                        "dead_zone",
                        "hold_frames",
                        "max_speed",
                        "speaker_min_switch",
                        "layout",
                    )
                },
            }
            plan = edit_service.face_plan(result, width=1080, height=1920)
            if plan:
                metrics = plan["metrics"]
                row["plan"] = {
                    key: metrics.get(key)
                    for key in (
                        "shots",
                        "cuts",
                        "cuts_per_minute",
                        "avg_shot_seconds",
                        "min_shot_seconds",
                        "jitter_raw_pct",
                        "jitter_smoothed_pct",
                        "jitter_cut_pct",
                        "cut_jumps_pct",
                        "zoom_min",
                        "zoom_max",
                        "zoom_mean",
                        "zoom_step_max",
                        "zoom_speed_max",
                        "zoom_turns",
                        "steps",
                    )
                }
            rows.append(row)
            print(
                f"{row['video']:<16} {clip_type:<13} faces={_fmt(row['faces']):>2} "
                f"cov={_fmt(row['coverage'], 2):>5} jitter {_fmt(row['jitter_raw_pct']):>6}% → "
                f"{_fmt(row['jitter_smoothed_pct']):>6}% ({_fmt(row['jitter_cut_pct'], 0):>4}%) "
                f"peak {_fmt(row['peak_raw_pct'], 1):>5}% → {_fmt(row['peak_smoothed_pct'], 1):>5}% "
                f"jumps {_fmt(row['jumps_raw']):>3} → {_fmt(row['jumps_smoothed']):>3} "
                f"switch={_fmt(row['speaker_switches']):>3} pts={row['points']:>4} "
                f"{row['elapsed_ms']}ms",
                flush=True,
            )
            plan_row = row.get("plan")
            if plan_row:
                print(
                    f"{'':<16} {'':<13} shots={_fmt(plan_row['shots']):>2} "
                    f"cuts={_fmt(plan_row['cuts']):>2} ({_fmt(plan_row['cuts_per_minute'], 1):>4}/min) "
                    f"len min/avg {_fmt(plan_row['min_shot_seconds'], 1):>4}/{_fmt(plan_row['avg_shot_seconds'], 1):>4}s "
                    f"jitter {_fmt(plan_row['jitter_raw_pct']):>6}% → {_fmt(plan_row['jitter_smoothed_pct']):>6}% "
                    f"cut-jump max {_fmt(max(plan_row['cut_jumps_pct'] or [0]), 1):>5}% "
                    f"zoom {_fmt(plan_row['zoom_min'], 2)}-{_fmt(plan_row['zoom_max'], 2)} "
                    f"(avg {_fmt(plan_row['zoom_mean'], 2)}, step {_fmt(plan_row['zoom_step_max'], 3)} "
                    f"= {_fmt(plan_row['zoom_speed_max'], 2)}/s, turns {_fmt(plan_row['zoom_turns']):>2})",
                    flush=True,
                )
    return rows


def _duration(video: Path) -> float:
    import cv2

    cap = cv2.VideoCapture(str(video))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
    finally:
        cap.release()
    return (frames / fps) if fps else 0.0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Face-tracking smoothness benchmark")
    parser.add_argument("videos", nargs="+", type=Path)
    parser.add_argument(
        "--types",
        default="talking_head",
        help=f"comma separated clip types ({', '.join(tracking_config.CLIP_TYPES)})",
    )
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--end", type=float, default=None)
    parser.add_argument("--json", type=Path, default=None, help="also write raw rows to this file")
    args = parser.parse_args(argv)

    missing = [str(v) for v in args.videos if not v.exists()]
    if missing:
        print(f"missing video: {', '.join(missing)}", file=sys.stderr)
        return 2

    types = [item.strip() for item in args.types.split(",") if item.strip()]
    unknown = [item for item in types if item not in tracking_config.CLIP_TYPES]
    if unknown:
        print(f"unknown clip type(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    rows = run(args.videos, types, args.start, args.end)
    if args.json:
        args.json.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
