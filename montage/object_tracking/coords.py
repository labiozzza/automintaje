"""Coordinate mapping: browser clicks -> source frame pixels.

The browser shows the video with ``object-fit: contain`` inside a sized
element, so clicks must be mapped through the letterbox math — never used
raw.  All stored prompts use normalized 0..1 coordinates in the *upright*
frame space (rotation metadata already applied, no mirror).
"""
from __future__ import annotations

import math
from typing import Any, Optional


def contain_mapping(
    element_w: float,
    element_h: float,
    video_w: float,
    video_h: float,
) -> dict[str, float]:
    """Return the displayed-video rect inside the element (contain fit)."""
    if element_w <= 0 or element_h <= 0 or video_w <= 0 or video_h <= 0:
        raise ValueError("positive element/video sizes are required")
    scale = min(element_w / video_w, element_h / video_h)
    disp_w = video_w * scale
    disp_h = video_h * scale
    return {
        "x": (element_w - disp_w) / 2.0,
        "y": (element_h - disp_h) / 2.0,
        "w": disp_w,
        "h": disp_h,
        "scale": scale,
    }


def click_to_normalized(
    click_x: float,
    click_y: float,
    rect: dict[str, float],
    element_w: float,
    element_h: float,
    video_w: float,
    video_h: float,
    device_pixel_ratio: float = 1.0,
) -> Optional[dict[str, float]]:
    """Map a browser click (CSS px, element-relative) to 0..1 frame coords.

    Returns ``None`` when the click lands on the letterbox area.
    """
    del device_pixel_ratio  # CSS px are already DPR-independent
    mapping = contain_mapping(element_w, element_h, video_w, video_h)
    fx = (click_x - mapping["x"]) / mapping["w"]
    fy = (click_y - mapping["y"]) / mapping["h"]
    if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
        return None
    _ = rect  # reserved: element offset already subtracted by the caller
    return {"x": fx, "y": fy}


def normalized_to_pixels(point: dict[str, float], width: int, height: int) -> tuple[int, int]:
    return (
        min(width - 1, max(0, int(round(float(point["x"]) * width)))),
        min(height - 1, max(0, int(round(float(point["y"]) * height)))),
    )


def normalize_box(box: dict[str, float]) -> Optional[dict[str, float]]:
    """Normalize an {x0,y0,x1,y1} box given in 0..1 coords (order-safe)."""
    try:
        x0, y0, x1, y1 = (float(box["x0"]), float(box["y0"]),
                          float(box["x1"]), float(box["y1"]))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x0, y0, x1, y1)):
        return None
    x0, x1 = sorted((min(max(x0, 0.0), 1.0), min(max(x1, 0.0), 1.0)))
    y0, y1 = sorted((min(max(y0, 0.0), 1.0), min(max(y1, 0.0), 1.0)))
    if x1 - x0 < 1e-4 or y1 - y0 < 1e-4:
        return None
    return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}


def mirror_point_x(x: float) -> float:
    """Mirror a normalized x for the mirrored render variant."""
    return 1.0 - max(0.0, min(1.0, float(x)))


def mirror_box(box: dict[str, float]) -> dict[str, float]:
    return {"x0": 1.0 - box["x1"], "y0": box["y0"],
            "x1": 1.0 - box["x0"], "y1": box["y1"]}


def mask_centroid(mask: Any) -> Optional[dict[str, float]]:
    """Stable tracking point: centroid of the largest connected component."""
    try:
        import numpy as np
    except ImportError:
        return None
    arr = np.asarray(mask)
    if arr.size == 0 or not arr.any():
        return None
    binary = (arr > 0).astype(np.uint8)
    components: int = 0
    try:
        import cv2
        count, labels, stats, centroids = cv2.connectedComponentsWithStats(
            binary, connectivity=8)
        components = count
        if count <= 1:
            return None
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        cx, cy = centroids[largest]
    except ImportError:
        ys, xs = np.nonzero(binary)
        if len(xs) == 0:
            return None
        cx, cy = float(xs.mean()), float(ys.mean())
        components = 2
    h, w = binary.shape[:2]
    return {"x": float(cx) / max(1, w - 1), "y": float(cy) / max(1, h - 1),
            "components": components}


def mask_bbox(mask: Any) -> Optional[dict[str, float]]:
    """Bounding box of all mask pixels, normalized 0..1."""
    try:
        import numpy as np
    except ImportError:
        return None
    arr = np.asarray(mask)
    if arr.size == 0 or not (arr > 0).any():
        return None
    ys, xs = np.nonzero(arr > 0)
    h, w = arr.shape[:2]
    return {"x0": float(xs.min()) / max(1, w - 1),
            "y0": float(ys.min()) / max(1, h - 1),
            "x1": float(xs.max()) / max(1, w - 1),
            "y1": float(ys.max()) / max(1, h - 1)}


def mask_area_fraction(mask: Any) -> float:
    try:
        import numpy as np
    except ImportError:
        return 0.0
    arr = np.asarray(mask)
    if arr.size == 0:
        return 0.0
    return float((arr > 0).mean())


def check_consistency(previous: dict[str, Any], current: dict[str, Any],
                       max_area_ratio: float = 4.0,
                       max_center_jump: float = 0.25) -> dict[str, Any]:
    """Geometric consistency between two consecutive samples.

    Returns raw metrics plus a ``suspicious`` flag.  The metrics are reported
    as-is; no calibrated confidence is fabricated (spec 7.3).
    """
    prev_area = float(previous.get("area", 0.0) or 0.0)
    cur_area = float(current.get("area", 0.0) or 0.0)
    area_ratio = (cur_area / prev_area) if prev_area > 0 else (float("inf") if cur_area > 0 else 1.0)
    dx = float(current.get("x", 0.5)) - float(previous.get("x", 0.5))
    dy = float(current.get("y", 0.5)) - float(previous.get("y", 0.5))
    jump = math.hypot(dx, dy)
    reasons = []
    if cur_area <= 0:
        reasons.append("empty_mask")
    if area_ratio > max_area_ratio or (area_ratio < 1.0 / max_area_ratio and cur_area > 0):
        reasons.append(f"area_ratio={area_ratio:.2f}")
    if jump > max_center_jump:
        reasons.append(f"center_jump={jump:.3f}")
    return {"area_ratio": area_ratio, "center_jump": jump,
            "suspicious": bool(reasons), "reasons": reasons}


__all__ = [
    "contain_mapping",
    "click_to_normalized",
    "normalized_to_pixels",
    "normalize_box",
    "mirror_point_x",
    "mirror_box",
    "mask_centroid",
    "mask_bbox",
    "mask_area_fraction",
    "check_consistency",
]
