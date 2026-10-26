"""Single source of truth for face-tracking tunables and clip-type presets.

Every knob the pipeline reads lives here (spec: "semua parameter berada di satu
tempat konfigurasi"). Each value can be overridden with an environment variable
named ``TRACKING_<UPPER_CASE_KEY>`` so deployments can tune without editing code.

Presets are *partial* overrides of :data:`DEFAULTS`; ``resolve()`` merges them
and fills the gaps, so adding a new clip type only means adding one dict entry.
"""

from __future__ import annotations

import os
from typing import Any

# --- defaults ---------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    # Tahap 1–2: decode + detection cadence
    "detect_fps": 10.0,          # how many frames/second we decode for analysis
    "detect_interval": 1,        # run the model every N decoded frames (N-1 interpolated)
    "analysis_width": 640,       # frames are downscaled to this width for inference
    # Tahap 3: identity
    "iou_threshold": 0.30,       # minimum IoU to keep the same face id
    "max_track_age": 12,         # detection steps a track may vanish before it is dropped
    "hold_frames": 8,            # steps we keep the last position when the face is lost
    # Tahap 4: active speaker
    "use_speaker": True,         # follow whoever is talking (off = follow biggest face)
    "speaker_window": 0.6,       # seconds of mouth-open history per face
    "speaker_threshold": 0.18,   # upper bound for the mean jawOpen that counts as "talking"
    "speaker_threshold_floor": 0.05,   # lower bound; the real value is adaptive per face
    "speaker_threshold_percentile": 60,  # each face is measured against its own 60th percentile
    "speaker_min_switch": 1.5,   # seconds a new speaker must talk before we cut to them
    # Tahap 5: smoothing
    "smoothing": "one_euro",     # one_euro | ema | kalman
    "one_euro_min_cutoff": 1.2,  # lower = smoother but laggier
    "one_euro_beta": 0.35,       # higher = follows fast motion more closely
    "one_euro_d_cutoff": 1.0,
    "ema_alpha": 0.22,
    "kalman_process_noise": 0.02,
    "kalman_measure_noise": 0.12,
    "dead_zone": 0.004,          # normalized; moves smaller than this are ignored
    "max_speed": 0.05,           # normalized units per output step (0.05 ≈ 5%/step)
    "savgol_window": 9,          # final polish window in output steps (odd, 0 disables)
    "savgol_poly": 2,
    # Tahap 6: crop
    "expr_hz": 10.0,             # resolution of the crop expression handed to FFmpeg
    "zoom": 1.0,                 # static baseline zoom applied to every shot
    "min_face_ratio": 0.04,      # ignore detections smaller than this (noise)
    # Tahap 7: shot list (every boundary is a hard cut)
    "shot_min_seconds": 2.0,     # no shot shorter than this
    "cut_min_talk": 0.8,         # new speaker must talk this long before a cut is placed
    "cut_lookahead": 0.15,       # cut this much *before* the new voice starts
    "cut_max_per_minute": 6.0,   # above this the draft lengthens the shortest shots
    # Tahap 7.2: voice activity (pluggable; default = numpy/scipy energy+pitch)
    "vad_backend": "energy",     # "energy" | any backend registered in tracking/vad.py
    "vad_frame_ms": 30.0,
    "vad_hop_ms": 10.0,
    "vad_margin_db": 6.0,        # energy above the clip's own noise floor
    "vad_release_db": 4.0,       # hysteresis: drop out this far below the entry
    "vad_floor_percentile": 10.0,
    "vad_pitch_min": 70.0,
    "vad_pitch_max": 350.0,
    "vad_pitch_confidence": 0.30,
    "vad_min_speech": 0.10,      # bursts shorter than this are discarded (cough)
    "vad_max_gap": 0.18,         # gaps shorter than this are bridged (stops)
    "vad_require_voiced": False,  # True = only voiced frames count as speech
    "vad_turn_tolerance": 0.45,  # a turn needs speech within this window
    # Optional Silero backend (vad_backend = "silero"); ignored by the default
    "silero_threshold": 0.5,     # speech probability that opens a turn
    "silero_release": 0.35,      # lower bar that keeps a turn open (hysteresis)
    "silero_model": "",          # explicit .onnx path; "" = auto-discover
    # Tahap 7.2: overlap -> whole_frame
    "overlap_min_seconds": 1.2,  # two people talking this long = whole_frame
    "overlap_max_gap": 0.30,     # bridge gaps in the overlap mask
    "whole_frame_enabled": True,
    "whole_frame_blur": 24,      # background blur strength (sigma)
    # Tahap 7: dynamic zoom (only *inside* a shot, never across a cut)
    "zoom_strength": 0.0,        # 0 = off; per clip type 0…1 scales the zoom amount
    "zoom_min": 1.0,
    "zoom_max": 1.4,
    "zoom_face_ratio": 0.30,     # target face height as a share of the output frame
    "zoom_talk_ratio": 0.45,     # mouth-activity gate: fraction of this face's p90 jawOpen
    "zoom_gate_window": 1.0,     # averaging window for that gate (seconds)
    "zoom_enter": 0.40,          # gate level that starts a push-in
    "zoom_exit": 0.18,           # gate level that starts a pull-out (hysteresis)
    "zoom_delay": 0.6,           # seconds of talking before zoom-in starts
    "zoom_release": 2.5,         # seconds of silence before zoom-out starts
    "zoom_attack": 0.55,         # zoom-in ramp length (seconds)
    "zoom_decay": 0.75,          # zoom-out ramp length (seconds)
    "zoom_max_rate": 0.50,       # hard ceiling on zoom change per second
    "zoom_entry": "normal",      # normal = start at 1.0x | instant = start at target
    "zoom_smooth": 7,            # Savitzky-Golay window for the zoom curve
    "zoom_src_min_height": 720,  # below this the max zoom is reduced (soft source)
}

_ENV_PREFIX = "TRACKING_"


def _env_value(key: str, default: Any) -> Any:
    raw = os.getenv(f"{_ENV_PREFIX}{key.upper()}")
    if raw is None or raw == "":
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int):
        try:
            return int(float(raw))
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return default
    return raw


def defaults() -> dict[str, Any]:
    """Defaults with environment overrides applied."""
    return {key: _env_value(key, value) for key, value in DEFAULTS.items()}


# --- clip types -------------------------------------------------------------

# `layout` tells the renderer how the frame is composed:
#   follow        → one full-frame crop that follows the tracked face
#   split_top_cam → wide shot at the bottom, head-and-shoulders strip on top
#   pip_corner    → full-frame crop plus a small framed facecam
#
# `status` marks how mature the preset is. The four "preview" entries already
# work (they reuse the generic pipeline) but have not been tuned against real
# footage of that genre yet — flip them to "stable" once they are.

CLIP_TYPES: dict[str, dict[str, Any]] = {
    "podcast": {
        "label": "Podcast / Interview",
        "description": "Follow whoever is talking; switch only after they keep talking.",
        "status": "stable",
        "layout": "follow",
        "overrides": {
            "use_speaker": True,
            "speaker_min_switch": 1.5,
            "dead_zone": 0.005,
            "hold_frames": 10,
            "max_speed": 0.045,
            "one_euro_min_cutoff": 1.0,
            "one_euro_beta": 0.30,
            "zoom": 1.0,
            "zoom_strength": 1.0,     # medium: podcast zoom is the reference
        },
    },
    "talking_head": {
        "label": "Talking Head / Vlog",
        "description": "One face, strong smoothing, almost no camera drift.",
        "status": "stable",
        "layout": "follow",
        "overrides": {
            "use_speaker": False,
            "detect_interval": 2,
            "dead_zone": 0.010,
            "hold_frames": 15,
            "max_speed": 0.025,
            "one_euro_min_cutoff": 0.6,
            "one_euro_beta": 0.18,
            "savgol_window": 13,
            "zoom": 1.05,
            "zoom_strength": 0.6,     # light: one face, already tight
        },
    },
    "gaming": {
        "label": "Gaming / Screen Recording",
        "description": "Gameplay at the bottom, facecam strip on top.",
        "status": "stable",
        "layout": "split_top_cam",
        "overrides": {
            "use_speaker": False,
            "detect_fps": 6.0,
            "detect_interval": 2,
            "dead_zone": 0.020,
            "hold_frames": 20,
            "max_speed": 0.020,
            "one_euro_min_cutoff": 0.5,
            "one_euro_beta": 0.10,
            "savgol_window": 15,
            "zoom_strength": 0.0,     # gameplay content must not breathe
        },
    },
    "tutorial": {
        "label": "Tutorial / Edukasi",
        "description": "Face plus screen content (uses the facecam corner).",
        "status": "preview",
        "layout": "pip_corner",
        "overrides": {
            "detect_fps": 6.0,
            "dead_zone": 0.012,
            "max_speed": 0.03,
            "zoom_strength": 0.5,
        },
    },
    "webinar": {
        "label": "Webinar / Presentasi",
        "description": "Speaker focus with a calmer camera than podcast.",
        "status": "preview",
        "layout": "follow",
        "overrides": {
            "dead_zone": 0.008,
            "max_speed": 0.035,
            "zoom": 0.95,
            "zoom_strength": 0.8,
        },
    },
    "reaction": {
        "label": "Reaction",
        "description": "Split screen, reaction cam on top.",
        "status": "preview",
        "layout": "split_top_cam",
        "overrides": {"detect_fps": 8.0, "max_speed": 0.04, "zoom_strength": 0.4},
    },
    "event": {
        "label": "Event / Panggung",
        "description": "Loose tracking with a wider shot.",
        "status": "preview",
        "layout": "follow",
        "overrides": {
            "use_speaker": True,
            "dead_zone": 0.015,
            "max_speed": 0.03,
            "one_euro_min_cutoff": 0.7,
            "savgol_window": 15,
            "zoom": 0.85,
            "zoom_strength": 0.5,
        },
    },
}

STABLE_TYPES = tuple(key for key, item in CLIP_TYPES.items() if item["status"] == "stable")


def resolve(clip_type: str | None) -> dict[str, Any]:
    """Merged parameters for ``clip_type`` (unknown values fall back to defaults)."""
    params = defaults()
    preset = CLIP_TYPES.get(str(clip_type or "").strip().lower())
    if preset:
        params.update(preset["overrides"])
        params["clip_type"] = next(key for key, item in CLIP_TYPES.items() if item is preset)
        params["layout"] = preset["layout"]
    else:
        params["clip_type"] = "custom"
        params["layout"] = "follow"
    params["expr_hz"] = float(params["expr_hz"]) or 10.0
    params["detect_interval"] = max(1, int(params["detect_interval"]))
    params["savgol_window"] = int(params["savgol_window"])
    if params["savgol_window"] % 2 == 0:
        params["savgol_window"] += 1
    return params


def catalog() -> list[dict[str, Any]]:
    """Preset list for the API/UI (params resolved so the UI can show them)."""
    items: list[dict[str, Any]] = []
    for key, preset in CLIP_TYPES.items():
        merged = resolve(key)
        items.append(
            {
                "id": key,
                "label": preset["label"],
                "description": preset["description"],
                "status": preset["status"],
                "layout": preset["layout"],
                "params": {
                    "detect_fps": merged["detect_fps"],
                    "detect_interval": merged["detect_interval"],
                    "dead_zone": merged["dead_zone"],
                    "hold_frames": merged["hold_frames"],
                    "max_speed": merged["max_speed"],
                    "speaker_min_switch": merged["speaker_min_switch"],
                    "use_speaker": merged["use_speaker"],
                    "zoom": merged["zoom"],
                    "zoom_strength": merged["zoom_strength"],
                    "shot_min_seconds": merged["shot_min_seconds"],
                    "cut_min_talk": merged["cut_min_talk"],
                    "cut_lookahead": merged["cut_lookahead"],
                },
            }
        )
    return items


def suggest(max_faces: int, aspect: float) -> str:
    """Heuristic "auto-detect" suggestion from face count + frame shape."""
    if max_faces >= 2:
        return "podcast"
    if max_faces == 1:
        return "gaming" if aspect < 1.2 else "talking_head"
    return "gaming"
