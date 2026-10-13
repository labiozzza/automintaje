"""Virtual-camera trajectory math (pure functions, no video I/O).

Two modes drive the crop anchor (the source point placed at the fixed screen
position by ``apply_face_zoom_v5.transform_frame``):

* ``hold`` — keep the eyes near ``eye_screen`` (default 0.50 x 0.42) with a
  fast but jitter-free smoothed camera.
* ``live`` — partially follow the head/eyes: interpolate between a static
  crop (from the initial stable eye position) and the full-hold position via
  ``follow_strength``, then apply lag for inertia.  At 0 the camera is
  (almost) static, at 1 it behaves like ``hold``.

Design rules enforced here:

* ``follow_strength`` moves the virtual camera, it is NOT a smoothing factor.
* Effective zoom is ``max(timeline_zoom, min_zoom)`` — never a product.
* The crop never leaves the source frame (no black bars); when geometry makes
  exact eye placement impossible the eyes are allowed to drift instead.
* Lost detections hold the last valid position; on recovery the camera glides
  back (the lag filter does this naturally).
* Single-sample outliers (blinks, glitches, face swaps) are rejected.
* Everything is deterministic for identical inputs (wobble included).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple


CENTER_EYE = (0.5, 0.36)


@dataclass
class CameraSettings:
    mode: str = "off"  # off | hold | live
    eye_x: float = 0.50
    eye_y: float = 0.42
    follow_strength: float = 0.30  # live only, 0..1
    lag_seconds: float = 0.40  # live only, inertia time constant
    hold_seconds: float = 0.25  # hold only, smoothing time constant
    min_zoom: float = 1.12
    max_speed: float = 0.35  # normalized anchor units per second
    max_jump: float = 0.12  # per-sample outlier rejection, normalized units
    reference_seconds: float = 2.0  # live static-position calibration window
    wobble: bool = False
    wobble_amp: float = 0.0015  # normalized units, capped


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _exp_alpha(dt: float, tau: float) -> float:
    if tau <= 0 or dt <= 0:
        return 1.0
    return 1.0 - math.exp(-dt / tau)


def clean_eye_series(
    points: Sequence[Optional[Tuple[float, float]]],
    max_jump: float,
) -> List[Optional[Tuple[float, float]]]:
    """Reject single-sample spikes; ``None`` (lost face) passes through.

    A sample is a spike only when it disagrees with BOTH neighbours by more
    than *max_jump*: a genuine fast move (step) agrees with the samples on
    one side and is kept, while an isolated glitch (blink artefact,
    misdetection, face swap) is replaced by the neighbours' midpoint.
    """
    indexed = [(( _clamp01(p[0]), _clamp01(p[1])) if p is not None
                and all(math.isfinite(v) for v in p) else None) for p in points]
    cleaned: List[Optional[Tuple[float, float]]] = list(indexed)
    if max_jump <= 0:
        return cleaned
    for i, point in enumerate(indexed):
        if point is None:
            continue
        prev = next((indexed[j] for j in range(i - 1, -1, -1)
                     if indexed[j] is not None), None)
        nxt = next((indexed[j] for j in range(i + 1, len(indexed))
                    if indexed[j] is not None), None)
        if prev is None or nxt is None:
            continue
        if (math.hypot(point[0] - prev[0], point[1] - prev[1]) > max_jump
                and math.hypot(point[0] - nxt[0], point[1] - nxt[1]) > max_jump):
            cleaned[i] = ((prev[0] + nxt[0]) / 2.0, (prev[1] + nxt[1]) / 2.0)
    return cleaned


def hold_last_valid(
    points: Sequence[Optional[Tuple[float, float]]],
    fallback: Tuple[float, float] = CENTER_EYE,
) -> List[Tuple[float, float]]:
    """Fill ``None`` gaps with the last valid position (or *fallback*)."""
    filled: List[Tuple[float, float]] = []
    last: Tuple[float, float] = (fallback[0], fallback[1])
    for point in points:
        if point is not None:
            last = point
        filled.append(last)
    return filled


def resample_to_frames(
    sample_frames: Sequence[int],
    sample_points: Sequence[Tuple[float, float]],
    total_frames: int,
) -> List[Tuple[float, float]]:
    """Linearly interpolate sparse tracking samples to every output frame."""
    if not sample_frames:
        return [CENTER_EYE] * total_frames
    order = sorted(range(len(sample_frames)), key=lambda i: sample_frames[i])
    frames = [float(sample_frames[i]) for i in order]
    xs = [float(sample_points[i][0]) for i in order]
    ys = [float(sample_points[i][1]) for i in order]
    out: List[Tuple[float, float]] = []
    for frame in range(total_frames):
        t = float(frame)
        if t <= frames[0]:
            out.append((xs[0], ys[0]))
            continue
        if t >= frames[-1]:
            out.append((xs[-1], ys[-1]))
            continue
        for k in range(len(frames) - 1):
            if frames[k] <= t <= frames[k + 1]:
                span = frames[k + 1] - frames[k]
                w = (t - frames[k]) / span if span > 0 else 0.0
                out.append((xs[k] + (xs[k + 1] - xs[k]) * w,
                            ys[k] + (ys[k + 1] - ys[k]) * w))
                break
    return out


def reference_eye(
    points: Sequence[Tuple[float, float]],
    valid: Sequence[bool],
    fps: float,
    window_seconds: float,
) -> Tuple[float, float]:
    """Median eye position over the first stable window (live static base)."""
    limit = max(1, int(round(window_seconds * fps)))
    xs = [p[0] for p, v in zip(points[:limit], valid[:limit]) if v]
    ys = [p[1] for p, v in zip(points[:limit], valid[:limit]) if v]
    if not xs:
        xs = [p[0] for p, v in zip(points, valid) if v]
        ys = [p[1] for p, v in zip(points, valid) if v]
    if not xs:
        return CENTER_EYE
    xs.sort()
    ys.sort()
    return (xs[len(xs) // 2], ys[len(ys) // 2])


def smooth_series(
    points: Sequence[Tuple[float, float]],
    fps: float,
    tau: float,
) -> List[Tuple[float, float]]:
    """Exponential smoothing with a time constant (inertia / de-jitter)."""
    out: List[Tuple[float, float]] = []
    dt = 1.0 / max(1e-6, fps)
    alpha = _exp_alpha(dt, tau)
    current: Optional[Tuple[float, float]] = None
    for point in points:
        if current is None:
            current = point
        else:
            current = (current[0] + alpha * (point[0] - current[0]),
                       current[1] + alpha * (point[1] - current[1]))
        out.append(current)
    return out


def limit_speed(
    points: Sequence[Tuple[float, float]],
    fps: float,
    max_speed: float,
) -> List[Tuple[float, float]]:
    """Cap per-frame travel distance (no visible jumps)."""
    if max_speed <= 0:
        return list(points)
    step = max_speed / max(1e-6, fps)
    out: List[Tuple[float, float]] = []
    current: Optional[Tuple[float, float]] = None
    for point in points:
        if current is None:
            current = point
        else:
            dx, dy = point[0] - current[0], point[1] - current[1]
            dist = math.hypot(dx, dy)
            if dist > step and dist > 0:
                scale = step / dist
                current = (current[0] + dx * scale, current[1] + dy * scale)
            else:
                current = point
        out.append(current)
    return out


def wobble_offset(frame: int, fps: float, amp: float) -> Tuple[float, float]:
    """Deterministic low-amplitude organic drift (two incommensurate sines)."""
    t = frame / max(1e-6, fps)
    dx = amp * (math.sin(2.0 * math.pi * t / 7.3)
                + 0.6 * math.sin(2.0 * math.pi * t / 3.1 + 1.7)) / 1.6
    dy = amp * (math.sin(2.0 * math.pi * t / 5.7 + 0.6)
                + 0.6 * math.sin(2.0 * math.pi * t / 2.3 + 2.9)) / 1.6
    return (dx, dy)


def compute_camera_track(
    sample_frames: Sequence[int],
    sample_points: Sequence[Optional[Tuple[float, float]]],
    fps: float,
    total_frames: int,
    timeline_zoom: Sequence[float],
    settings: CameraSettings,
) -> Tuple[List[float], List[float], List[float]]:
    """Full per-frame camera track: eye anchors (xs, ys) + effective zoom.

    ``sample_points`` holds source-normalized eye positions (or ``None`` for
    lost frames) at ``sample_frames``.  Returns dense per-frame series ready
    for ``transform_frame``.
    """
    fps = max(1e-6, float(fps))
    total_frames = max(1, int(total_frames))
    mode = str(settings.mode or "off").lower()
    if mode not in {"off", "hold", "live"}:
        mode = "off"

    base_zoom = [max(1.0, float(z)) for z in (list(timeline_zoom) or [1.0])]
    if len(base_zoom) < total_frames:
        base_zoom += [base_zoom[-1]] * (total_frames - len(base_zoom))
    base_zoom = base_zoom[:total_frames]
    min_zoom = max(1.0, float(settings.min_zoom or 1.0))
    zs = [z if z >= min_zoom else min_zoom for z in base_zoom]

    if mode == "off":
        return ([float(settings.eye_x)] * total_frames,
                [float(settings.eye_y)] * total_frames, zs)

    cleaned = clean_eye_series(sample_points, settings.max_jump)
    valid = [p is not None for p in cleaned]
    filled = hold_last_valid(cleaned)
    dense = resample_to_frames(sample_frames, filled, total_frames)

    if mode == "hold":
        smoothed = smooth_series(dense, fps, settings.hold_seconds)
        limited = limit_speed(smoothed, fps, settings.max_speed)
    else:  # live
        strength = min(1.0, max(0.0, float(settings.follow_strength)))
        # Static crop from the initial stable eye position, evaluated with
        # the current frame zoom; full-hold target for the current eye.
        ref = reference_eye(filled, valid, fps, settings.reference_seconds)
        static = [ref] * total_frames
        blended = [(
            static[i][0] + (dense[i][0] - static[i][0]) * strength,
            static[i][1] + (dense[i][1] - static[i][1]) * strength,
        ) for i in range(total_frames)]
        lagged = smooth_series(blended, fps, settings.lag_seconds)
        limited = limit_speed(lagged, fps, settings.max_speed)

    if settings.wobble and settings.wobble_amp > 0:
        amp = min(0.005, float(settings.wobble_amp))
        limited = [(p[0] + wobble_offset(i, fps, amp)[0],
                    p[1] + wobble_offset(i, fps, amp)[1])
                   for i, p in enumerate(limited)]

    xs = [_clamp01(p[0]) for p in limited]
    ys = [_clamp01(p[1]) for p in limited]
    return (xs, ys, zs)


def settings_from_dict(raw: dict) -> CameraSettings:
    """Build settings from a render config block (with validation).

    Eye modes (off/hold/live) behave exactly as before; object modes
    (object_hold/object_live) are accepted for the Object Tracking path.
    """
    raw = raw or {}
    mode = str(raw.get("mode") or "off").lower()
    if mode not in {"off", "hold", "live", "object_hold", "object_live"}:
        mode = "off"
    return CameraSettings(
        mode=mode,
        eye_x=float(raw.get("eye_x", 0.50)),
        eye_y=float(raw.get("eye_y", 0.42)),
        follow_strength=min(1.0, max(0.0, float(raw.get("follow_strength", 0.30)))),
        lag_seconds=max(0.0, float(raw.get("lag_seconds", 0.40))),
        hold_seconds=max(0.0, float(raw.get("hold_seconds", 0.25))),
        min_zoom=min(1.25, max(1.05, float(raw.get("min_zoom", 1.12)))),
        max_speed=max(0.0, float(raw.get("max_speed", 0.35))),
        max_jump=max(0.0, float(raw.get("max_jump", 0.12))),
        reference_seconds=max(0.5, float(raw.get("reference_seconds", 2.0))),
        wobble=bool(raw.get("wobble", False)),
        wobble_amp=min(0.005, max(0.0, float(raw.get("wobble_amp", 0.0015)))),
    )


__all__ = [
    "CENTER_EYE",
    "CameraSettings",
    "clean_eye_series",
    "hold_last_valid",
    "resample_to_frames",
    "reference_eye",
    "smooth_series",
    "limit_speed",
    "wobble_offset",
    "compute_camera_track",
    "settings_from_dict",
]
