"""Debug overlay: watch exactly what the tracker and the shot list decided.

Re-runs the stages for one clip window and writes an annotated video at the
detection rate: every face box with its speaker label (A, B, C …) and mouth
state, the jaw-open ratio that drove the speaker choice, the chosen subject, the
smoothed crop window the renderer will follow, the cut points, the whole_frame
spans, and a timeline strip along the bottom with the voice-activity band.

Nothing here is used by the render path — it exists so a two-person dialogue can
be checked by eye in seconds, and so a cut that lands on the wrong frame is
obvious instead of subtle.

    backend/.venv/bin/python -m backend.scripts.tracking_debug VIDEO OUT.mp4 \
        --start 0 --end 20 --type podcast

Colours: grey = other faces, green = chosen subject, yellow = crop window,
red = raw (unsmoothed) crop, white = cut marker, orange = whole_frame.

Timeline strip (bottom): blue blocks are follow_speaker shots, orange blocks are
whole_frame shots, white ticks are cuts, the yellow band is the VAD speech mask,
the thin lanes are per-person mouth activity, the vertical line is "now".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

from backend.services.shots import draft as shot_draft
from backend.services.shots import plan as shot_plan
from backend.services.tracking import config as tracking_config
from backend.services.tracking import detector as detector_mod
from backend.services.tracking import sampler, speaker, tracker, trajectory, vad
from backend.services.video_engine import _crop_box

CUT_COLOUR = (255, 255, 255)
CHOSEN = (80, 220, 80)
OTHER = (170, 170, 170)
CROP = (60, 220, 240)
RAW = (70, 70, 230)
WHOLE = (40, 160, 255)
STRIP_BG = (28, 28, 28)
STRIP_FOLLOW = (200, 140, 60)
STRIP_WHOLE = (40, 160, 255)
STRIP_SPEECH = (90, 220, 220)
STRIP_PLAYHEAD = (255, 255, 255)


def _put(image: np.ndarray, text: str, y: int, colour=CHOSEN, scale: float = 0.5) -> None:
    cv2.putText(image, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(image, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, scale, colour, 1, cv2.LINE_AA)


def _banner(image: np.ndarray, text: str, colour, y_ratio: float = 0.5, scale: float = 1.3) -> None:
    height, width = image.shape[:2]
    size = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)[0]
    x = max(4, (width - size[0]) // 2)
    y = int(height * y_ratio)
    cv2.putText(image, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), 6, cv2.LINE_AA)
    cv2.putText(image, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, colour, 2, cv2.LINE_AA)


def _timeline(
    image: np.ndarray,
    *,
    span: float,
    shots: list[dict],
    cuts: list[float],
    spans: list[tuple[float, float]],
    times: np.ndarray,
    speech: np.ndarray | None,
    talk: np.ndarray | None,
    labels: list[str],
    now: float,
) -> None:
    """Shot blocks + speech band + per-person lanes along the bottom edge."""
    height, width = image.shape[:2]
    lanes = 1 + (1 if speech is not None else 0) + (min(3, talk.shape[0]) if talk is not None else 0)
    strip_h = 14 * lanes + 8
    top = height - strip_h
    if top <= 2:
        return
    overlay = image.copy()
    cv2.rectangle(overlay, (0, top), (width, height), STRIP_BG, -1)
    cv2.addWeighted(overlay, 0.85, image, 0.15, 0, image)

    def px(t: float) -> int:
        return int(np.clip(t / max(span, 1e-6), 0.0, 1.0) * (width - 1))

    for shot in shots:
        left = px(float(shot["_rel_start"]))
        right = max(left + 1, px(float(shot["_rel_end"])))
        colour = STRIP_WHOLE if shot["kind"] == "whole_frame" else STRIP_FOLLOW
        cv2.rectangle(image, (left, top + 2), (right, top + 12), colour, -1)
        cv2.putText(
            image,
            f"{shot['index']}{'W' if shot['kind'] == 'whole_frame' else ''}",
            (left + 2, top + 11),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.32,
            (12, 12, 12),
            1,
            cv2.LINE_AA,
        )

    row = top + 14
    if speech is not None and speech.size:
        for index in range(0, speech.size, 2):
            if not speech[index]:
                continue
            cv2.line(image, (px(times[index]), row + 4), (px(times[index]), row + 10), STRIP_SPEECH, 2)
        row += 14
    if talk is not None:
        for face in range(min(3, talk.shape[0])):
            for index in range(0, times.size, 2):
                if not talk[face][index]:
                    continue
                cv2.line(
                    image,
                    (px(times[index]), row + 3),
                    (px(times[index]), row + 9),
                    CHOSEN if face == 0 else OTHER,
                    2,
                )
            cv2.putText(
                image,
                labels[face] if face < len(labels) else str(face),
                (2, row + 9),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.3,
                (235, 235, 235),
                1,
                cv2.LINE_AA,
            )
            row += 13

    for lo, hi in spans:
        cv2.rectangle(image, (px(lo), top + 1), (px(hi), top + 13), WHOLE, 1)
    for cut in cuts:
        cv2.line(image, (px(cut), top), (px(cut), height - 1), CUT_COLOUR, 1)
    cv2.line(image, (px(now), top), (px(now), height - 1), STRIP_PLAYHEAD, 2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="tracking debug overlay")
    parser.add_argument("video")
    parser.add_argument("out")
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--end", type=float, default=0.0, help="0 = whole file (capped at 60 s)")
    parser.add_argument("--type", default="talking_head")
    parser.add_argument("--no-vad", action="store_true", help="skip voice activity (faster)")
    args = parser.parse_args(argv)

    source = Path(args.video)
    info = sampler.probe_video(source)
    duration = float(info["duration"])
    start = max(0.0, args.start)
    end = args.end if args.end > start else min(duration, start + 60.0)

    detector = detector_mod.load_detector()
    if detector is None:
        print("no detector available", file=sys.stderr)
        return 1

    params = tracking_config.resolve(args.type)
    frames = sampler.sample_frames(
        source,
        start,
        end,
        sample_fps=float(params["detect_fps"]),
        target_width=int(params["analysis_width"]),
    )
    interval = max(1, int(params["detect_interval"]))
    detections = [
        (frame.t, detector.detect(frame.image))
        for position, frame in enumerate(frames)
        if position % interval == 0
    ]
    tracks = tracker.build_tracks(
        detections,
        iou_threshold=float(params["iou_threshold"]),
        max_age=int(params["max_track_age"]),
    )

    span = max(0.2, end - start)
    times = np.linspace(0.0, span, max(2, int(round(span * float(params["expr_hz"]))) + 1))
    hold = float(params["hold_frames"]) / max(0.5, float(params["detect_fps"]))
    series = tracker.dense_series(tracks, times, hold_seconds=hold)
    decision = speaker.choose_target(series, times, params)
    built = trajectory.build(
        series, times, decision, params, src_w=int(info["width"]), src_h=int(info["height"])
    )

    labels = [chr(ord("A") + index) if index < 26 else str(index) for index in range(len(series))]
    talk_matrix = getattr(decision, "talk", None)

    # Voice activity — the exact mask the shot draft will see.
    speech = None
    vad_info: dict = {}
    if not args.no_vad:
        try:
            result = vad.detect(str(source), start=start, end=end, params=params)
            speech = result.at(times, tolerance=float(params["vad_turn_tolerance"]))
            vad_info = result.to_dict()
        except Exception as exc:  # the overlay must still render without audio
            print(f"vad failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    # Tahap 7: the renderer follows the shot plan, so build the same draft the
    # pipeline builds (per-person talk matrix + speech gate + overlap) and hand
    # it to the plan. Anything else would debug a shot list nobody renders.
    shot_list, shot_stats = shot_draft.build_draft(
        times,
        decision.target,
        talk_matrix if talk_matrix is not None else decision.talking,
        params,
        start=start,
        end=end,
        person_labels=labels,
        speech=speech,
        overlap=getattr(decision, "overlap", None),
    )

    payload = dict(built)
    payload.update(
        start=start,
        end=end,
        duration=span,
        clip_type=args.type,
        shots=[shot.to_dict() for shot in shot_list],
        metrics=dict(built["metrics"], src_size=[int(info["width"]), int(info["height"])]),
        activity={
            **built.get("activity", {}),
            "speech": (speech.astype(np.int8).tolist() if speech is not None else []),
        },
    )
    plan = shot_plan.build(
        payload, params, src_w=int(info["width"]), src_h=int(info["height"]), out_w=540, out_h=960
    )
    points = plan["points"]
    plan_shots = plan["shots"]
    plan_metrics = plan["metrics"]

    for shot in plan_shots:  # strip needs clip-relative times
        shot["_rel_start"] = max(0.0, float(shot["start"]) - start)
        shot["_rel_end"] = min(span, float(shot["end"]) - start)

    # The renderer crops a window whose aspect follows the output (9:16 by default),
    # so the window is wider than the face: reuse its own maths instead of guessing.
    crop_px_w, crop_px_h = _crop_box(540, 960, int(info["width"]), int(info["height"]))
    crop_w = crop_px_w / float(info["width"])
    crop_h = crop_px_h / float(info["height"])

    height, width = frames[0].image.shape[:2]
    writer = cv2.VideoWriter(
        str(args.out), cv2.VideoWriter_fourcc(*"mp4v"), float(params["detect_fps"]), (width, height)
    )

    print(
        f"{args.type}: {len(tracks)} tracks ({', '.join(labels) or '-'}), {len(points)} path points, "
        f"{decision.switches} switches, jitter {plan_metrics['jitter_raw_pct']}% → "
        f"{plan_metrics['jitter_smoothed_pct']}%, shots {plan_metrics['shots']} "
        f"({plan_metrics['cuts']} cuts, {plan_metrics['cuts_per_minute']}/min), "
        f"zoom {plan_metrics['zoom_min']}-{plan_metrics['zoom_max']}"
    )
    if vad_info:
        # The energy backend gates on dB, Silero on probability; report whichever applies.
        if "prob_threshold" in vad_info:
            gate = f"threshold p>{vad_info['prob_threshold']}, release p>{vad_info.get('prob_release', '-')}"
        else:
            gate = f"threshold {vad_info.get('threshold_db')} dB"
        print(f"  vad {vad_info['backend']}: speech {vad_info['speech_ratio'] * 100:.0f}% ({gate})")
    print(
        f"  whole_frame {shot_stats.get('whole_frame_shots', 0)} shot(s), "
        f"overlap spans {shot_stats.get('overlap_spans')}"
    )
    for shot in plan_shots:
        print(
            f"  shot {shot['index']}: {shot['start']:.2f}-{shot['end']:.2f}s "
            f"{shot['kind']} zoom {shot['zoom_start']:.2f}→{shot['zoom_end']:.2f} "
            f"({shot['source']}{', locked' if shot.get('locked') else ''})"
        )

    cut_marks = sorted(float(shot["start"]) for shot in plan_shots[1:])
    cut_times = sorted({round(mark - start, 3) for mark in cut_marks})
    overlap_spans = [(float(a), float(b)) for a, b in (shot_stats.get("overlap_spans") or [])]

    for frame in frames:
        image = frame.image.copy()
        step = int(round(frame.t / span * (times.size - 1))) if span > 0 else 0
        step = min(max(step, 0), times.size - 1)
        chosen = int(decision.target[step])
        absolute = start + frame.t

        shot_now = None
        for shot in plan_shots:
            if float(shot["start"]) - 1e-6 <= absolute < float(shot["end"]) + 1e-6:
                shot_now = shot
                break
        whole_now = bool(shot_now and shot_now["kind"] == "whole_frame")

        for index, item in enumerate(series):
            if not item.valid[step]:
                continue
            cx, cy = item.cx[step], item.cy[step]
            w, h = item.w[step], item.h[step]
            box = (
                int((cx - w / 2) * width),
                int((cy - h / 2) * height),
                int(w * width),
                int(h * height),
            )
            speaking = bool(talk_matrix[index][step]) if talk_matrix is not None else False
            active = index == chosen
            colour = CHOSEN if active else OTHER
            cv2.rectangle(
                image, (box[0], box[1]), (box[0] + box[2], box[1] + box[3]), colour,
                3 if (active and speaking) else 2,
            )
            tag = labels[index] if index < len(labels) else f"#{item.id}"
            text = f"{tag} {'TALK' if speaking else 'silent'} jaw {item.jaw[step]:.2f}"
            cv2.putText(
                image, text, (box[0], max(12, box[1] - 6)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3, cv2.LINE_AA,
            )
            cv2.putText(
                image, text, (box[0], max(12, box[1] - 6)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1, cv2.LINE_AA,
            )

        zoom = 1.0
        cut_here = False
        if points:
            point = points[min(step, len(points) - 1)]
            x = float(point["x"])
            y = float(point["y"])
            zoom = float(point.get("z") or 1.0)
            cut_here = bool(point.get("cut"))
            # Zoom is applied by scaling the cropped frame up and cropping back,
            # so the region actually on screen is the crop window divided by z.
            half_w = crop_w / 2.0 / zoom
            half_h = crop_h / 2.0 / zoom
            left = int(np.clip(x - half_w, 0.0, 1.0 - crop_w / zoom) * width)
            top = int(np.clip(y - half_h, 0.0, 1.0 - crop_h / zoom) * height)
            cv2.rectangle(
                image,
                (left, top),
                (left + int(crop_w / zoom * width), top + int(crop_h / zoom * height)),
                CROP,
                2,
            )
            raw_x = series[chosen].cx[step] if chosen >= 0 else x
            raw_left = int(np.clip(raw_x - crop_w / 2.0, 0.0, 1.0 - crop_w) * width)
            cv2.rectangle(
                image, (raw_left, top), (raw_left + int(crop_w * width), top + int(crop_h * height)), RAW, 1
            )

        if whole_now:
            cv2.rectangle(image, (2, 2), (width - 3, height - 3), WHOLE, 4)
            _banner(image, "WHOLE FRAME", WHOLE, 0.32, 1.0)

        if cut_here:
            cv2.line(image, (0, 0), (width, height), CUT_COLOUR, 3)

        # A cut lives on a single frame, which is impossible to catch by eye at
        # 10 fps. Keep the banner up for half a second after the cut and show a
        # countdown before the next one, so a wrong cut is visible while playing.
        recent = [c for c in cut_marks if 0.0 <= absolute - c <= 0.5]
        if recent:
            following = next(
                (shot for shot in plan_shots if abs(float(shot["start"]) - recent[-1]) < 0.6), None
            )
            into = "-"
            if following is not None:
                subject = labels[chosen] if 0 <= chosen < len(labels) else "-"
                into = f"{following['kind']} {subject}".strip()
            _banner(image, f"CUT -> {into}", CUT_COLOUR, 0.5, 1.0)

        talking = bool(decision.talking[step]) if decision.talking is not None else False
        overlap = int(decision.overlap[step]) if decision.overlap is not None else 0
        speech_now = bool(speech[step]) if speech is not None and speech.size else False
        subject = labels[chosen] if 0 <= chosen < len(labels) else "-"

        _put(
            image,
            f"t={frame.t:5.2f}s  {args.type}  crop {crop_px_w}x{crop_px_h}px of "
            f"{int(info['width'])}x{int(info['height'])}",
            18,
        )
        _put(
            image,
            f"subject {subject}  switches {decision.switches}  "
            f"voice {'on' if speech_now else 'off'}"
            f"{'  OVERLAP x%d' % overlap if overlap >= 2 else ''}",
            38,
            CROP,
        )
        upcoming = [c for c in cut_marks if c > absolute]
        when = (
            f"next cut in {upcoming[0] - absolute:.1f}s"
            if upcoming
            else "no cut left"
        )
        _put(
            image,
            f"shot {shot_now['index'] if shot_now else '-'} "
            f"{shot_now['kind'] if shot_now else ''}  zoom {zoom:.2f}x  "
            f"{'TALK' if talking else 'silent'}  people talking {overlap}  {when}",
            58,
            CHOSEN if talking else OTHER,
        )

        _timeline(
            image,
            span=span,
            shots=plan_shots,
            cuts=cut_times,
            spans=overlap_spans,
            times=times,
            speech=speech,
            talk=talk_matrix,
            labels=labels,
            now=frame.t,
        )
        writer.write(image)

    writer.release()
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
