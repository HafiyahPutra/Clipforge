"""Tahap 5 — make the crop path smooth.

Chain applied to every tracked axis, in this order:

    One-Euro (or EMA / Kalman) → dead zone → Savitzky-Golay → speed clamp

* One-Euro removes jitter without adding the lag of a plain low-pass: it
  filters harder when the face is still and lets go when it moves.
* Dead zone kills the sub-pixel breathing that makes a static shot look alive.
* Savitzky-Golay smooths the whole path while keeping its curvature.
* Speed clamp runs *last*: a polynomial fit rings around a step (speaker
  switch, face reappearing), and clamping afterwards keeps the guarantee that
  the crop never travels more than ``max_speed`` per step — the ring becomes a
  quick pan instead of a snap. Clamping before the fit would let the ringing
  through again.

Parameters live in ``tracking/config.py``.
"""

from __future__ import annotations

import math

import cv2
import numpy as np


class OneEuro:
    """1-D One-Euro filter (Casiez et al. 2012)."""

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.3, d_cutoff: float = 1.0) -> None:
        self.min_cutoff = max(1e-4, float(min_cutoff))
        self.beta = max(0.0, float(beta))
        self.d_cutoff = max(1e-4, float(d_cutoff))
        self._x: float | None = None
        self._dx = 0.0

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / max(1e-6, dt))

    def filter(self, value: float, dt: float) -> float:
        dt = max(1e-3, float(dt))
        if self._x is None:
            self._x = float(value)
            return self._x
        raw_dx = (value - self._x) / dt
        self._dx += self._alpha(self.d_cutoff, dt) * (raw_dx - self._dx)
        cutoff = self.min_cutoff + self.beta * abs(self._dx)
        self._x += self._alpha(cutoff, dt) * (value - self._x)
        return self._x


class Ema:
    def __init__(self, alpha: float = 0.25) -> None:
        self.alpha = min(1.0, max(0.01, float(alpha)))
        self._x: float | None = None

    def filter(self, value: float, dt: float) -> float:
        if self._x is None:
            self._x = float(value)
        else:
            self._x += self.alpha * (float(value) - self._x)
        return self._x


class Kalman1D:
    """Constant-velocity Kalman filter on a single axis."""

    def __init__(self, process_noise: float = 0.02, measure_noise: float = 0.12) -> None:
        self._kf = cv2.KalmanFilter(2, 1)
        self._kf.transitionMatrix = np.array([[1.0, 1.0], [0.0, 1.0]], dtype=np.float32)
        self._kf.measurementMatrix = np.array([[1.0, 0.0]], dtype=np.float32)
        self._kf.processNoiseCov = np.eye(2, dtype=np.float32) * float(process_noise)
        self._kf.measurementNoiseCov = np.eye(1, dtype=np.float32) * float(measure_noise)
        self._kf.errorCovPost = np.eye(2, dtype=np.float32)
        self._ready = False

    def filter(self, value: float, dt: float) -> float:
        measurement = np.array([[float(value)]], dtype=np.float32)
        if not self._ready:
            self._kf.statePost = np.array([[float(value)], [0.0]], dtype=np.float32)
            self._ready = True
            return float(value)
        self._kf.predict()
        corrected = self._kf.correct(measurement)
        return float(corrected[0, 0])


def _filter_axis(values: np.ndarray, dt: float, params: dict) -> np.ndarray:
    kind = str(params.get("smoothing", "one_euro")).lower()
    if kind == "ema":
        filt = Ema(float(params.get("ema_alpha", 0.22)))
    elif kind == "kalman":
        filt = Kalman1D(
            float(params.get("kalman_process_noise", 0.02)),
            float(params.get("kalman_measure_noise", 0.12)),
        )
    else:
        filt = OneEuro(
            float(params.get("one_euro_min_cutoff", 1.2)),
            float(params.get("one_euro_beta", 0.35)),
            float(params.get("one_euro_d_cutoff", 1.0)),
        )
    return np.asarray([filt.filter(float(value), dt) for value in values], dtype=float)


def dead_zone(values: np.ndarray, zone: float) -> np.ndarray:
    if zone <= 0 or values.size == 0:
        return values
    out = np.empty_like(values)
    out[0] = values[0]
    for i in range(1, values.size):
        out[i] = out[i - 1] if abs(values[i] - out[i - 1]) < zone else values[i]
    return out


def clamp_speed(values: np.ndarray, max_step: float) -> np.ndarray:
    if max_step <= 0 or values.size == 0:
        return values
    out = np.empty_like(values)
    out[0] = values[0]
    for i in range(1, values.size):
        delta = values[i] - out[i - 1]
        out[i] = out[i - 1] + max(-max_step, min(max_step, delta))
    return out


def _savgol_coefficients(window: int, poly: int) -> np.ndarray:
    half = window // 2
    x = np.arange(-half, half + 1, dtype=float)
    design = np.vander(x, poly + 1, increasing=True)
    return np.linalg.pinv(design)[0]


def savgol(values: np.ndarray, window: int, poly: int) -> np.ndarray:
    """Savitzky-Golay smoothing implemented with NumPy (no SciPy dependency)."""
    if window <= 2 or values.size < window:
        return values
    window = min(window, values.size if values.size % 2 == 1 else values.size - 1)
    if window <= 2:
        return values
    poly = max(1, min(poly, window - 1))
    coeffs = _savgol_coefficients(window, poly)
    half = window // 2
    padded = np.pad(values, (half, half), mode="edge")
    return np.convolve(padded, coeffs[::-1], mode="valid")


def smooth_axis(values: np.ndarray, times: np.ndarray, params: dict) -> np.ndarray:
    """Full smoothing chain for one axis."""
    if values.size == 0:
        return values
    dt = float(np.median(np.diff(times))) if times.size > 1 else 0.1
    out = _filter_axis(values, max(1e-3, dt), params)
    out = dead_zone(out, float(params.get("dead_zone", 0.0)))
    out = savgol(out, int(params.get("savgol_window", 0)), int(params.get("savgol_poly", 2)))
    out = clamp_speed(out, float(params.get("max_speed", 0.0)))
    return out
