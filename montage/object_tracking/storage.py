"""Session storage for Object Tracking (main-process side, no heavy deps).

Layout inside a session dir::

    selection.json   prompt set, frame/time, model info, status
    tracking.jsonl   one compact sample per analyzed frame (RLE masks)
    preview/         overlay JPEGs for scrubbing (written on demand)
    logs/            worker logs

Masks are stored as COCO-style RLE strings (counts), never as full arrays
inside JSON.  ``tracking.jsonl`` is append-friendly for resume/re-correction.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA_VERSION = 1


def session_files(session_dir: Path) -> dict[str, Path]:
    session_dir = Path(session_dir)
    return {
        "selection": session_dir / "selection.json",
        "tracking": session_dir / "tracking.jsonl",
        "preview": session_dir / "preview",
        "logs": session_dir / "logs",
        "cancel": session_dir / "cancel.flag",
        "task": session_dir / "task.json",
    }


def ensure_session(session_dir: Path) -> dict[str, Path]:
    files = session_files(session_dir)
    files["preview"].mkdir(parents=True, exist_ok=True)
    files["logs"].mkdir(parents=True, exist_ok=True)
    return files


def video_fingerprint(path: Path) -> str:
    """Stable id binding a session to an exact source file."""
    try:
        stat = Path(path).stat()
        blob = f"{Path(path).resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode()
    except OSError:
        blob = str(path).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def write_selection(session_dir: Path, payload: dict[str, Any]) -> Path:
    files = ensure_session(session_dir)
    payload = dict(payload)
    payload.setdefault("schema_version", SCHEMA_VERSION)
    payload.setdefault("updated_at", time.time())
    files["selection"].write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return files["selection"]


def read_selection(session_dir: Path) -> Optional[dict[str, Any]]:
    path = session_files(session_dir)["selection"]
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def append_sample(session_dir: Path, sample: dict[str, Any]) -> None:
    files = ensure_session(session_dir)
    with files["tracking"].open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(sample, ensure_ascii=False) + "\n")


def iter_samples(session_dir: Path) -> Iterator[dict[str, Any]]:
    path = session_files(session_dir)["tracking"]
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                sample = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(sample, dict):
                yield sample


def clear_tracking(session_dir: Path) -> None:
    path = session_files(session_dir)["tracking"]
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def encode_rle(mask: Any, max_side: int = 256) -> Optional[dict[str, Any]]:
    """COCO-style RLE of a (downscaled) binary mask."""
    try:
        import numpy as np
    except ImportError:
        return None
    arr = (np.asarray(mask) > 0).astype(np.uint8)
    if arr.size == 0 or not arr.any():
        return None
    h, w = arr.shape[:2]
    scale = min(1.0, max_side / max(h, w))
    if scale < 1.0:
        try:
            import cv2
            small = cv2.resize(arr, (max(1, int(w * scale)), max(1, int(h * scale))),
                              interpolation=cv2.INTER_NEAREST)
        except ImportError:
            step = max(1, int(round(1.0 / scale)))
            small = arr[::step, ::step]
    else:
        small = arr
    flat = small.reshape(-1, order="F")
    counts: list[int] = []
    current = 0
    run = 0
    for value in flat:
        if int(value) == current:
            run += 1
        else:
            counts.append(run)
            current = int(value)
            run = 1
    counts.append(run)
    if flat[0] == 1:
        counts.insert(0, 0)
    return {"counts": counts, "size": [int(small.shape[0]), int(small.shape[1])],
            "orig": [int(h), int(w)]}


def decode_rle(rle: dict[str, Any]) -> Optional[Any]:
    """Decode an RLE dict back to a uint8 mask (upscaled to orig size)."""
    try:
        import numpy as np
    except ImportError:
        return None
    try:
        h, w = int(rle["size"][0]), int(rle["size"][1])
        counts = [int(c) for c in rle["counts"]]
    except (KeyError, TypeError, ValueError):
        return None
    flat = np.zeros(h * w, dtype=np.uint8)
    pos = 0
    value = 0
    for run in counts:
        flat[pos:pos + run] = value
        pos += run
        value = 1 - value
    mask = flat.reshape((h, w), order="F")
    orig = rle.get("orig") or [h, w]
    if (int(orig[0]), int(orig[1])) != (h, w):
        try:
            import cv2
            mask = cv2.resize(mask, (int(orig[1]), int(orig[0])),
                              interpolation=cv2.INTER_NEAREST)
        except ImportError:
            pass
    return mask


def prune_session(session_dir: Path, max_age_seconds: float = 7 * 24 * 3600) -> bool:
    """Delete a session dir if older than TTL. Returns True when removed."""
    session_dir = Path(session_dir)
    try:
        age = time.time() - session_dir.stat().st_mtime
    except OSError:
        return False
    if age < max_age_seconds:
        return False
    import shutil
    shutil.rmtree(session_dir, ignore_errors=True)
    return True


__all__ = [
    "SCHEMA_VERSION",
    "session_files",
    "ensure_session",
    "video_fingerprint",
    "write_selection",
    "read_selection",
    "append_sample",
    "iter_samples",
    "clear_tracking",
    "encode_rle",
    "decode_rle",
    "prune_session",
]
