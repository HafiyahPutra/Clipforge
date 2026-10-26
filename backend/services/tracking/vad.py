"""Voice activity detection with a pluggable backend.

The default backend is deliberately dependency-free: short-time RMS energy plus
an autocorrelation pitch estimate, both built on ``numpy``/``scipy`` which the
project already ships. A trained alternative is available as ``vad_backend =
"silero"``: the Silero VAD v5/v6 ONNX graph run through ``onnxruntime`` (already
installed as a faster-whisper dependency), with the model file that ships inside
``faster-whisper``. Nothing here imports ``torch``, ever.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from .. import video_engine

log = logging.getLogger(__name__)

#: Sample rate every backend receives. 16 kHz mono is what webrtcvad and Silero
#: both expect, so the decode step is shared.
SAMPLE_RATE = 16_000


@dataclass
class VadResult:
    """Per-frame speech decisions on a fixed hop grid.

    ``start`` is the offset of frame 0 inside the clip window, so callers can map
    frame indices back to clip-relative or absolute video time.
    """

    backend: str
    sample_rate: int
    hop: float                     # seconds between frames
    start: float                   # clip-relative time of frame 0
    speech: np.ndarray             # bool[frames]
    energy_db: np.ndarray          # float32[frames]
    pitch_hz: np.ndarray           # float32[frames], 0 where unvoiced
    voiced: np.ndarray             # bool[frames]
    threshold_db: float
    duration: float
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def times(self) -> np.ndarray:
        return self.start + np.arange(self.speech.size, dtype=np.float32) * self.hop

    @property
    def speech_ratio(self) -> float:
        return float(self.speech.mean()) if self.speech.size else 0.0

    def spans(self, min_gap: float = 0.0, min_length: float = 0.0) -> list[tuple[float, float]]:
        """Speech intervals in clip-relative seconds."""
        if self.speech.size == 0:
            return []
        edges = np.diff(np.concatenate(([0], self.speech.astype(np.int8), [0])))
        starts = np.flatnonzero(edges == 1)
        ends = np.flatnonzero(edges == -1)
        out: list[tuple[float, float]] = []
        for lo, hi in zip(starts, ends):
            begin = self.start + lo * self.hop
            finish = self.start + hi * self.hop
            if out and begin - out[-1][1] <= min_gap:
                out[-1] = (out[-1][0], finish)
                continue
            out.append((begin, finish))
        return [(a, b) for a, b in out if b - a >= min_length]

    def at(self, times: np.ndarray, tolerance: float = 0.0) -> np.ndarray:
        """Speech flag sampled at arbitrary times (nearest frame, ± tolerance)."""
        times = np.asarray(times, dtype=np.float32)
        if self.speech.size == 0:
            return np.zeros(times.shape, dtype=bool)
        idx = np.rint((times - self.start) / self.hop).astype(int)
        idx = np.clip(idx, 0, self.speech.size - 1)
        hit = self.speech[idx]
        if tolerance > 0:
            half = max(1, int(round(tolerance / self.hop)))
            padded = np.pad(self.speech.astype(np.int8), (half, half), mode="edge")
            window = np.lib.stride_tricks.sliding_window_view(padded, half * 2 + 1)
            hit = hit | window[idx].max(axis=1).astype(bool)
        return hit

    def to_dict(self) -> dict[str, Any]:
        """Compact, JSON-friendly summary (frames are stored separately)."""
        return {
            "backend": self.backend,
            "sample_rate": self.sample_rate,
            "hop": round(self.hop, 4),
            "start": round(self.start, 3),
            "duration": round(self.duration, 3),
            "frames": int(self.speech.size),
            "speech_ratio": round(self.speech_ratio, 4),
            "threshold_db": round(self.threshold_db, 2),
            "voiced_ratio": round(float(self.voiced.mean()), 4) if self.voiced.size else 0.0,
            **self.extra,
        }


class Vad(Protocol):
    """Everything a caller may rely on."""

    name: str

    def run(self, samples: np.ndarray, sample_rate: int, *, start: float = 0.0) -> VadResult:
        ...


# --------------------------------------------------------------------------- #
# Shared helpers (used by every backend)
# --------------------------------------------------------------------------- #
def pitch_track(
    frames: np.ndarray, sample_rate: int, pitch_min: float, pitch_max: float
) -> tuple[np.ndarray, np.ndarray]:
    """Normalised-autocorrelation pitch per frame (Hz, confidence)."""
    window = np.hanning(frames.shape[1]).astype(np.float32)
    spec = np.fft.rfft(frames * window, n=2 * frames.shape[1], axis=1)
    acf = np.fft.irfft(np.abs(spec) ** 2, axis=1)[:, : frames.shape[1]]
    zero = acf[:, :1]
    acf = acf / np.maximum(zero, 1e-9)
    lo = max(2, int(sample_rate / pitch_max))
    hi = min(frames.shape[1] - 1, int(sample_rate / pitch_min))
    pitch = np.zeros(frames.shape[0], dtype=np.float32)
    confidence = np.zeros(frames.shape[0], dtype=np.float32)
    if hi <= lo:
        return pitch, confidence
    band = acf[:, lo:hi]
    best = np.argmax(band, axis=1)
    confidence = band[np.arange(band.shape[0]), best]
    lag = (best + lo).astype(np.float32)
    pitch = np.where(confidence > 0.0, sample_rate / np.maximum(lag, 1.0), 0.0)
    return pitch.astype(np.float32), confidence.astype(np.float32)


def bridge_mask(mask: np.ndarray, hop: float, min_speech: float, max_gap: float) -> np.ndarray:
    """Fill gaps shorter than `max_gap` and drop bursts shorter than `min_speech`."""
    if mask.size == 0:
        return mask
    out = mask.copy()
    edges = np.diff(np.concatenate(([0], out.astype(np.int8), [0])))
    for lo, hi in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
        if (hi - lo) * hop < min_speech:
            out[lo:hi] = False
    edges = np.diff(np.concatenate(([0], out.astype(np.int8), [0])))
    for lo, hi in zip(np.flatnonzero(edges == -1), np.flatnonzero(edges == 1)[1:]):
        if (hi - lo) * hop <= max_gap:
            out[lo:hi] = True
    return out


# --------------------------------------------------------------------------- #
# Audio loading
# --------------------------------------------------------------------------- #
def load_audio(
    path: str,
    *,
    start: float | None = None,
    end: float | None = None,
    sample_rate: int = SAMPLE_RATE,
) -> np.ndarray:
    """Decode `path` (optionally a window) to mono float32 at `sample_rate`."""
    binary = video_engine.capabilities()["path"]
    command = [binary, "-v", "error", "-nostdin"]
    if start is not None:
        command += ["-ss", f"{max(0.0, float(start)):.3f}"]
    command += ["-i", str(path)]
    if end is not None and start is not None:
        command += ["-t", f"{max(0.0, float(end) - float(start)):.3f}"]
    elif end is not None:
        command += ["-t", f"{max(0.0, float(end)):.3f}"]
    command += [
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(int(sample_rate)),
        "-f",
        "f32le",
        "-acodec",
        "pcm_f32le",
        "pipe:1",
    ]
    proc = subprocess.run(command, capture_output=True, check=False)
    if proc.returncode != 0 or not proc.stdout:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(proc.stdout, dtype="<f4").astype(np.float32, copy=False)


# --------------------------------------------------------------------------- #
# Default backend: energy + pitch
# --------------------------------------------------------------------------- #
class EnergyPitchVad:
    """Energy gate with a pitch cross-check and hysteresis.

    Steps per 10 ms hop:

    1. RMS energy in dB over a 30 ms window.
    2. Adaptive floor = low percentile of the clip's own energy, so a quiet
       recording is not classified as silence and a loud one is not all speech.
    3. Pitch by normalised autocorrelation (FFT), used to reject broadband
       transients — a door slam is loud but not voiced.
    4. Two thresholds (enter/exit) plus short-gap bridging and short-burst
       removal, which is what keeps a single cough from opening a turn.
    """

    name = "energy"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        params = params or {}
        self.frame_ms = float(params.get("vad_frame_ms", 30.0))
        self.hop_ms = float(params.get("vad_hop_ms", 10.0))
        self.margin_db = float(params.get("vad_margin_db", 9.0))
        self.release_db = float(params.get("vad_release_db", 4.0))
        self.floor_percentile = float(params.get("vad_floor_percentile", 20.0))
        self.pitch_min = float(params.get("vad_pitch_min", 70.0))
        self.pitch_max = float(params.get("vad_pitch_max", 350.0))
        self.pitch_confidence = float(params.get("vad_pitch_confidence", 0.30))
        self.min_speech = float(params.get("vad_min_speech", 0.10))
        self.max_gap = float(params.get("vad_max_gap", 0.18))
        self.require_voiced = bool(params.get("vad_require_voiced", False))

    # -- internals ---------------------------------------------------------
    def _frames(self, samples: np.ndarray, sample_rate: int) -> tuple[np.ndarray, int]:
        frame = max(1, int(round(sample_rate * self.frame_ms / 1000.0)))
        hop = max(1, int(round(sample_rate * self.hop_ms / 1000.0)))
        if samples.size < frame:
            samples = np.pad(samples, (0, frame - samples.size))
        count = 1 + (samples.size - frame) // hop
        index = np.arange(frame)[None, :] + hop * np.arange(count)[:, None]
        return samples[index], hop

    def _pitch(self, frames: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
        return pitch_track(frames, sample_rate, self.pitch_min, self.pitch_max)

    def _bridge(self, mask: np.ndarray, hop: float) -> np.ndarray:
        return bridge_mask(mask, hop, self.min_speech, self.max_gap)

    # -- public ------------------------------------------------------------
    def run(self, samples: np.ndarray, sample_rate: int, *, start: float = 0.0) -> VadResult:
        hop = self.hop_ms / 1000.0
        if samples.size == 0:
            empty = np.zeros(0, dtype=np.float32)
            return VadResult(
                backend=self.name,
                sample_rate=sample_rate,
                hop=hop,
                start=start,
                speech=np.zeros(0, dtype=bool),
                energy_db=empty,
                pitch_hz=empty,
                voiced=np.zeros(0, dtype=bool),
                threshold_db=0.0,
                duration=0.0,
            )

        frames, hop_samples = self._frames(samples, sample_rate)
        rms = np.sqrt(np.mean(frames.astype(np.float32) ** 2, axis=1) + 1e-12)
        energy_db = 20.0 * np.log10(np.maximum(rms, 1e-7))
        pitch, confidence = self._pitch(frames, sample_rate)
        voiced = (confidence >= self.pitch_confidence) & (pitch >= self.pitch_min) & (
            pitch <= self.pitch_max
        )

        floor = float(np.percentile(energy_db, self.floor_percentile))
        peak = float(np.percentile(energy_db, 95))
        # A very flat recording (no quiet parts) would make the floor useless;
        # fall back to a fraction of the dynamic range.
        if peak - floor < 6.0:
            floor = peak - 12.0
        enter = floor + self.margin_db
        exit_ = max(floor, enter - self.release_db)

        # Entry is strict (pitch-backed, so a clap or a door slam does not open
        # a turn); holding is loose, because speech tails off well below the
        # level that opened it. Using the strict test for both was cutting every
        # quiet syllable out of the mask.
        loud = enter + 6.0
        state = False
        speech = np.zeros(energy_db.shape[0], dtype=bool)
        for i, level in enumerate(energy_db):
            if state:
                state = level >= exit_
            elif level >= enter:
                state = voiced[i] or not self.require_voiced or level >= loud
            speech[i] = state
        speech = self._bridge(speech, hop)

        return VadResult(
            backend=self.name,
            sample_rate=sample_rate,
            hop=hop,
            start=start,
            speech=speech,
            energy_db=energy_db.astype(np.float32),
            pitch_hz=pitch,
            voiced=voiced,
            threshold_db=float(enter),
            duration=float(samples.size) / float(sample_rate),
            extra={
                "floor_db": round(floor, 2),
                "enter_db": round(float(enter), 2),
                "exit_db": round(float(exit_), 2),
                "hop_samples": int(hop_samples),
                "frame_ms": self.frame_ms,
                "hop_ms": self.hop_ms,
            },
        )


# --------------------------------------------------------------------------- #
# Optional backend: Silero VAD through onnxruntime
# --------------------------------------------------------------------------- #
SILERO_WINDOW = 512          # samples per decision at 16 kHz (32 ms)
SILERO_CONTEXT = 64          # samples of the previous window the model expects
SILERO_MODEL_NAMES = ("silero_vad_v6.onnx", "silero_vad.onnx", "silero_vad_v5.onnx")


def _silero_model_path(params: dict[str, Any]) -> str | None:
    """Find a Silero ONNX file without downloading anything at runtime.

    Order: an explicit path (config `silero_model` / `SILERO_VAD_MODEL`), then a
    copy shipped in `backend/models`, then the asset that ships inside
    `faster-whisper` — which is already installed, so the default path costs no
    extra bytes in the image.
    """
    explicit = str(params.get("silero_model") or os.getenv("SILERO_VAD_MODEL") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return str(path) if path.is_file() else None

    local = Path(__file__).resolve().parents[2] / "models"
    for name in SILERO_MODEL_NAMES:
        candidate = local / name
        if candidate.is_file():
            return str(candidate)

    try:
        from faster_whisper.utils import get_assets_path

        assets = Path(get_assets_path())
        for name in SILERO_MODEL_NAMES:
            candidate = assets / name
            if candidate.is_file():
                return str(candidate)
    except Exception:  # faster-whisper absent or moved: no model, no problem
        return None
    return None


class SileroOnnxVad:
    """Silero VAD v5/v6 as a single ONNX session on the CPU.

    The model is a small streaming RNN: it takes 512 new samples plus the last 64
    samples of the previous window, and carries its state in ``h``/``c``. Windows
    are therefore not independent — they are stacked into one batched call that
    keeps the state in order, which is both faithful to the streaming protocol
    and far faster than a Python loop over 32 ms chunks.

    The decision grid is 512/16000 = 32 ms, coarser than the energy backend's
    10 ms hop. Every consumer (`spans()`, `at()`) works on any hop, and 32 ms is
    the model's native resolution, so nothing is resampled.
    """

    name = "silero"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        params = params or {}
        self.threshold = float(params.get("silero_threshold", 0.5))
        self.release = float(params.get("silero_release", 0.35))
        self.min_speech = float(params.get("vad_min_speech", 0.10))
        self.max_gap = float(params.get("vad_max_gap", 0.18))
        self.pitch_min = float(params.get("vad_pitch_min", 70.0))
        self.pitch_max = float(params.get("vad_pitch_max", 350.0))
        self.model_path = _silero_model_path(params)
        if not self.model_path:
            raise RuntimeError(
                "Silero VAD model not found: set TRACKING_SILERO_MODEL to a silero_vad*.onnx file"
            )
        self._session = None

    def _session_or_open(self):
        if self._session is None:
            import onnxruntime

            opts = onnxruntime.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            opts.enable_cpu_mem_arena = False
            opts.log_severity_level = 4
            self._session = onnxruntime.InferenceSession(
                self.model_path, providers=["CPUExecutionProvider"], sess_options=opts
            )
        return self._session

    def _probabilities(self, samples: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
        """Speech probability per 32 ms window, plus the frames for debug meters."""
        if sample_rate != SAMPLE_RATE:
            # Only ever fed 16 kHz by `detect()`; resample defensively rather than
            # silently mislabeling the model's fixed rate.
            count = int(round(samples.size * SAMPLE_RATE / float(sample_rate)))
            samples = np.interp(
                np.linspace(0.0, samples.size, count, endpoint=False),
                np.arange(samples.size),
                samples,
            ).astype(np.float32)
        pad = (-samples.size) % SILERO_WINDOW
        if pad:
            samples = np.pad(samples, (0, pad))
        frames = samples.reshape(-1, SILERO_WINDOW)
        context = np.zeros((frames.shape[0], SILERO_CONTEXT), dtype=np.float32)
        if frames.shape[0] > 1:
            context[1:] = frames[:-1, -SILERO_CONTEXT:]
        batch = np.concatenate([context, frames], axis=1).astype(np.float32)

        session = self._session_or_open()
        h = np.zeros((1, 1, 128), dtype=np.float32)
        c = np.zeros((1, 1, 128), dtype=np.float32)
        probs: list[np.ndarray] = []
        step = 10000  # matches upstream: keeps one run() under a few MB of activations
        for i in range(0, batch.shape[0], step):
            out, h, c = session.run(None, {"input": batch[i : i + step], "h": h, "c": c})
            probs.append(np.asarray(out, dtype=np.float32).reshape(-1))
        return np.concatenate(probs) if probs else np.zeros(0, dtype=np.float32), frames

    def run(self, samples: np.ndarray, sample_rate: int, *, start: float = 0.0) -> VadResult:
        hop = SILERO_WINDOW / float(SAMPLE_RATE)
        empty = np.zeros(0, dtype=np.float32)
        if samples.size == 0:
            return VadResult(
                backend=self.name,
                sample_rate=SAMPLE_RATE,
                hop=hop,
                start=start,
                speech=np.zeros(0, dtype=bool),
                energy_db=empty,
                pitch_hz=empty,
                voiced=np.zeros(0, dtype=bool),
                threshold_db=0.0,
                duration=0.0,
            )

        probs, frames = self._probabilities(samples, sample_rate)
        rms = np.sqrt(np.mean(frames.astype(np.float32) ** 2, axis=1) + 1e-12)
        energy_db = 20.0 * np.log10(np.maximum(rms, 1e-7))
        pitch, confidence = pitch_track(frames, SAMPLE_RATE, self.pitch_min, self.pitch_max)
        voiced = confidence >= 0.30

        # Same hysteresis idea as the energy backend, on probabilities instead of
        # dB: a high bar to open a turn, a lower one to keep it open.
        state = False
        speech = np.zeros(probs.size, dtype=bool)
        for i, prob in enumerate(probs):
            if state:
                state = prob >= self.release
            else:
                state = prob >= self.threshold
            speech[i] = state
        speech = bridge_mask(speech, hop, self.min_speech, self.max_gap)

        return VadResult(
            backend=self.name,
            sample_rate=SAMPLE_RATE,
            hop=hop,
            start=start,
            speech=speech,
            energy_db=energy_db.astype(np.float32),
            pitch_hz=pitch,
            voiced=voiced,
            threshold_db=0.0,
            duration=float(samples.size) / float(SAMPLE_RATE),
            extra={
                "prob_threshold": self.threshold,
                "prob_release": self.release,
                "window_samples": SILERO_WINDOW,
                "context_samples": SILERO_CONTEXT,
                "mean_prob": round(float(probs.mean()), 3) if probs.size else 0.0,
                "model": Path(self.model_path).name if self.model_path else "",
            },
        )


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
_BACKENDS: dict[str, type] = {"energy": EnergyPitchVad, "silero": SileroOnnxVad}


def register(name: str, factory: type) -> None:
    """Register an alternative backend (webrtcvad, silero-onnx, …)."""
    _BACKENDS[name] = factory


def get_vad(params: dict[str, Any] | None = None) -> Vad:
    """Return the backend named by ``params['vad_backend']`` (default energy).

    A backend that cannot start (missing model, missing wheel) must never break a
    render, so the failure is logged and the dependency-free default takes over.
    """
    params = params or {}
    name = str(params.get("vad_backend", "energy") or "energy").strip().lower()
    factory = _BACKENDS.get(name)
    if factory is None:
        # Never fail the pipeline over a typo in an env var.
        return EnergyPitchVad(params)
    try:
        return factory(params)
    except Exception as exc:
        log.warning("VAD backend %r unavailable (%s); falling back to energy", name, exc)
        return EnergyPitchVad(params)


def detect(
    path: str,
    *,
    start: float,
    end: float,
    params: dict[str, Any] | None = None,
) -> VadResult:
    """Convenience wrapper: decode a window and run the configured backend."""
    samples = load_audio(path, start=start, end=end, sample_rate=SAMPLE_RATE)
    return get_vad(params).run(samples, SAMPLE_RATE, start=0.0)
