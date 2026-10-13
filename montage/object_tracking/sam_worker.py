"""SAM 2.1 worker (runs ONLY under the isolated sam2 venv, never in app env).

Usage:
    sam2-python -u sam_worker.py <session_dir> <task.json>

Reads the task, (re)computes the requested frame span with the SAM 2.1
video predictor, appends compact samples to ``tracking.jsonl`` and prints
JSON progress lines on stdout.  Exits with code 3 when ``cancel.flag``
appears in the session dir.  Memory is freed by process exit.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from montage.object_tracking import storage as ot_storage_mod  # noqa: E402
_rle_encode = ot_storage_mod.encode_rle
print(f"DBG worker import: storage={ot_storage_mod.__file__}", file=sys.stderr, flush=True)
from typing import Optional


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def fail(message: str) -> int:
    emit({"type": "error", "message": message})
    return 1


def mask_stats(mask) -> dict:
    import numpy as np
    arr = (np.asarray(mask) > 0).astype(np.uint8)
    h, w = arr.shape[:2]
    area = float(arr.mean())
    if area <= 0:
        return {"x": 0.5, "y": 0.5, "bbox": None, "area": 0.0,
                "visible": False, "mask": None}
    ys, xs = np.nonzero(arr)
    # Largest connected component for the tracking point (decoy-robust).
    try:
        import cv2
        count, _, stats, centroids = cv2.connectedComponentsWithStats(arr, connectivity=8)
        if count > 1:
            import numpy as _np
            largest = 1 + int(_np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            cx, cy = centroids[largest]
        else:
            cx, cy = float(xs.mean()), float(ys.mean())
    except ImportError:
        cx, cy = float(xs.mean()), float(ys.mean())
    return {
        "x": float(cx) / max(1, w - 1),
        "y": float(cy) / max(1, h - 1),
        "bbox": [float(xs.min()) / max(1, w - 1), float(ys.min()) / max(1, h - 1),
                 float(xs.max()) / max(1, w - 1), float(ys.max()) / max(1, h - 1)],
        "area": area,
        "visible": True,
        "mask": _rle_encode(arr),
    }


def probe_rotation(source: Path) -> int:
    """Display rotation in tag convention (same numbers the engine uses)."""
    import json as _json
    try:
        ffprobe = shutil.which("ffprobe")
        if not ffprobe:
            return 0
        proc = subprocess.run(
            [ffprobe, "-v", "error",
             "-show_entries", "stream=codec_type:stream_tags=rotate",
             "-show_entries", "stream_side_data=rotation",
             "-of", "json", str(source)],
            capture_output=True, text=True, timeout=60)
        probe = _json.loads(proc.stdout or "{}")
    except Exception:
        return 0
    for stream in probe.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        tags = stream.get("tags") or {}
        if tags.get("rotate") not in (None, ""):
            try:
                return int(float(str(tags["rotate"]))) % 360
            except (TypeError, ValueError):
                pass
        for side in list(stream.get("side_data_list") or []) + list(stream.get("side_data") or []):
            if isinstance(side, dict) and side.get("rotation") not in (None, ""):
                try:
                    return int(round(float(str(side["rotation"])))) % 360
                except (TypeError, ValueError):
                    pass
    return 0


def downsample_clip(source: Path, workdir: Path, fps: float) -> tuple[Path, float]:
    """Transcode to the analysis fps; returns (clip, actual_fps).

    The clip is stored upright (rotation normalized like the engine does),
    so cv2 dims, SAM frames and user coords always agree.
    """
    import cv2
    probe = cv2.VideoCapture(str(source))
    src_fps = float(probe.get(cv2.CAP_PROP_FPS) or 0) or 30.0
    total = int(probe.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    probe.release()
    target = min(float(fps or 6.0), src_fps)
    rotation = probe_rotation(source)
    transpose = ""
    if rotation == 270:
        transpose = "transpose=1,"
    elif rotation == 90:
        transpose = "transpose=2,"
    elif rotation == 180:
        transpose = "transpose=2,transpose=2,"
    if target >= src_fps - 1e-6 and not transpose:
        return source, src_fps
    clip = workdir / f"analysis_{target:g}fps_r{rotation}.mp4"
    if not clip.is_file():
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-noautorotate", "-i", str(source),
             "-vf", f"{transpose}fps={target}",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-pix_fmt", "yuv420p", "-an", str(clip)],
            capture_output=True, text=True)
        if proc.returncode != 0 or not clip.is_file():
            return source, src_fps
    import cv2 as _cv2
    actual = float(_cv2.VideoCapture(str(clip)).get(_cv2.CAP_PROP_FPS) or target)
    return clip, actual or target


def run_segment(task: dict, video: Path, device, model_cfg: str,
                checkpoint: str) -> int:
    """Single-frame mask for the selection preview (no tracking)."""
    import cv2
    import numpy as np
    import torch
    from sam2.build_sam import build_sam2
    from sam2.sam2_image_predictor import SAM2ImagePredictor

    frame_idx = max(0, int(task.get("frame_idx", 0)))
    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    frame_idx = min(frame_idx, max(0, total - 1))
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        return fail("cannot read frame")
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    predictor = SAM2ImagePredictor(build_sam2(model_cfg, checkpoint, device=device))
    predictor.set_image(rgb)
    prompt = (task.get("prompts") or [{}])[0]
    pts = [[float(p[0]) * rgb.shape[1], float(p[1]) * rgb.shape[0]]
           for p in prompt.get("points") or []]
    labels = [1 if int(p[2]) >= 0 else 0 for p in prompt.get("points") or []]
    box = prompt.get("box")
    box_px = None
    if box:
        box_px = np.array([float(box[0]) * rgb.shape[1], float(box[1]) * rgb.shape[0],
                           float(box[2]) * rgb.shape[1], float(box[3]) * rgb.shape[0]])
    # SAM 2 stores memory in bfloat16: compute must match (MPS aborts and
    # CPU errors on mixed dtypes without autocast).
    with torch.autocast(device.type, dtype=torch.bfloat16):
        masks, scores, _ = predictor.predict(
            point_coords=np.array(pts) if pts else None,
            point_labels=np.array(labels) if labels else None,
            box=box_px, multimask_output=False)
    mask = masks[0]
    stats = mask_stats(mask)
    stats["score"] = round(float(scores[0]), 4)
    stats["frame"] = frame_idx
    stats.pop("mask", None)  # preview mask travels separately, not in the line
    emit({"type": "mask", **stats})
    rle = _rle_encode(mask)
    if rle is not None:
        emit({"type": "mask_rle", "frame": frame_idx, "rle": rle})
    return 0


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: sam_worker.py <session_dir> <task.json>", file=sys.stderr)
        return 2
    session_dir = Path(sys.argv[1])
    try:
        task = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    except Exception as exc:
        return fail(f"bad task.json: {exc}")

    video = Path(str(task.get("video") or ""))
    if not video.is_file():
        return fail("source video not found")
    action = str(task.get("action") or "track")
    device_name = str(task.get("device") or "auto")
    checkpoint = str(task.get("checkpoint") or "")
    model_cfg = str(task.get("model_cfg") or "configs/sam2.1/sam2.1_hiera_s.yaml")
    if not checkpoint or not Path(checkpoint).is_file():
        return fail("SAM checkpoint not found")

    import torch
    if device_name == "auto":
        device_name = "mps" if torch.backends.mps.is_available() else "cpu"
    device = torch.device(device_name)
    emit({"type": "device", "device": device_name})

    if action == "segment":
        return run_segment(task, video, device, model_cfg, checkpoint)
    if action != "track":
        return fail(f"unknown action: {action}")

    prompts = task.get("prompts") or []
    if not prompts:
        return fail("no prompts (select the object first)")
    span = task.get("span") or {}
    start_frame = max(0, int(span.get("start_frame", 0)))
    end_frame = max(start_frame, int(span.get("end_frame", start_frame)))
    analysis_fps = float(task.get("analysis_fps", 6.0) or 6.0)

    workdir = session_dir / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    cancel_flag = session_dir / "cancel.flag"
    if cancel_flag.exists():
        try:
            cancel_flag.unlink()
        except OSError:
            pass

    clip, fps = downsample_clip(video, workdir, analysis_fps)
    # SAM reads mp4 via decord (often unavailable); a JPEG dir always works.
    frames_dir = workdir / "frames"
    marker = frames_dir / ".ready"
    if not marker.is_file():
        shutil.rmtree(frames_dir, ignore_errors=True)
        frames_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(clip), "-q:v", "3", str(frames_dir / "%05d.jpg")],
            capture_output=True, text=True)
        if proc.returncode != 0 or not any(frames_dir.glob("*.jpg")):
            return fail("cannot extract frames for analysis")
        marker.write_text("ok", encoding="utf-8")
    # Map requested source-frame span to analysis-clip frames.
    import cv2
    probe = cv2.VideoCapture(str(video))
    src_fps = float(probe.get(cv2.CAP_PROP_FPS) or 30.0) or 30.0
    probe.release()
    ratio = fps / src_fps if src_fps > 0 else 1.0
    clip_start = int(round(start_frame * ratio))
    clip_end = int(round(end_frame * ratio))

    from sam2.build_sam import build_sam2_video_predictor
    import numpy as np
    predictor = build_sam2_video_predictor(model_cfg, checkpoint, device=device)
    state = predictor.init_state(video_path=str(frames_dir))
    num_frames = int(state["num_frames"])
    clip_start = max(0, min(clip_start, num_frames - 1))
    clip_end = max(clip_start, min(clip_end, num_frames - 1))

    # Prompts live in normalized upright-frame coords; SAM wants pixels of
    # the analysis clip (same aspect, letterbox-free).
    probe = cv2.VideoCapture(str(clip))
    clip_w = int(probe.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    clip_h = int(probe.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    probe.release()
    for prompt in prompts:
        # Prompt frames arrive in source-video numbering; SAM needs clip indices.
        frame_idx = max(0, min(int(round(int(prompt.get("frame_idx", clip_start)) * ratio)), num_frames - 1))
        pts, labels = [], []
        for point in prompt.get("points") or []:
            pts.append([float(point[0]) * clip_w, float(point[1]) * clip_h])
            labels.append(1 if int(point[2]) >= 0 else 0)
        box = prompt.get("box")
        box_px = None
        if box:
            box_px = np.array([float(box[0]) * clip_w, float(box[1]) * clip_h,
                               float(box[2]) * clip_w, float(box[3]) * clip_h])
        with torch.autocast(device.type, dtype=torch.bfloat16):
            predictor.add_new_points_or_box(
                state, frame_idx=frame_idx, obj_id=1,
                points=np.array(pts) if pts else None,
                labels=np.array(labels) if labels else None,
                box=box_px)

    tracking_path = session_dir / "tracking.jsonl"
    # Selection frame in clip coords: forward goes selection -> end,
    # reverse goes selection -> start.
    sel_clip = max(clip_start, min(int(round(
        float(task.get("selection_frame", start_frame)) * ratio)), clip_end))
    total = max(1, (clip_end - sel_clip + 1) + (sel_clip - clip_start + 1))
    done = 0
    t_start = time.time()
    # Same autocast rule as segment: memory is bfloat16, compute must match.
    with torch.autocast(device.type, dtype=torch.bfloat16):
        for name, reverse, max_track in (
                ("forward", False, clip_end - sel_clip + 1),
                ("reverse", True, sel_clip - clip_start + 1)):
            if max_track <= 0:
                continue
            for frame_idx, _, pred_masks in predictor.propagate_in_video(
                    state, start_frame_idx=sel_clip, reverse=reverse,
                    max_frame_num_to_track=max_track):
                if cancel_flag.exists():
                    emit({"type": "cancelled", "done": done, "total": total})
                    return 3
                if not reverse and frame_idx > clip_end:
                    break
                if reverse and frame_idx < clip_start:
                    break
                raw = pred_masks[0][0]
                mask = (raw.to("cpu").float().numpy() > 0.0)
                stats = mask_stats(mask)
                sample = {
                    "frame": int(round(frame_idx / ratio)),
                    "time": round(frame_idx / fps, 6) if fps else 0.0,
                    "x": round(stats["x"], 6), "y": round(stats["y"], 6),
                    "bbox": stats["bbox"], "area": round(stats["area"], 6),
                    "visible": bool(stats["visible"]),
                    "direction": name,
                    "mask": stats["mask"],
                }
                with tracking_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(sample) + "\n")
                done += 1
                emit({"type": "progress", "done": done, "total": total,
                      "frame": sample["frame"], "direction": name,
                      "elapsed": round(time.time() - t_start, 1)})
    emit({"type": "done", "done": done,
          "elapsed": round(time.time() - t_start, 1)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
