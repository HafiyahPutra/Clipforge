"""A/B the crop *path* itself: sparse keyframes (Phase 5) vs dense (Phase 6).

The face detector cannot tell us where the crop actually was, so this uses a
synthetic source: a horizontal brightness ramp. The mean brightness of every
output frame is a linear function of the crop's x position, so both renders can
be compared sample by sample against the intended (dense) path.

    backend/.venv/bin/python -m backend.scripts.tracking_path_ab /tmp/out [video] [clip_type]
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

from backend.services.tracking import analyze
from backend.services.video_engine import render_clip

def _ramp_video(path: Path, size: tuple[int, int], seconds: float = 12.0, fps: int = 15) -> None:
    width, height = size
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    ramp = np.tile(np.linspace(0, 255, width, dtype=np.uint8), (height, 1))
    frame = cv2.cvtColor(ramp, cv2.COLOR_GRAY2BGR)
    for _ in range(int(seconds * fps)):
        writer.write(frame)
    writer.release()


def _crop_x(video: Path, width: int) -> np.ndarray:
    """Recover the crop x (normalised) from the mean brightness of each frame."""
    cap = cv2.VideoCapture(str(video))
    values: list[float] = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            values.append(float(frame.mean()))
    finally:
        cap.release()
    mean = np.array(values, dtype=float)
    # crop width for 9:16 out of a 320x240 source = 135 px; centre = x + 67.5
    centre_px = mean / 255.0 * width
    return np.clip(centre_px / width, 0.0, 1.0)


def main(argv: list[str]) -> int:
    out_dir = Path(argv[0] if argv else "/tmp/path_ab")
    path_source = Path(argv[1]) if len(argv) > 1 else Path("data/media/jNQXAC9IVRw.mp4")
    clip_type = argv[2] if len(argv) > 2 else "talking_head"
    out_dir.mkdir(parents=True, exist_ok=True)

    probe = cv2.VideoCapture(str(path_source))
    size = (int(probe.get(cv2.CAP_PROP_FRAME_WIDTH)), int(probe.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    probe.release()
    source = out_dir / "ramp.mp4"
    _ramp_video(source, size)

    data = analyze(path_source, 0.0, 12.0, clip_type, use_cache=False)
    dense = [{"t": p["t"], "x": p["x"]} for p in data["trajectory"]]
    step = max(1, len(dense) // 10)
    sparse = [{"t": p["t"], "x": p["x"]} for p in dense[::step]]
    if sparse[-1] is not dense[-1]:
        sparse.append(dense[-1])

    ideal_t = np.array([p["t"] for p in dense])
    ideal_x = np.array([p["x"] for p in dense])

    print(f"path source={path_source.name} type={clip_type} dense={len(dense)} sparse={len(sparse)}")
    paths = {}
    for label, keys in (("sparse", sparse), ("dense", dense)):
        out = out_dir / f"crop_{label}.mp4"
        render_clip(
            source,
            out,
            start=0.0,
            end=12.0,
            width=540,
            height=960,
            has_audio=False,
            src_size=size,
            face_keyframes=keys,
            preview=True,
        )
        paths[label] = _crop_x(out, size[0])

    fps = 30.0
    for label, path in paths.items():
        times = np.arange(path.size) / fps
        intended = np.interp(times, ideal_t, ideal_x)
        delta = np.abs(np.diff(path))
        err = np.abs(path - intended)
        print(
            f"{label:<7} frames={path.size} step_mean={delta.mean()*100:.2f}% "
            f"step_max={delta.max()*100:.2f}% err_vs_intended_mean={err.mean()*100:.2f}% "
            f"err_p95={np.percentile(err, 95)*100:.2f}% err_max={err.max()*100:.2f}%",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
