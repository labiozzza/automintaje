"""Bounded keyframe and transcript analysis for an immutable montage snapshot."""

from __future__ import annotations

import math
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .transcript_map import build_output_transcript


MAX_KEYFRAMES = 18
MAX_FRAME_DIMENSION = 640
MAX_FRAME_BYTES = 2 * 1024 * 1024


class AnalysisCancelled(RuntimeError):
    pass


def _sample_times(duration_ms: int, max_frames: int = MAX_KEYFRAMES) -> List[int]:
    duration_ms = max(1, int(duration_ms))
    count = min(max_frames, max(4, int(math.ceil(duration_ms / 5000.0))))
    if count <= 1:
        return [duration_ms // 2]
    start = min(1000, max(0, duration_ms // 20))
    end = max(start, duration_ms - min(1200, duration_ms // 20))
    if end <= start:
        return [duration_ms // 2]
    return sorted(set(int(round(start + (end - start) * index / (count - 1))) for index in range(count)))


def _extract_frame(video: Path, timestamp_ms: int, output_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    scale = "scale='min(%d,iw)':-2" % MAX_FRAME_DIMENSION
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % (timestamp_ms / 1000.0),
        "-i", str(video), "-map", "0:v:0", "-frames:v", "1", "-vf", scale,
        "-q:v", "5", "-threads", "1", "-y", str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0 or not output_path.is_file():
        detail = (result.stderr or "").strip()[-300:]
        raise RuntimeError("Не удалось извлечь кадр Motion" + (": " + detail if detail else ""))
    if output_path.stat().st_size > MAX_FRAME_BYTES:
        output_path.unlink(missing_ok=True)
        raise RuntimeError("Кадр анализа превышает лимит размера")


def _detect_faces(frame_path: Path) -> List[dict]:
    try:
        import cv2
    except Exception:
        return []
    try:
        cascade = cv2.CascadeClassifier(str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"))
        image = cv2.imread(str(frame_path), cv2.IMREAD_GRAYSCALE)
        if image is None or cascade.empty():
            return []
        height, width = image.shape[:2]
        boxes = cascade.detectMultiScale(image, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24))
        return [
            {
                "x": round(float(x) / width, 4), "y": round(float(y) / height, 4),
                "w": round(float(w) / width, 4), "h": round(float(h) / height, 4),
                "detector": "opencv-haar", "confidence": None,
            }
            for x, y, w, h in boxes[:5]
        ]
    except Exception:
        return []


def _subtitle_zone(params: Optional[dict]) -> dict:
    params = params or {}
    try:
        x = float(params.get("subtitle_x") if params.get("subtitle_x") is not None else 0.12)
        y = float(params.get("subtitle_y") if params.get("subtitle_y") is not None else 0.78)
        scale = float(params.get("subtitle_scale") if params.get("subtitle_scale") is not None else 1.0)
    except (TypeError, ValueError):
        x, y, scale = 0.12, 0.78, 1.0
    height = max(0.05, min(0.22, 0.11 * scale))
    return {
        "x": max(0.0, min(0.9, x)), "y": max(0.0, min(0.95, y - height)),
        "w": 0.86, "h": height, "source": "montage_subtitle_settings",
    }


def analyze_source(snapshot_path: Path, metadata: dict, transcript_segments: Optional[list],
                   srt_text: str, timeline_segments: Optional[list], montage_params: Optional[dict],
                   analysis_dir: Path, cancel_event: Optional[threading.Event] = None) -> dict:
    """Sample a bounded set of output-timeline frames and attach transcript/occupancy data."""
    snapshot = Path(snapshot_path)
    duration_ms = int(metadata.get("duration_ms") or 0)
    if duration_ms <= 0:
        raise ValueError("У Motion-источника отсутствует длительность")
    frame_dir = Path(analysis_dir) / "frames"
    frame_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    luminance = []
    try:
        import cv2
    except Exception:
        cv2 = None
    for index, timestamp_ms in enumerate(_sample_times(duration_ms)):
        if cancel_event is not None and cancel_event.is_set():
            raise AnalysisCancelled("Анализ отменён пользователем")
        frame_path = frame_dir / ("frame_%03d.jpg" % index)
        _extract_frame(snapshot, timestamp_ms, frame_path)
        faces = _detect_faces(frame_path)
        try:
            from PIL import Image
            with Image.open(frame_path) as sampled_image:
                frame_width, frame_height = sampled_image.size
        except Exception:
            frame_width, frame_height = MAX_FRAME_DIMENSION, 0
        mean_luma = None
        if cv2 is not None:
            image = cv2.imread(str(frame_path), cv2.IMREAD_GRAYSCALE)
            if image is not None:
                mean_luma = round(float(image.mean()), 2)
        frame = {
            "time_ms": int(timestamp_ms),
            "file": "analysis/frames/" + frame_path.name,
            "width": int(frame_width),
            "height": int(frame_height),
            "faces": faces,
            "mean_luminance": mean_luma,
        }
        frames.append(frame)
        luminance.append(mean_luma)

    cuts = []
    for index in range(1, len(frames)):
        before, after = luminance[index - 1], luminance[index]
        if before is not None and after is not None and abs(after - before) >= 48:
            cuts.append({"time_ms": frames[index]["time_ms"], "kind": "luminance_change", "strength": round(abs(after - before) / 255.0, 3)})

    transcript = build_output_transcript(transcript_segments, srt_text, timeline_segments)
    occupancy = []
    for frame in frames:
        for face in frame["faces"]:
            occupancy.append({"start_ms": frame["time_ms"], "end_ms": min(duration_ms, frame["time_ms"] + 1500), "kind": "face", "box": face})
    subtitle_zone = _subtitle_zone(montage_params)
    if montage_params and montage_params.get("subtitles", True):
        occupancy.append({"start_ms": 0, "end_ms": duration_ms, "kind": "existing_subtitles", "box": subtitle_zone})

    return {
        "schema_version": "motion-analysis.v1",
        "duration_ms": duration_ms,
        "fps": {"num": int(metadata.get("fps_num") or 0), "den": int(metadata.get("fps_den") or 1)},
        "video": {
            key: metadata.get(key) for key in (
                "width", "height", "coded_width", "coded_height", "rotation",
                "sample_aspect_ratio", "display_aspect_ratio", "video_codec", "audio_codec",
                "audio_present", "color",
            )
        },
        "frames": frames,
        "scene_change_hints": cuts,
        "transcript": transcript,
        "occupancy_map": occupancy,
        "safe_zones": {
            "instagram_reels": {"x": 0.08, "y": 0.14, "w": 0.78, "h": 0.68},
            "youtube_shorts": {"x": 0.08, "y": 0.12, "w": 0.78, "h": 0.72},
            "subtitle_zone": subtitle_zone,
        },
        "limits": {"sampled_frames": len(frames), "max_sampled_frames": MAX_KEYFRAMES},
    }
