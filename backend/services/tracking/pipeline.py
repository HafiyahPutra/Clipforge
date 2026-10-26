"""Face-tracking pipeline: video → tracked faces → smooth crop path.

    sampler.sample_frames        Tahap 1–2  decode + downscale
    detector.detect              Tahap 3    boxes + landmarks + jawOpen
    tracker.build_tracks         Tahap 3    identity + gap filling
    speaker.choose_target        Tahap 4    active speaker with hysteresis
    smoother.smooth_axis         Tahap 5    One-Euro + dead zone + speed clamp + SG
    trajectory.build             Tahap 6    dense crop path + metrics

Results are cached in ``data/faces`` (file + mtime + window + clip type + cache
version), so a second preview or export of the same clip is free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np

from backend import config
from backend.services.tracking import config as tracking_config
from backend.services.tracking import detector as detector_mod
from backend.services.shots import draft as shot_draft
from backend.services.tracking import sampler, speaker, tracker, trajectory, vad

logger = logging.getLogger("clipforge.tracking")

# Bump whenever the maths changes so stale paths are not reused.
CACHE_VERSION = 10  # 10: per-face series cached for the People panel


def _cache_path(video: Path, start: float, end: float, clip_type: str) -> Path:
    try:
        stat = video.stat()
        stamp = f"{stat.st_size}:{int(stat.st_mtime)}"
    except OSError:
        stamp = "0:0"
    key = (
        f"{video.resolve()}|{stamp}|{start:.3f}|{end:.3f}|{clip_type}"
        f"|{json.dumps(tracking_config.defaults(), sort_keys=True)}|{CACHE_VERSION}"
    )
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return config.FACES_DIR / f"{digest}.json"


def auto_detect(video: Path, start: float, end: float) -> dict[str, Any]:
    """Suggest a clip type from face count and frame shape."""
    detector = detector_mod.load_detector()
    info = sampler.probe_video(video)
    aspect = info["width"] / info["height"] if info["height"] else 1.78
    max_faces = 0
    samples = 0
    if detector is not None:
        for frame in sampler.iter_thumbnail_frames(video, start, end, count=8):
            samples += 1
            max_faces = max(max_faces, len(detector.detect(frame.image)))
    suggested = tracking_config.suggest(max_faces, aspect)
    return {
        "suggested": suggested,
        "max_faces": max_faces,
        "samples": samples,
        "aspect": round(aspect, 3),
        "reason": (
            f"{max_faces} face(s) in {samples} sampled frames, "
            f"aspect {aspect:.2f}"
        ),
    }


def _empty(start: float, end: float, detector_kind: str, clip_type: str) -> dict[str, Any]:
    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "duration": round(end - start, 3),
        "samples": 0,
        "hits": 0,
        "coverage": 0.0,
        "face": None,
        "keyframes": [],
        "trajectory": [],
        "metrics": {},
        "series": [],
        "person_index": {},
        "available": detector_kind != "none",
        "detector": detector_kind,
        "clip_type": clip_type,
        "cached": False,
    }


def analyze(
    video: str | Path,
    start: float,
    end: float,
    clip_type: str | None = None,
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Analyse ``[start, end)`` and return a JSON-serialisable payload."""
    path = Path(video)
    start = max(0.0, float(start))
    end = max(start + 0.2, float(end))
    requested = (clip_type or "auto").strip().lower()

    detector = detector_mod.load_detector()
    kind = getattr(detector, "kind", "none") if detector else "none"

    suggestion: dict[str, Any] | None = None
    if requested in {"auto", ""}:
        if detector is None or not path.is_file():
            return _empty(start, end, kind, "auto")
        suggestion = auto_detect(path, start, end)
        requested = str(suggestion["suggested"])

    cache_file = _cache_path(path, start, end, requested)
    if use_cache and cache_file.is_file():
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            payload["cached"] = True
            if suggestion:
                payload["auto_detect"] = suggestion
            return payload
        except (OSError, ValueError):
            cache_file.unlink(missing_ok=True)

    if detector is None or not path.is_file():
        payload = _empty(start, end, kind, requested)
        payload["clip_type"] = requested
        return payload

    params = tracking_config.resolve(requested)
    info = sampler.probe_video(path)
    src_w, src_h = int(info["width"]), int(info["height"])
    started = time.perf_counter()

    frames = sampler.sample_frames(
        path,
        start,
        end,
        sample_fps=float(params["detect_fps"]),
        target_width=int(params["analysis_width"]),
    )

    interval = max(1, int(params["detect_interval"]))
    detections: list[tuple[float, list[Any]]] = []
    hits = 0
    for position, frame in enumerate(frames):
        if position % interval:
            continue  # Tahap 3: detect every N frames, interpolate the rest
        found = detector.detect(frame.image)
        detections.append((frame.t, found))
        hits += len(found)

    tracks = tracker.build_tracks(
        detections,
        iou_threshold=float(params["iou_threshold"]),
        max_age=int(params["max_track_age"]),
    )

    duration = max(0.2, end - start)
    expr_hz = float(params["expr_hz"])
    steps = max(2, int(round(duration * expr_hz)) + 1)
    times = np.linspace(0.0, duration, steps)

    hold_seconds = float(params["hold_frames"]) / max(0.5, float(params["detect_fps"]))
    series = tracker.dense_series(tracks, times, hold_seconds=hold_seconds)

    # Voice activity (Tahap 7.2). Cheap (0.3 s for 40 s of audio) and cached with
    # the rest of the payload, so the shot draft can require an actual voice
    # behind a moving mouth. A failure here must never fail the analysis.
    vad_info, speech = _vad(path, start, end, times, params)

    # Per-face series are cached too: the "People" panel and "follow person"
    # shots need every face's path, not just the one the camera followed.
    built = _finish(
        series,
        times,
        params,
        start=start,
        end=end,
        src_w=src_w,
        src_h=src_h,
        speech=speech,
        labels=[_person_label(index) for index in range(len(series))],
    )

    face = built["face"]
    coverage = built["coverage"]
    hit_ratio = round(hits / max(1, len(detections) * max(1, len(series) or 1)), 3)
    keyframes = built["keyframes"]

    payload = {
        "start": round(start, 3),
        "end": round(end, 3),
        "duration": round(duration, 3),
        "samples": len(frames),
        "detections": len(detections),
        "detect_interval": interval,
        "hits": hits,
        "coverage": coverage,
        "hit_ratio": hit_ratio,
        "face": face,
        "keyframes": keyframes,
        "trajectory": built["trajectory"],
        "raw": built["raw"],
        "activity": built["activity"],
        "vad": vad_info,
        "shots": [shot.to_dict() for shot in built["shots"]],
        "shot_metrics": built["shot_metrics"],
        "metrics": built["metrics"],
        "faces": built["faces"],
        "series": series_payload(series, times),
        "person_index": {_person_label(index): index for index in range(len(series))},
        "available": True,
        "detector": kind,
        "clip_type": requested,
        "params": {
            "detect_fps": params["detect_fps"],
            "detect_interval": params["detect_interval"],
            "dead_zone": params["dead_zone"],
            "hold_frames": params["hold_frames"],
            "max_speed": params["max_speed"],
            "smoothing": params["smoothing"],
            "speaker_min_switch": params["speaker_min_switch"],
            "layout": params["layout"],
            "shot_min_seconds": params["shot_min_seconds"],
            "cut_min_talk": params["cut_min_talk"],
            "cut_lookahead": params["cut_lookahead"],
            "cut_max_per_minute": params["cut_max_per_minute"],
            "zoom_strength": params["zoom_strength"],
            "zoom_min": params["zoom_min"],
            "zoom_max": params["zoom_max"],
            "zoom_face_ratio": params["zoom_face_ratio"],
            "zoom_delay": params["zoom_delay"],
            "zoom_release": params["zoom_release"],
            "zoom_entry": params["zoom_entry"],
            "vad_backend": params.get("vad_backend", "energy"),
            "overlap_min_seconds": params.get("overlap_min_seconds", 1.2),
            "whole_frame_enabled": params.get("whole_frame_enabled", True),
        },
        "elapsed_ms": int((time.perf_counter() - started) * 1000),
        "cached": False,
    }
    if suggestion:
        payload["auto_detect"] = suggestion

    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:  # pragma: no cover - disk hiccup only
        logger.warning("could not cache face path: %s", exc)

    metrics = payload["metrics"]
    logger.info(
        "tracking %s [%.1f–%.1f] %s: %d faces, jitter %.2f%% → %.2f%% (jumps %d → %d), %d ms",
        path.name,
        start,
        end,
        requested,
        payload["faces"],
        metrics.get("jitter_raw_pct", 0.0),
        metrics.get("jitter_smoothed_pct", 0.0),
        metrics.get("jumps_raw", 0),
        metrics.get("jumps_smoothed", 0),
        payload["elapsed_ms"],
    )
    return payload


def _person_label(index: int) -> str:
    """Stable label for a track index: A, B, C … (used by follow_person shots)."""
    if index < 26:
        return chr(ord("A") + index)
    return str(index + 1)


def _vad(
    path: Path, start: float, end: float, times: np.ndarray, params: dict
) -> tuple[dict[str, Any], np.ndarray | None]:
    """Voice activity for the window; never raises."""
    info: dict[str, Any] = {"backend": params.get("vad_backend", "energy"), "available": False}
    speech = None
    try:
        result = vad.detect(str(path), start=start, end=end, params=params)
        speech = result.at(times, tolerance=float(params["vad_turn_tolerance"]))
        info = {**result.to_dict(), "available": True}
    except Exception as exc:  # noqa: BLE001 - VAD is a gate, not a requirement
        info = {"backend": "error", "available": False, "error": f"{type(exc).__name__}: {exc}"}
    return info, speech


def _finish(
    series: list[Any],
    times: np.ndarray,
    params: dict,
    *,
    start: float,
    end: float,
    src_w: int,
    src_h: int,
    speech: np.ndarray | None = None,
    labels: list[str] | None = None,
) -> dict[str, Any]:
    """Everything after identity: speaker, smoothed path, metrics, draft shots.

    Split out of :func:`analyze` so the People panel can re-run it on a *corrected*
    series list (ignored faces dropped, merged ids folded together) without
    decoding a single frame again.
    """
    steps = times.size
    decision = speaker.choose_target(series, times, params)
    built = trajectory.build(series, times, decision, params, src_w=src_w, src_h=src_h)

    # Dominant face (used by the facecam crop / legacy consumers).
    face = None
    if series:
        dominant = max(series, key=lambda item: item.weight * item.coverage())
        mask = dominant.valid
        if mask.any():
            face = {
                "x": round(float(np.median(dominant.cx[mask])), 4),
                "y": round(float(np.median(dominant.cy[mask])), 4),
                "w": round(float(np.median(dominant.w[mask])), 4),
                "h": round(float(np.median(dominant.h[mask])), 4),
            }

    # `coverage` = share of the clip where at least one face was known.
    if series:
        any_valid = np.zeros(steps, dtype=bool)
        for item in series:
            any_valid |= item.valid
        coverage = round(float(any_valid.mean()), 3)
    else:
        coverage = 0.0

    points = built["points"]
    # Backwards compatibility: the old API exposed ≤10 keyframes.
    keyframes: list[dict[str, float]] = []
    if points:
        step = max(1, len(points) // 10)
        keyframes = [{"t": point["t"], "x": point["x"]} for point in points[::step]]

    # Draft shot list (Tahap 7). Cheap: it only reads the activity timeline
    # built above. Stored in the cache so a render never re-derives cuts.
    try:
        per_face_talk = getattr(decision, "talk", None)
        if per_face_talk is None:
            per_face_talk = (
                decision.talking if decision.talking is not None else np.zeros(steps, dtype=bool)
            )
        shot_list, shot_stats = shot_draft.build_draft(
            times,
            decision.target,
            per_face_talk,
            params,
            start=start,
            end=end,
            person_labels=list(labels or []),
            speech=speech,
            overlap=getattr(decision, "overlap", None),
        )
    except Exception as exc:  # never fail analysis because of the shot draft
        shot_list, shot_stats = [], {"error": f"{type(exc).__name__}: {exc}"}

    activity = {
        **built.get("activity", {}),
        "speech": speech.astype(np.int8).tolist() if speech is not None else [],
    }
    return {
        "face": face,
        "coverage": coverage,
        "keyframes": keyframes,
        "trajectory": points,
        "raw": built.get("raw", {}),
        "activity": activity,
        "shots": shot_list,
        "shot_metrics": shot_stats,
        "metrics": built["metrics"],
        "faces": built["faces"],
    }


def _round_list(values: np.ndarray, digits: int = 5) -> list[float]:
    return [round(float(value), digits) for value in np.nan_to_num(np.asarray(values, dtype=float))]


def series_payload(series: list[Any], times: np.ndarray) -> list[dict[str, Any]]:
    """Per-face series on the shared step grid, small enough to cache as JSON.

    The People panel, the "follow person" shot kind and the identity suggestions
    all read this, so every consumer sees the same numbers.
    """
    out: list[dict[str, Any]] = []
    for index, item in enumerate(series):
        valid = np.asarray(item.valid, dtype=bool)
        best = None
        if valid.any():
            area = np.where(valid, np.nan_to_num(item.w) * np.nan_to_num(item.h), 0.0)
            pick = int(np.argmax(area))
            best = {
                "t": round(float(times[pick]), 3),
                "cx": round(float(item.cx[pick]), 5),
                "cy": round(float(item.cy[pick]), 5),
                "w": round(float(item.w[pick]), 5),
                "h": round(float(item.h[pick]), 5),
            }
        out.append(
            {
                "id": index,
                "label": _person_label(index),
                "cx": _round_list(item.cx),
                "cy": _round_list(item.cy),
                "w": _round_list(item.w),
                "h": _round_list(item.h),
                "jaw": _round_list(item.jaw, 4),
                "valid": valid.astype(np.int8).tolist(),
                "detections": int(getattr(item, "detections", 0) or 0),
                "weight": round(float(item.weight), 6),
                "coverage": round(float(item.coverage()), 3),
                "best": best,
            }
        )
    return out


def series_from_payload(raw: list[dict[str, Any]]) -> list[Any]:
    """Inverse of :func:`series_payload` — rebuild TrackSeries for a re-run."""
    out = []
    for item in raw:
        out.append(
            tracker.TrackSeries(
                id=int(item.get("id") or 0),
                cx=np.asarray(item.get("cx") or [], dtype=float),
                cy=np.asarray(item.get("cy") or [], dtype=float),
                w=np.asarray(item.get("w") or [], dtype=float),
                h=np.asarray(item.get("h") or [], dtype=float),
                jaw=np.asarray(item.get("jaw") or [], dtype=float),
                valid=np.asarray(item.get("valid") or [], dtype=bool),
                detections=int(item.get("detections") or 0),
            )
        )
    return out


def times_from_payload(payload: dict[str, Any]) -> np.ndarray:
    points = payload.get("trajectory") or []
    if points:
        return np.asarray([float(point["t"]) for point in points], dtype=float)
    duration = float(payload.get("duration") or 0.0)
    return np.linspace(0.0, max(0.2, duration), 2)


def rebuild(
    payload: dict[str, Any],
    state: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-derive framing, activity and draft shots from a corrected people list.

    Nothing is decoded: the per-face series were cached with the analysis, so an
    "ignore / merge / split" round trip is a few milliseconds of NumPy instead of
    a new detection pass.
    """
    from backend.services import people as people_mod

    raw = payload.get("series") or []
    if not raw or not payload.get("raw"):
        return payload  # legacy cache entry: nothing to re-derive from
    times = times_from_payload(payload)
    if times.size != len(raw[0].get("cx") or []):
        return payload

    params = dict(params or tracking_config.resolve(payload.get("clip_type") or "auto"))
    size = (payload.get("metrics") or {}).get("src_size") or []
    src_w, src_h = (int(size[0]), int(size[1])) if len(size) == 2 else (1920, 1080)

    speech_raw = (payload.get("activity") or {}).get("speech") or []
    speech = None
    if len(speech_raw) == times.size:
        speech = np.asarray(speech_raw, dtype=np.int8).astype(bool)

    effective, labels = people_mod.effective_series(raw, state, times)
    built = _finish(
        effective,
        times,
        params,
        start=float(payload.get("start") or 0.0),
        end=float(payload.get("end") or 0.0),
        src_w=src_w,
        src_h=src_h,
        speech=speech,
        labels=labels,
    )
    out = dict(payload)
    out.update(
        {
            "face": built["face"],
            "coverage": built["coverage"],
            "keyframes": built["keyframes"],
            "trajectory": built["trajectory"],
            "raw": built["raw"],
            "activity": built["activity"],
            "shots": [shot.to_dict() for shot in built["shots"]],
            "shot_metrics": built["shot_metrics"],
            "metrics": built["metrics"],
            "faces": built["faces"],
            "people_applied": True,
            "person_index": people_mod.person_index(state, labels),
        }
    )
    return out

