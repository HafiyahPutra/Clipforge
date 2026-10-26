"""Measure the energy/pitch VAD against Whisper word timings.

Whisper is not ground truth, but its word spans are a much better reference than
nothing: a word that Whisper heard is certainly speech, and a gap of >0.5 s
between words is very likely silence. We therefore score two things:

* recall  — share of Whisper speech time the VAD also calls speech (misses are
            the dangerous ones: a missed turn means no cut).
* precision — share of VAD speech that lands inside a Whisper word span (or
            within a small pad, since Whisper trims leading/trailing silence).

Usage::

    backend/.venv/bin/python -m backend.scripts.vad_eval \
        --src data/media/W7Q_5U2YeI8.mp4 --start 50 --end 90
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from backend.services import video_engine, whisper_service
from backend.services.tracking import config as tracking_config
from backend.services.tracking import vad

PAD = 0.25  # Whisper trims silence inside a word span; forgive this much


def _extract(src: str, start: float, end: float, out: Path) -> None:
    binary = video_engine.capabilities()["path"]
    subprocess.run(
        [
            binary, "-v", "error", "-nostdin", "-y",
            "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", src,
            "-vn", "-ac", "1", "-ar", "16000", str(out),
        ],
        check=True,
        capture_output=True,
    )


def evaluate(src: str, start: float, end: float, params: dict, *, model: str | None = None) -> dict:
    window = end - start
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "window.wav"
        _extract(src, start, end, wav)

        t0 = time.time()
        result = vad.get_vad(params).run(vad.load_audio(str(wav)), vad.SAMPLE_RATE)
        vad_seconds = time.time() - t0

        t1 = time.time()
        transcript = whisper_service.transcribe(str(wav), model_size=model)
        whisper_seconds = time.time() - t1

    words = [
        (float(w["start"]), float(w["end"]))
        for seg in transcript.get("segments", [])
        for w in (seg.get("words") or [])
    ]
    if not words:
        return {"error": "whisper produced no words", "src": src, "start": start, "end": end}

    reference = np.zeros(result.speech.size, dtype=bool)
    padded = np.zeros(result.speech.size, dtype=bool)
    for lo, hi in words:
        a, b = int(lo / result.hop), int(hi / result.hop)
        reference[max(0, a) : max(0, b)] = True
        pa, pb = int(max(0.0, lo - PAD) / result.hop), int((hi + PAD) / result.hop)
        padded[max(0, pa) : max(0, pb)] = True

    speech = result.speech
    tp = int((speech & reference).sum())
    fp = int((speech & ~padded).sum())
    fn = int((~speech & reference).sum())
    tn = int((~speech & ~padded).sum())

    def runs(mask: np.ndarray, minimum: float = 0.08) -> list[tuple[float, float]]:
        if not mask.any():
            return []
        edges = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
        out = []
        for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
            if (b - a) * result.hop >= minimum:
                out.append((round(a * result.hop, 2), round(b * result.hop, 2)))
        return out

    return {
        "src": Path(src).name,
        "window": [start, end],
        "backend": result.backend,
        "hop": result.hop,
        "vad_seconds": round(vad_seconds, 2),
        "whisper_seconds": round(whisper_seconds, 2),
        "whisper_words": len(words),
        "speech_ratio_vad": round(result.speech_ratio, 3),
        "speech_ratio_whisper": round(float(reference.mean()), 3),
        "precision": round(tp / max(tp + fp, 1), 3),
        "recall": round(tp / max(tp + fn, 1), 3),
        "fp_seconds": round(fp * result.hop, 2),
        "fn_seconds": round(fn * result.hop, 2),
        "fp_per_minute": round(fp * result.hop / window * 60, 1),
        "fn_per_minute": round(fn * result.hop / window * 60, 1),
        "word_centre_hit": round(float(result.at(np.array([(a + b) / 2 for a, b in words])).mean()), 3),
        "fn_runs": runs(~speech & reference)[:12],
        "fp_runs": runs(speech & ~padded)[:12],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", required=True)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--end", type=float, required=True)
    parser.add_argument("--clip-type", default="podcast")
    parser.add_argument("--model", default=None, help="whisper size override")
    parser.add_argument("--json", default=None, help="append the result to this file")
    args = parser.parse_args(argv)

    params = tracking_config.resolve(args.clip_type)
    report = evaluate(args.src, args.start, args.end, params, model=args.model)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.json:
        path = Path(args.json)
        rows = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        rows.append(report)
        path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
