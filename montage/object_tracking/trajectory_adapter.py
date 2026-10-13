"""Object-camera trajectory adapter.

Reuses the pure ``camera_trajectory`` math (the same code path as eye
modes) without changing eye behaviour: object samples are translated into
the anchor format the shared ``compute_camera_track`` already understands.
Eye modes never pass through here.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

try:
    from ..camera_trajectory import CameraSettings, compute_camera_track
except ImportError:  # run/imported without package context
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from montage.camera_trajectory import (  # noqa: E402
        CameraSettings, compute_camera_track,
    )

try:
    from .coords import mirror_point_x
except ImportError:
    from montage.object_tracking.coords import (  # noqa: E402
        mirror_point_x,
    )


OBJECT_MODES = ("object_hold", "object_live")


def samples_to_anchors(samples: list[dict[str, Any]]) -> tuple[
        list[int], list[Optional[tuple[float, float]]], list[tuple[int, int]]]:
    """Split stored samples into frames, anchor points and loss spans.

    Invisible or geometrically suspicious samples become ``None`` (the shared
    math then holds the last good position); contiguous bad ranges are
    returned as loss spans for the UI timeline.
    """
    frames: list[int] = []
    points: list[Optional[tuple[float, float]]] = []
    loss: list[tuple[int, int]] = []
    open_start: Optional[int] = None
    for sample in samples:
        frame = int(sample.get("frame", 0))
        frames.append(frame)
        good = bool(sample.get("visible")) and not bool(sample.get("suspicious"))
        if good:
            points.append((float(sample.get("x", 0.5)), float(sample.get("y", 0.5))))
            if open_start is not None:
                loss.append((open_start, frame))
                open_start = None
        else:
            points.append(None)
            if open_start is None:
                open_start = frame
    if open_start is not None and frames:
        loss.append((open_start, frames[-1]))
    return frames, points, loss


def object_camera_settings(raw: dict[str, Any]) -> CameraSettings:
    raw = raw or {}
    mode = str(raw.get("mode") or "object_hold")
    base = "live" if mode == "object_live" else "hold"
    return CameraSettings(
        mode=base,
        eye_x=float(raw.get("eye_x", 0.50)),
        eye_y=float(raw.get("eye_y", 0.50)),
        follow_strength=min(1.0, max(0.0, float(raw.get("follow_strength", 0.30)))),
        lag_seconds=max(0.0, float(raw.get("lag_seconds", 0.40))),
        hold_seconds=max(0.0, float(raw.get("hold_seconds", 0.25))),
        min_zoom=min(1.25, max(1.05, float(raw.get("min_zoom", 1.12)))),
        max_speed=max(0.0, float(raw.get("max_speed", 0.35))),
        wobble=bool(raw.get("wobble", False)),
    )


def compute_object_track(
    samples: list[dict[str, Any]],
    fps: float,
    total_frames: int,
    timeline_zoom: list[float],
    raw_settings: dict[str, Any],
    mirrored: bool = False,
) -> tuple[list[float], list[float], list[float], list[tuple[int, int]]]:
    """Per-frame (anchor_x, anchor_y, zoom) plus loss spans for the UI."""
    frames, points, loss = samples_to_anchors(samples)
    settings = object_camera_settings(raw_settings)
    xs, ys, zs = compute_camera_track(
        frames, points, fps, total_frames, timeline_zoom, settings)
    if mirrored:
        xs = [mirror_point_x(x) for x in xs]
    return xs, ys, zs, loss


__all__ = ["OBJECT_MODES", "samples_to_anchors", "object_camera_settings",
           "compute_object_track"]
