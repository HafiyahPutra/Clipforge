"""Sweep the energy/pitch VAD thresholds against Whisper word spans.

Development helper, not part of the runtime pipeline: it answers "which margin
and release give the best precision/recall on our own footage?" so the defaults
in tracking/config.py are not guesses.

    backend/.venv/bin/python -m backend.scripts.vad_sweep --src ... --start 50 --end 90
"""

from __future__ import annotations

import argparse
import itertools
import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from backend.services import video_engine, whisper_service
from backend.services.tracking import config as tracking_config
from backend.services.tracking import vad


def _words(src: str, start: float, end: float) -> list[tuple[float, float]]:
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "w.wav"
        subprocess.run(
            [
                video_engine.capabilities()["path"], "-v", "error", "-nostdin", "-y",
                "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", src,
                "-vn", "-ac", "1", "-ar", "16000", str(wav),
            ],
            check=True,
            capture_output=True,
        )
        tr = whisper_service.transcribe(str(wav))
    return [
        (float(w["start"]), float(w["end"]))
        for seg in tr.get("segments", [])
        for w in (seg.get("words") or [])
    ]


def score(result, words, pad: float = 0.25) -> dict:
    ref = np.zeros(result.speech.size, dtype=bool)
    soft = np.zeros(result.speech.size, dtype=bool)
    for lo, hi in words:
        ref[int(lo / result.hop) : int(hi / result.hop)] = True
        soft[int(max(0.0, lo - pad) / result.hop) : int((hi + pad) / result.hop)] = True
    sp = result.speech
    tp = int((sp & ref).sum())
    fp = int((sp & ~soft).sum())
    fn = int((~sp & ref).sum())
    return {
        "P": round(tp / max(tp + fp, 1), 3),
        "R": round(tp / max(tp + fn, 1), 3),
        "f1": round(2 * tp / max(2 * tp + fp + fn, 1), 3),
        "speech": round(float(sp.mean()), 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--clip-type", default="podcast")
    args = ap.parse_args()

    base = tracking_config.resolve(args.clip_type)
    words = _words(args.src, args.start, args.end)
    print("words", len(words))

    samples = vad.load_audio(args.src, start=args.start, end=args.end)
    rows = []
    for margin, release, floor_p in itertools.product([4.0, 6.0, 8.0, 10.0], [3.0, 6.0], [10.0, 20.0]):
        params = dict(base, vad_margin_db=margin, vad_release_db=release, vad_floor_percentile=floor_p)
        result = vad.EnergyPitchVad(params).run(samples, vad.SAMPLE_RATE)
        rows.append({"margin": margin, "release": release, "floor_pct": floor_p, **score(result, words)})

    rows.sort(key=lambda r: r["f1"], reverse=True)
    print(json.dumps(rows[:8], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
