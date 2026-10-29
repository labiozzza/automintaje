"""Isolated overlay render, real-source preview, stills and machine visual QA."""

from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List

import cv2
import numpy as np

from montage.color_pipeline import encoder_color_args
from motion.source_snapshot import probe_video

from .sandbox import DockerSceneSandbox
from .workspace import SceneWorkspace


class SceneRenderError(RuntimeError):
    pass


class GeneratedSceneRenderer:
    def __init__(self, project_root: Path, sandbox: DockerSceneSandbox):
        self.project_root = Path(project_root).resolve()
        self.sandbox = sandbox

    def render(self, workspace: SceneWorkspace, source_video: Path, start_ms: int, end_ms: int,
               width: int, height: int, fps_num: int, fps_den: int) -> dict:
        source = Path(source_video).resolve()
        if source.is_symlink() or not source.is_file():
            raise SceneRenderError("Scene source snapshot is unavailable")
        if end_ms <= start_ms or end_ms - start_ms > 12_000:
            raise SceneRenderError("Scene interval must be 0.4–12 seconds")
        duration_ms = int(end_ms) - int(start_ms)
        duration_frames = max(1, int(round(duration_ms * fps_num / (1000.0 * fps_den))))
        generated_dir = workspace.root / "generated"
        entrypoint = generated_dir / "entrypoint.tsx"
        entrypoint.write_text(
            """import React from 'react';
import {Composition, registerRoot} from 'remotion';
import {GeneratedScene} from './src/Scene';
const Root: React.FC = () => <Composition id=\"GeneratedScene\" component={GeneratedScene}
  durationInFrames={%(frames)d} fps={%(fps).10f} width={%(width)d} height={%(height)d}
  defaultProps={{width:%(width)d,height:%(height)d,fps:%(fps).10f,durationInFrames:%(frames)d}} />;
registerRoot(Root);
""" % {"frames": duration_frames, "fps": float(fps_num) / fps_den, "width": width, "height": height},
            encoding="utf-8",
        )
        source_hash = _sha256(source)
        overlay_info = self.sandbox.render(workspace, width, height, fps_num, fps_den, duration_ms)
        overlay = Path(overlay_info["overlay_path"])
        probe = _ffprobe(overlay)
        stream = next((item for item in probe.get("streams", []) if item.get("codec_type") == "video"), {})
        fps = Fraction(str(stream.get("avg_frame_rate") or "0/1"))
        duration = float((probe.get("format") or {}).get("duration") or 0)
        if int(stream.get("width") or 0) != int(width) or int(stream.get("height") or 0) != int(height):
            raise SceneRenderError("Generated overlay dimensions do not match source snapshot")
        if abs(float(fps) - float(Fraction(fps_num, fps_den))) > 0.02:
            raise SceneRenderError("Generated overlay FPS does not match source snapshot")
        if "a" not in str(stream.get("pix_fmt") or ""):
            raise SceneRenderError("Generated overlay lost its alpha channel")
        if abs(duration - duration_frames / (float(fps_num) / fps_den)) > 1.0 / (fps_num / fps_den) + 0.01:
            raise SceneRenderError("Generated overlay duration differs from the approved scene interval")

        frames_dir = workspace.qa_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        still_paths = self._sample_frames(overlay, frames_dir, duration)
        overlay_contact_sheet = workspace.qa_dir / "overlay-contact-sheet.jpg"
        self._contact_sheet(still_paths, overlay_contact_sheet)
        preview = workspace.output_dir / "preview-on-source.mp4"
        self._composite_preview(source, overlay, preview, start_ms, duration_ms)
        composite_frames_dir = workspace.qa_dir / "composite-frames"
        composite_frames_dir.mkdir(parents=True, exist_ok=True)
        composite_stills = self._sample_frames(preview, composite_frames_dir, duration)
        contact_sheet = workspace.qa_dir / "contact-sheet.jpg"
        self._contact_sheet(composite_stills, contact_sheet)
        qa = self._machine_qa(overlay, preview, still_paths, probe, source_hash, duration_ms)
        report_path = workspace.qa_dir / "qa-report.json"
        report_path.write_text(json.dumps(qa, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manifest = {
            "schema_version": "motion-scene.v1",
            "scene_id": workspace.scene_id,
            "revision": workspace.revision,
            "source_sha256": source_hash,
            "start_ms": int(start_ms), "end_ms": int(end_ms),
            "entrypoint": "generated/src/Scene.tsx",
            "generated_scene_sha256": _sha256(workspace.generated_src / "Scene.tsx"),
            "overlay_path": str(overlay.resolve()),
            "overlay_sha256": _sha256(overlay),
            "preview_path": str(preview.resolve()),
            "contact_sheet": str(contact_sheet.resolve()),
            "overlay_contact_sheet": str(overlay_contact_sheet.resolve()),
            "composite_stills": [str(path.resolve()) for path in composite_stills],
            "qa_report": str(report_path.resolve()),
            "status": "ready_for_review" if qa["status"] == "pass" else "needs_manual_review",
            "review_status": "proposed",
            "renderer_image": self.sandbox.image,
            "sandbox": {"network": "none", "memory": self.sandbox.memory, "cpus": self.sandbox.cpus},
        }
        manifest_path = workspace.root / "scene-manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {**manifest, "qa": qa, "duration_frames": duration_frames}

    @staticmethod
    def _sample_frames(video: Path, output_dir: Path, duration: float) -> List[Path]:
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        metadata = _ffprobe(video)
        stream = next((item for item in metadata.get("streams", []) if item.get("codec_type") == "video"), {})
        try:
            from fractions import Fraction
            fps = float(Fraction(str(stream.get("avg_frame_rate") or "30/1")))
        except (ValueError, ZeroDivisionError):
            fps = 30.0
        total_frames = int(stream.get("nb_frames") or 0) or max(1, int(round(duration * fps)))
        paths = []
        for index, fraction in enumerate((0.03, 0.25, 0.5, 0.75, 0.95)):
            frame_path = output_dir / ("frame_%02d.png" % (index + 1))
            frame_number = min(total_frames - 1, max(0, int(round((total_frames - 1) * fraction))))
            select_filter = "select=eq(n\\,%d)" % frame_number
            result = subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
                 "-vf", select_filter, "-fps_mode", "vfr", "-frames:v", "1", str(frame_path)],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode or not frame_path.is_file():
                raise SceneRenderError("Failed to sample generated scene frame %d: " % frame_number + (result.stderr or "")[-500:])
            paths.append(frame_path)
        return paths

    @staticmethod
    def _contact_sheet(frames: List[Path], target: Path) -> None:
        images = [cv2.imread(str(path), cv2.IMREAD_UNCHANGED) for path in frames]
        images = [image for image in images if image is not None]
        if not images:
            raise SceneRenderError("Visual QA produced no still frames")
        height, width = images[0].shape[:2]
        thumb_w = 300
        thumb_h = int(height * thumb_w / width)
        thumbs = [cv2.resize(image, (thumb_w, thumb_h), interpolation=cv2.INTER_AREA) for image in images]
        if thumbs[0].ndim == 3 and thumbs[0].shape[2] == 4:
            background = np.full((thumb_h, thumb_w, 3), 32, dtype=np.uint8)
            opaque = []
            for image in thumbs:
                alpha = image[:, :, 3:4].astype(np.float32) / 255.0
                rgb = image[:, :, :3].astype(np.float32)
                composed = rgb * alpha + background.astype(np.float32) * (1 - alpha)
                opaque.append(composed.astype(np.uint8))
            thumbs = opaque
        sheet = np.concatenate(thumbs, axis=1)
        if not cv2.imwrite(str(target), sheet):
            raise SceneRenderError("Failed to write visual QA contact sheet")

    @staticmethod
    def _composite_preview(source: Path, overlay: Path, target: Path, start_ms: int, duration_ms: int) -> None:
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        duration = duration_ms / 1000.0
        start = start_ms / 1000.0
        source_probe = probe_video(source)
        audio_codec = str(source_probe.get("audio_codec") or "").lower()
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", "%.6f" % start,
            "-i", str(source), "-i", str(overlay), "-t", "%.6f" % duration,
            "-filter_complex", "[0:v:0]setpts=PTS-STARTPTS[base];[base][1:v:0]overlay=shortest=1:eof_action=pass:format=auto,format=yuv420p[v]",
            "-map", "[v]", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "fast", "-crf", "17",
            "-pix_fmt", "yuv420p",
        ]
        if audio_codec in {"aac", "alac", "ac3", "eac3"}:
            command += ["-c:a", "copy"]
        else:
            command += ["-c:a", "aac", "-b:a", "192k"]
        command += ["-movflags", "+faststart", str(target)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        if result.returncode or not target.is_file():
            raise SceneRenderError("Scene preview composite failed: " + (result.stderr or "")[-1000:])

    @staticmethod
    def _machine_qa(overlay: Path, preview: Path, frames: List[Path], probe: dict,
                    source_hash: str, duration_ms: int) -> dict:
        stream = next((item for item in probe.get("streams", []) if item.get("codec_type") == "video"), {})
        images = [cv2.imread(str(path), cv2.IMREAD_UNCHANGED) for path in frames]
        alpha_coverages = []
        frame_diffs = []
        for image in images:
            if image is None or image.ndim != 3 or image.shape[2] < 4:
                alpha_coverages.append(0.0)
            else:
                alpha_coverages.append(float(np.count_nonzero(image[:, :, 3] > 4) / (image.shape[0] * image.shape[1])))
        for before, after in zip(images, images[1:]):
            if before is not None and after is not None:
                a = cv2.resize(before[:, :, :3], (64, 64), interpolation=cv2.INTER_AREA)
                b = cv2.resize(after[:, :, :3], (64, 64), interpolation=cv2.INTER_AREA)
                frame_diffs.append(float(cv2.absdiff(a, b).mean()))
        checks = {
            "overlay_has_alpha": "a" in str(stream.get("pix_fmt") or ""),
            "five_stills_sampled": len(images) == 5 and all(image is not None for image in images),
            "visible_layer_in_samples": any(value > 0.001 for value in alpha_coverages),
            "content_changes_over_time": bool(frame_diffs and max(frame_diffs) > 0.5),
            "source_preview_exists": preview.is_file() and preview.stat().st_size > 10_000,
            "duration_under_fixed_timeline": duration_ms <= 12_000,
            "source_hash_bound": len(source_hash) == 64,
        }
        return {
            "schema_version": "motion-qa.v1",
            "status": "pass" if all(checks.values()) else "revise",
            "blocking_issues": [] if all(checks.values()) else [{"code": key.upper(), "summary": "Machine visual preflight failed: " + key} for key, passed in checks.items() if not passed],
            "checks": checks,
            "alpha_coverage": alpha_coverages,
            "sampled_motion_mean_abs_diff": frame_diffs,
            "overlay": {"codec": stream.get("codec_name"), "pixel_format": stream.get("pix_fmt"), "width": stream.get("width"), "height": stream.get("height"), "fps": stream.get("avg_frame_rate")},
            "preview_bytes": preview.stat().st_size if preview.is_file() else 0,
            "source_sha256": source_hash,
        }


def _ffprobe(path: Path) -> dict:
    executable = shutil.which("ffprobe") or "ffprobe"
    result = subprocess.run(
        [executable, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise SceneRenderError("ffprobe failed for generated scene: " + (result.stderr or "")[-500:])
    return json.loads(result.stdout)


def _sha256(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
