"""Compare VAD backends on real footage against Whisper word timings.

One reference per window (Whisper words), N backends scored on the same grid
reference, plus wall-clock cost. Whisper is not ground truth, but a word it heard
is certainly speech and a >0.5 s gap between words is very likely silence, which
is enough to compare two detectors against each other.

    backend/.venv/bin/python -m backend.scripts.vad_compare \
        --src data/media/W7Q_5U2YeI8.mp4 --windows 50:90,90:130,600:640 \
        --backends energy,silero --json /tmp/vad_compare.json
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path

import numpy as np

from backend.services import whisper_service
from backend.services.tracking import config as tracking_config
from backend.services.tracking import vad

from .vad_eval import PAD, _extract


def score(result: vad.VadResult, words: list[tuple[float, float]], window: float) -> dict:
    speech = result.speech
    hop = result.hop
    reference = np.zeros(speech.size, dtype=bool)
    padded = np.zeros(speech.size, dtype=bool)
    for lo, hi in words:
        reference[max(0, int(lo / hop)) : max(0, int(hi / hop))] = True
        padded[max(0, int(max(0.0, lo - PAD) / hop)) : int((hi + PAD) / hop)] = True

    tp = int((speech & reference).sum())
    fp = int((speech & ~padded).sum())
    fn = int((~speech & reference).sum())
    return {
        "backend": result.backend,
        "hop": round(hop, 4),
        "frames": int(speech.size),
        "speech_ratio": round(result.speech_ratio, 3),
        "precision": round(tp / max(tp + fp, 1), 3),
        "recall": round(tp / max(tp + fn, 1), 3),
        "f1": round(2 * tp / max(2 * tp + fp + fn, 1), 3),
        "fp_seconds": round(fp * hop, 2),
        "fn_seconds": round(fn * hop, 2),
        "fp_per_minute": round(fp * hop / window * 60, 1),
        "fn_per_minute": round(fn * hop / window * 60, 1),
        "word_centre_hit": round(
            float(result.at(np.array([(a + b) / 2 for a, b in words])).mean()), 3
        ),
        "spans": len(result.spans(min_gap=0.25, min_length=0.2)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", required=True)
    parser.add_argument("--windows", required=True, help="start:end,start:end,…")
    parser.add_argument("--backends", default="energy,silero")
    parser.add_argument("--clip-type", default="podcast")
    parser.add_argument("--model", default=None, help="whisper size override")
    parser.add_argument("--json", default=None, help="write the full report here")
    args = parser.parse_args(argv)

    params = tracking_config.resolve(args.clip_type)
    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    rows: list[dict] = []

    for chunk in args.windows.split(","):
        start, end = (float(x) for x in chunk.split(":"))
        window = end - start
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "window.wav"
            _extract(args.src, start, end, wav)
            samples = vad.load_audio(str(wav))

            t0 = time.time()
            transcript = whisper_service.transcribe(str(wav), model_size=args.model)
            whisper_seconds = time.time() - t0
            words = [
                (float(w["start"]), float(w["end"]))
                for seg in transcript.get("segments", [])
                for w in (seg.get("words") or [])
            ]

        if not words:
            print(f"[{start:.0f}-{end:.0f}s] whisper produced no words, skipped")
            continue

        print(f"\n[{start:.0f}-{end:.0f}s]  whisper {len(words)} words in {whisper_seconds:.1f}s")
        print(f"  {'backend':8} {'hop':>6} {'P':>6} {'R':>6} {'F1':>6} {'FP/min':>7} {'FN/min':>7} {'ratio':>6} {'sec':>6}")
        for name in backends:
            run_params = dict(params)
            run_params["vad_backend"] = name
            engine = vad.get_vad(run_params)
            t1 = time.time()
            result = engine.run(samples, vad.SAMPLE_RATE)
            seconds = time.time() - t1
            row = score(result, words, window)
            row.update(
                {
                    "src": Path(args.src).name,
                    "window": [start, end],
                    "whisper_words": len(words),
                    "whisper_seconds": round(whisper_seconds, 2),
                    "vad_seconds": round(seconds, 2),
                    "realtime_factor": round(window / max(seconds, 1e-6), 1),
                }
            )
            rows.append(row)
            print(
                f"  {row['backend']:8} {row['hop']:6.3f} {row['precision']:6.3f} {row['recall']:6.3f}"
                f" {row['f1']:6.3f} {row['fp_per_minute']:7.1f} {row['fn_per_minute']:7.1f}"
                f" {row['speech_ratio']:6.3f} {row['vad_seconds']:6.2f}"
            )

    if rows:
        print("\nmean over windows")
        for name in backends:
            picked = [r for r in rows if r["backend"] == name]
            if not picked:
                continue
            mean = lambda key: sum(r[key] for r in picked) / len(picked)  # noqa: E731
            print(
                f"  {name:8} P {mean('precision'):.3f}  R {mean('recall'):.3f}  F1 {mean('f1'):.3f}"
                f"  FP/min {mean('fp_per_minute'):.1f}  FN/min {mean('fn_per_minute'):.1f}"
                f"  {mean('realtime_factor'):.0f}x realtime"
            )

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
