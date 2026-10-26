"""Detection timeline: see what the pipeline heard and decided, against the video.

Renders one wide PNG per clip window with every signal that drives a cut on the
same time axis, plus an optional contact sheet of the frames around each cut so
the numbers can be checked against the actual footage.

    backend/.venv/bin/python -m backend.scripts.timeline data/media/clip.mp4 \
        --start 50 --end 90 --type podcast --out /tmp/timeline.png --contact /tmp/cuts.png

Lanes, top to bottom:
    shots        one block per shot (blue = follow_speaker, orange = whole_frame)
    voice        VAD speech mask (the gate the draft uses)
    A / B / C    per-person mouth activity (talk matrix)
    subject      which person the crop follows
    together     how many people are talking at once (>= 2 = overlap)

White vertical lines are cuts; the header carries the summary and the VAD
backend, so a run with a different `vad_backend` is comparable side by side.

The payload comes from `face_service.detect`, i.e. the cached production path —
not a re-run — so the timeline shows exactly what a render would do.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

from backend.services import face_service
from backend.services.shots import draft as shot_draft
from backend.services.tracking import config as tracking_config

BG = (24, 24, 24)
LANE = (44, 44, 44)
GRID = (70, 70, 70)
TEXT = (235, 235, 235)
DIM = (150, 150, 150)
FOLLOW = (200, 140, 60)
WHOLE = (40, 160, 255)
VOICE = (90, 220, 220)
MOUTH = (120, 210, 120)
SUBJECT = (80, 220, 80)
CUT = (255, 255, 255)
OVERLAP = (60, 90, 235)


def _text(image, text, x, y, colour=TEXT, scale=0.42, thickness=1):
    cv2.putText(image, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, colour, thickness, cv2.LINE_AA)


def _lane_rect(image, left, right, y, height, colour, alpha=1.0):
    if right <= left:
        right = left + 1
    if alpha >= 1.0:
        cv2.rectangle(image, (left, y), (right, y + height), colour, -1)
        return
    overlay = image.copy()
    cv2.rectangle(overlay, (left, y), (right, y + height), colour, -1)
    cv2.addWeighted(overlay, alpha, image, 1 - alpha, 0, image)


def _nice_step(span: float) -> float:
    for step in (1, 2, 5, 10, 15, 30, 60, 120, 300):
        if span / step <= 14:
            return float(step)
    return float(max(1, int(span / 12)))


def render(
    *,
    payload: dict,
    start: float,
    end: float,
    clip_type: str,
    out_path: Path,
    width: int = 1800,
) -> dict:
    span = max(0.2, end - start)
    trajectory = payload.get("trajectory") or []
    if isinstance(trajectory, dict):
        times = np.asarray(trajectory.get("t") or [], dtype=float)
    else:
        times = np.asarray([float(item.get("t", 0.0)) for item in trajectory], dtype=float)
    activity = payload.get("activity") or {}
    speech = np.asarray(activity.get("speech") or [], dtype=int)
    target = np.asarray(activity.get("target") or [], dtype=int)
    overlap = np.asarray(activity.get("overlap") or [], dtype=int)
    talk = np.asarray(activity.get("talk") or [], dtype=int)
    if talk.ndim == 1:
        talk = talk[None, :]
    persons = int(talk.shape[0]) if talk.size else 0
    shots = payload.get("shots") or []
    metrics = payload.get("shot_metrics") or {}
    vad_info = payload.get("vad") or {}

    labels = [chr(ord("A") + i) if i < 26 else str(i) for i in range(persons)]
    cuts = [float(shot["start"]) for shot in shots[1:]]

    lanes: list[tuple[str, int]] = [("shots", 30), ("voice", 20)]
    lanes += [(f"mouth {labels[i]}", 16) for i in range(min(4, persons))]
    lanes += [("subject", 30), ("together", 22)]

    header = 62
    axis = 30
    left_pad = 92
    height = header + sum(size for _, size in lanes) + axis
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:] = BG

    plot_w = width - left_pad - 16

    def px(t: float) -> int:
        return left_pad + int(np.clip((t - start) / span, 0.0, 1.0) * plot_w)

    y = header
    lane_y: dict[str, int] = {}
    for name, size in lanes:
        lane_y[name] = y
        cv2.rectangle(image, (left_pad, y), (left_pad + plot_w, y + size), LANE, -1)
        _text(image, name, 8, y + size - 6, DIM, 0.4)
        y += size + 2

    # shots
    for shot in shots:
        lo, hi = float(shot["start"]), float(shot["end"])
        colour = WHOLE if shot.get("kind") == "whole_frame" else FOLLOW
        _lane_rect(image, px(lo), px(hi), lane_y["shots"], 28, colour)
        label = f"{shot['index']} {'WF' if shot.get('kind') == 'whole_frame' else 'follow'}"
        _text(image, label, px(lo) + 4, lane_y["shots"] + 19, (16, 16, 16), 0.4, 2)

    # voice activity
    for index in range(times.size):
        if speech.size > index and speech[index]:
            _lane_rect(image, px(times[index]), px(times[index]) + 2, lane_y["voice"], 18, VOICE)

    # per-person mouth
    for face in range(min(4, persons)):
        key = f"mouth {labels[face]}"
        for index in range(times.size):
            if talk[face][index]:
                _lane_rect(image, px(times[index]), px(times[index]) + 2, lane_y[key], 14, MOUTH)

    # subject
    for index in range(times.size):
        if target.size <= index or target[index] < 0:
            continue
        value = int(target[index])
        top = lane_y["subject"] + 26 - int(24 * (value + 1) / max(1, persons))
        cv2.rectangle(
            image, (px(times[index]), top), (px(times[index]) + 2, lane_y["subject"] + 27), SUBJECT, -1
        )

    # overlap count
    for index in range(times.size):
        count = int(overlap[index]) if overlap.size > index else 0
        if count <= 0:
            continue
        bar = min(20, int(6 * count))
        colour = OVERLAP if count >= 2 else DIM
        cv2.rectangle(
            image,
            (px(times[index]), lane_y["together"] + 20 - bar),
            (px(times[index]) + 2, lane_y["together"] + 20),
            colour,
            -1,
        )

    for span_lo, span_hi in metrics.get("overlap_spans") or []:
        lo = px(start + float(span_lo) if span_lo < start else float(span_lo))
        hi = px(start + float(span_hi) if span_hi < start else float(span_hi))
        cv2.rectangle(image, (lo, header - 2), (hi, height - axis + 2), OVERLAP, 1)

    for cut in cuts:
        x = px(cut)
        cv2.line(image, (x, header - 6), (x, height - axis), CUT, 2)

    # axis
    axis_y = height - axis + 4
    step = _nice_step(span)
    tick = start - (start % step)
    while tick <= end + 1e-6:
        if tick >= start:
            x = px(tick)
            cv2.line(image, (x, axis_y), (x, axis_y + 6), GRID, 1)
            _text(image, f"{tick:.0f}s", x - 10, axis_y + 20, DIM, 0.38)
        tick += step

    # header
    _text(image, f"{Path(payload.get('source') or '').name or 'clip'}  {clip_type}  "
                f"{start:.1f}-{end:.1f}s ({span:.1f}s)", 10, 20, TEXT, 0.55, 2)
    vad_line = "vad -"
    if vad_info:
        vad_line = (
            f"vad {vad_info.get('backend')}  speech {float(vad_info.get('speech_ratio') or 0) * 100:.0f}%  "
            f"enter {vad_info.get('enter_db')} dB  exit {vad_info.get('exit_db')} dB"
        )
    _text(image, vad_line, 10, 40, VOICE, 0.42)
    _text(
        image,
        f"people {persons}  shots {len(shots)}  cuts {len(cuts)} "
        f"({metrics.get('cuts_per_minute')} /min)  whole_frame {metrics.get('whole_frame_shots', 0)} "
        f"({metrics.get('whole_frame_seconds', 0)}s)  "
        f"gate {'speech' if metrics.get('speech_gate') else 'mouth'}",
        10,
        56,
        TEXT,
        0.42,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), image)
    return {
        "out": str(out_path),
        "span": [start, end],
        "persons": persons,
        "shots": [
            {
                "index": shot["index"],
                "start": shot["start"],
                "end": shot["end"],
                "kind": shot["kind"],
                "zoom": shot.get("zoom"),
                "source": shot.get("source"),
            }
            for shot in shots
        ],
        "cuts": cuts,
        "cuts_per_minute": metrics.get("cuts_per_minute"),
        "overlap_spans": metrics.get("overlap_spans"),
        "whole_frame_shots": metrics.get("whole_frame_shots"),
        "vad": vad_info,
    }


def contact_sheet(
    *,
    source: Path,
    cuts: list[float],
    out_path: Path,
    before: float = 0.4,
    after: float = 0.4,
    tile_w: int = 320,
) -> str | None:
    """Frames either side of every cut, so the cut can be checked by eye."""
    if not cuts:
        return None
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        return None
    tiles: list[np.ndarray] = []
    for cut in cuts[:8]:
        for offset in (-before, after):
            capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, cut + offset) * 1000.0)
            ok, frame = capture.read()
            if not ok:
                continue
            scale = tile_w / frame.shape[1]
            frame = cv2.resize(frame, (tile_w, int(frame.shape[0] * scale)))
            cv2.rectangle(frame, (0, 0), (frame.shape[1] - 1, frame.shape[0] - 1), BG, 2)
            tag = f"{'before' if offset < 0 else 'after'} {cut + offset:.2f}s"
            cv2.putText(frame, tag, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
            cv2.putText(frame, tag, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, TEXT, 1, cv2.LINE_AA)
            tiles.append(frame)
    capture.release()
    if not tiles:
        return None
    rows = []
    for index in range(0, len(tiles), 4):
        row = tiles[index : index + 4]
        height = max(tile.shape[0] for tile in row)
        padded = [
            cv2.copyMakeBorder(tile, 0, height - tile.shape[0], 0, 0, cv2.BORDER_CONSTANT, value=BG)
            for tile in row
        ]
        while len(padded) < 4:
            padded.append(np.zeros((height, tile_w, 3), dtype=np.uint8))
        rows.append(np.hstack(padded))
    sheet = np.vstack(rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), sheet)
    return str(out_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="detection timeline")
    parser.add_argument("video")
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--end", type=float, default=0.0)
    parser.add_argument("--type", default="podcast")
    parser.add_argument("--out", default="")
    parser.add_argument("--json", default="")
    parser.add_argument("--contact", default="")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args(argv)

    source = Path(args.video).resolve()
    start = max(0.0, args.start)
    end = args.end if args.end > start else start + 60.0
    payload = face_service.detect(source, start, end, clip_type=args.type, use_cache=not args.no_cache)
    payload["source"] = str(source)

    out = Path(args.out) if args.out else Path(f"/tmp/timeline-{start:.0f}-{end:.0f}.png")
    report = render(payload=payload, start=start, end=end, clip_type=args.type, out_path=out)

    if args.contact:
        sheet = contact_sheet(source=source, cuts=report["cuts"], out_path=Path(args.contact))
        report["contact"] = sheet

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
