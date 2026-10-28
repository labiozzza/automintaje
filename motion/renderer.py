"""Transparent Remotion layers and one-pass FFmpeg composition."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from montage.color_pipeline import encoder_color_args
from montage.engine import MontageCancelled, run_ffmpeg_progress
from .quality import validate_composite
from .source_snapshot import probe_video


class MotionRenderError(RuntimeError):
    pass


class MotionRenderer:
    def __init__(self, project_root: Path, montage_work: Path):
        self.project_root = Path(project_root).resolve()
        self.montage_work = Path(montage_work).resolve()
        self.remotion_root = self.project_root / "motion" / "assets" / "remotion"
        self._lock = threading.Lock()
        self._semaphore = threading.Semaphore(1)
        self._cancel_events: Dict[str, threading.Event] = {}
        self._active_processes: Dict[str, subprocess.Popen] = {}

    def capabilities(self) -> dict:
        node = shutil.which("node")
        cli = self.remotion_root / "node_modules" / ".bin" / "remotion"
        package = self.remotion_root / "package.json"
        return {
            "available": bool(node and cli.is_file() and package.is_file()),
            "node": node or "",
            "remotion_cli": str(cli) if cli.is_file() else "",
            "transparent_codec": "prores 4444 / yuva444p10le",
            "max_parallel_renders": 1,
        }

    @staticmethod
    def _check_cancel(cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise MotionRenderError("Motion рендер отменён пользователем")

    def render(self, project_id: str, revision: int, source_path: Path,
               source_metadata: dict, plan: dict, preview: bool,
               preview_duration_ms: Optional[int], cancel_event: threading.Event,
               progress_cb: Optional[Callable[[float, str], None]] = None) -> dict:
        if not self.capabilities()["available"]:
            raise MotionRenderError("Remotion renderer не установлен (npm install в motion/assets/remotion)")
        source_path = Path(source_path)
        if source_path.is_symlink() or not source_path.is_file():
            raise MotionRenderError("Закреплённый исходный мастер недоступен")
        source_path = source_path.resolve()
        metadata = probe_video(source_path)
        if metadata.get("color", {}).get("hdr"):
            raise MotionRenderError("Motion-композиция пока принимает SDR-мастер; HDR не будет переобозначен как SDR")

        duration_ms = int(source_metadata.get("duration_ms") or metadata.get("duration_ms") or 0)
        if duration_ms <= 0:
            raise MotionRenderError("Не определена длительность source snapshot")
        render_duration_ms = min(duration_ms, int(preview_duration_ms or 0)) if preview else duration_ms
        fps_num = int(source_metadata.get("fps_num") or metadata.get("fps_num") or 0)
        fps_den = int(source_metadata.get("fps_den") or metadata.get("fps_den") or 1)
        width = int(source_metadata.get("width") or metadata.get("width") or 0)
        height = int(source_metadata.get("height") or metadata.get("height") or 0)
        if fps_num <= 0 or fps_den <= 0 or width <= 0 or height <= 0:
            raise MotionRenderError("Метаданные source snapshot неполные")
        self.montage_work.mkdir(parents=True, exist_ok=True)
        estimated_overlay_bytes = max(128 * 1024 * 1024, int(render_duration_ms / 1000.0 * 8 * 1024 * 1024))
        required_free_bytes = int(source_path.stat().st_size) + estimated_overlay_bytes + 512 * 1024 * 1024
        if int(shutil.disk_usage(self.montage_work).free) < required_free_bytes:
            raise MotionRenderError("Недостаточно свободного места для Motion-рендера и временных слоёв")

        output_job_id = "mg_" + uuid.uuid4().hex
        output_dir = self.montage_work / output_job_id
        output_dir.mkdir(parents=True, exist_ok=False)
        render_dir = output_dir / "motion_render"
        render_dir.mkdir()
        overlay_path = render_dir / "overlay_prores.mov"
        master_path = output_dir / "master.mp4"
        final_path = output_dir / "final.mp4"
        preview_path = output_dir / "preview.mp4"
        preview_destination = None
        remotion_log = render_dir / "remotion.log"

        props = {
            "scenes": list(plan.get("scenes") or []),
            "durationMs": duration_ms,
            "fpsNum": fps_num,
            "fpsDen": fps_den,
            "outputWidth": width,
            "outputHeight": height,
            "profile": str(plan.get("profile") or "minimal"),
            "renderMode": "preview" if preview else "full",
            "previewDurationMs": render_duration_ms,
        }
        props_path = render_dir / "props.json"
        props_path.write_text(json.dumps(props, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        operation_key = "%s:%s:%s" % (project_id, revision, output_job_id)
        try:
            with self._semaphore:
                self._check_cancel(cancel_event)
                if progress_cb:
                    progress_cb(4, "Проверка source snapshot и параметров композиции")
                if any(scene.get("review_status") == "accepted" and scene.get("effect_type") != "none"
                       for scene in props["scenes"]):
                    command = [
                        str(self.remotion_root / "node_modules" / ".bin" / "remotion"),
                        "render", "src/index.ts", "MotionOverlay", str(overlay_path),
                        "--props", json.dumps(props, ensure_ascii=False, separators=(",", ":")),
                        "--codec", "prores", "--prores-profile", "4444",
                        "--pixel-format", "yuva444p10le", "--image-format", "png",
                        "--concurrency", "2", "--log", "info", "--overwrite",
                        "--disable-ask-ai",
                    ]
                    self._run_remotion(operation_key, command, remotion_log, cancel_event, progress_cb)
                    self._validate_overlay(overlay_path, width, height, render_duration_ms, fps_num, fps_den)
                else:
                    # A user-approved plan may deliberately contain only `none` scenes.
                    # Reuse the immutable source bytes instead of encoding an empty layer.
                    shutil.copy2(source_path, master_path)
                self._check_cancel(cancel_event)
                if overlay_path.is_file():
                    self._compose(
                        operation_key, source_path, overlay_path, master_path, metadata,
                        render_duration_ms, preview, cancel_event, progress_cb,
                    )
                final_source = master_path
                if not final_source.is_file() or final_source.stat().st_size <= 0:
                    raise MotionRenderError("Motion композиция не создала пригодный результат")
                if preview:
                    # Preview must be browser playable and is intentionally short.
                    if final_source != preview_path:
                        shutil.copy2(final_source, preview_path)
                    quality = validate_composite(source_path, preview_path, render_duration_ms)
                    project_dir = self.work_root / ".motion" / "projects" / project_id
                    preview_dir = project_dir / "revisions" / ("%04d" % revision) / "previews"
                    preview_dir.mkdir(parents=True, exist_ok=True)
                    preview_destination = preview_dir / ("preview_%s.mp4" % uuid.uuid4().hex[:12])
                    staging_preview = preview_dir / ("." + preview_destination.name + ".tmp")
                    shutil.copy2(preview_path, staging_preview)
                    os.replace(staging_preview, preview_destination)
                    shutil.rmtree(output_dir, ignore_errors=True)
                    return {
                        "preview_video": str(preview_destination.resolve()),
                        "duration_seconds": self._duration(preview_destination),
                        "width": width, "height": height,
                        "quality": quality,
                    }

                if not final_path.exists():
                    os.link(str(master_path), str(final_path))
                if not preview_path.exists():
                    os.link(str(master_path), str(preview_path))
                quality = validate_composite(source_path, final_path, duration_ms)
                if progress_cb:
                    progress_cb(96, "Проверка итогового видео и аудиодорожки")
                result = {
                    "job_id": output_job_id,
                    "output_dir": str(output_dir.resolve()),
                    "master_video": str(master_path.resolve()),
                    "final_video": str(final_path.resolve()),
                    "preview_video": str(preview_path.resolve()),
                    "duration_seconds": self._duration(final_path),
                    "width": width,
                    "height": height,
                    "fps_num": fps_num,
                    "fps_den": fps_den,
                    "source_sha256": str(source_metadata.get("sha256") or ""),
                    "render_mode": "remotion-prores4444+ffmpeg-composite" if overlay_path.is_file() else "source-copy-no-scenes",
                    "quality": quality,
                }
                shutil.rmtree(output_dir / "motion_render", ignore_errors=True)
                if progress_cb:
                    progress_cb(98, "Motion-файл подготовлен")
                return result
        except Exception as exc:
            if remotion_log.is_file():
                diagnostic_dir = self.work_root / ".motion" / "projects" / project_id / "revisions" / ("%04d" % revision) / "logs"
                diagnostic_dir.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2(remotion_log, diagnostic_dir / (output_job_id + "-remotion.log"))
                except OSError:
                    pass
            shutil.rmtree(output_dir, ignore_errors=True)
            if preview_destination is not None:
                preview_destination.unlink(missing_ok=True)
            raise

    def cancel(self, operation_id: str) -> None:
        with self._lock:
            event = self._cancel_events.get(operation_id)
            process = self._active_processes.get(operation_id)
        if event:
            event.set()
        if process is not None and process.poll() is None:
            self._terminate_process(process)

    def shutdown(self) -> None:
        with self._lock:
            active = list(self._active_processes.items())
            events = list(self._cancel_events.values())
        for event in events:
            event.set()
        for _operation, process in active:
            self._terminate_process(process)

    def _run_remotion(self, operation_key: str, command: list, log_path: Path,
                      cancel_event: threading.Event,
                      progress_cb: Optional[Callable[[float, str], None]]) -> None:
        proc = subprocess.Popen(
            command, cwd=str(self.remotion_root), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            bufsize=1, start_new_session=True,
        )
        with self._lock:
            self._active_processes[operation_key] = proc
            self._cancel_events[operation_key] = cancel_event
        lines = queue.Queue()

        def drain():
            try:
                for line in iter(proc.stdout.readline, ""):
                    lines.put(line)
            finally:
                lines.put(None)

        reader = threading.Thread(target=drain, name="motion-remotion-log", daemon=True)
        reader.start()
        last_lines = []
        deadline = time.monotonic() + 1800
        progress_pattern = re.compile(r"(?:rendered|frame)\s+(\d+)\s*(?:/|of)\s*(\d+)", re.I)
        percent_pattern = re.compile(r"\b(\d{1,3}(?:\.\d+)?)%")
        try:
            with log_path.open("w", encoding="utf-8") as log:
                while proc.poll() is None or not lines.empty():
                    if cancel_event.is_set():
                        self._terminate_process(proc)
                        raise MotionRenderError("Motion рендер отменён пользователем")
                    if time.monotonic() >= deadline:
                        self._terminate_process(proc)
                        raise MotionRenderError("Remotion рендер превысил лимит 30 минут")
                    try:
                        line = lines.get(timeout=0.25)
                    except queue.Empty:
                        continue
                    if line is None:
                        continue
                    clean = line.rstrip("\r\n")
                    if clean:
                        log.write(clean[-1000:] + "\n")
                        log.flush()
                        last_lines.append(clean[-500:])
                        last_lines = last_lines[-30:]
                    match = progress_pattern.search(clean)
                    percent = None
                    if match and int(match.group(2)) > 0:
                        percent = int(match.group(1)) / int(match.group(2)) * 100
                    else:
                        percent_match = percent_pattern.search(clean)
                        if percent_match:
                            try:
                                percent = float(percent_match.group(1))
                            except ValueError:
                                percent = None
                    if percent is not None and progress_cb:
                        progress_cb(min(55, 10 + percent * 0.45), "Рендер прозрачных графических слоёв: %.0f%%" % percent)
            reader.join(timeout=2)
            if proc.returncode != 0:
                raise MotionRenderError("Remotion завершился с ошибкой: " + " | ".join(last_lines[-5:])[-1500:])
            if cancel_event.is_set():
                raise MotionRenderError("Motion рендер отменён пользователем")
        finally:
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except Exception:
                    pass
            reader.join(timeout=2)
            with self._lock:
                self._active_processes.pop(operation_key, None)
                self._cancel_events.pop(operation_key, None)

    @staticmethod
    def _terminate_process(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            proc.wait(timeout=4)
        except Exception:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _compose(self, job_id: str, source: Path, overlay: Path, output: Path,
                 source_probe: dict, duration_ms: int, preview: bool,
                 cancel_event: threading.Event,
                 progress_cb: Optional[Callable[[float, str], None]]) -> None:
        if cancel_event.is_set():
            raise MotionRenderError("Motion рендер отменён пользователем")
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        color = source_probe.get("color") or {}
        if color.get("hdr"):
            raise MotionRenderError("Нельзя смешать SDR-графику с HDR источником без tone mapping")
        duration = max(0.1, float(duration_ms) / 1000.0)
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source), "-i", str(overlay),
            "-filter_complex", "[0:v:0][1:v:0]overlay=shortest=1:eof_action=pass:format=auto,format=yuv420p[v]",
            "-map", "[v]", "-map", "0:a:0?", "-t", "%.6f" % duration,
            "-c:v", "libx264", "-preset", "medium", "-crf", "15", "-pix_fmt", "yuv420p",
            "-fps_mode", "passthrough",
        ] + encoder_color_args(color, "h264")
        audio_codec = str(source_probe.get("audio_codec") or "").lower()
        if source_probe.get("audio_present") and audio_codec not in {"aac", "alac", "ac3", "eac3"}:
            command += ["-c:a", "aac", "-b:a", "192k"]
        else:
            command += ["-c:a", "copy"]
        command += ["-movflags", "+faststart", str(output)]
        if progress_cb:
            progress_cb(58, "Композиция с исходным мастером без изменения тайминга")
        try:
            return_code = run_ffmpeg_progress(
                command, duration=duration, cwd=output.parent,
                log_path=output.parent / ("%s-compose.log" % job_id.replace(":", "-")),
                on_progress=lambda pct: progress_cb(58 + min(32, float(pct) * 0.32), "Композиция Motion-слоёв") if progress_cb else None,
                timeout=1800, job_id="motion_" + uuid.uuid4().hex[:12],
                cancel_cb=cancel_event.is_set,
            )
        except MontageCancelled as exc:
            raise MotionRenderError("Motion рендер отменён пользователем") from exc
        if return_code != 0:
            raise MotionRenderError("FFmpeg не завершил композицию Motion-слоёв")
        if not output.is_file() or output.stat().st_size <= 0:
            raise MotionRenderError("FFmpeg не создал композиционный MP4")

    @staticmethod
    def _validate_overlay(path: Path, width: int, height: int,
                          duration_ms: int, fps_num: int, fps_den: int) -> None:
        ffprobe = shutil.which("ffprobe") or "ffprobe"
        result = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=codec_name,pix_fmt,width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=90, check=False,
        )
        if result.returncode != 0:
            raise MotionRenderError("Не удалось проверить прозрачный Remotion-файл")
        data = json.loads(result.stdout or "{}")
        stream = next(iter(data.get("streams") or []), {})
        if int(stream.get("width") or 0) != int(width) or int(stream.get("height") or 0) != int(height):
            raise MotionRenderError("Размер Remotion-слоя не совпадает с исходным мастером")
        if "yuva" not in str(stream.get("pix_fmt") or ""):
            raise MotionRenderError("Remotion-слой потерял alpha channel")
        from fractions import Fraction
        try:
            output_fps = Fraction(str(stream.get("r_frame_rate") or "0/1"))
            target_fps = Fraction(int(fps_num), int(fps_den))
        except (ValueError, ZeroDivisionError):
            raise MotionRenderError("FPS прозрачного Remotion-слоя не распознан")
        if output_fps <= 0 or abs(float(output_fps - target_fps)) > 0.02:
            raise MotionRenderError("FPS прозрачного слоя не совпадает с исходным мастером")
        try:
            duration = float((data.get("format") or {}).get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        expected = float(duration_ms) / 1000.0
        tolerance = max(0.12, float(fps_den) / max(1, fps_num) + 0.04)
        if abs(duration - expected) > tolerance:
            raise MotionRenderError("Длительность прозрачного слоя отличается от согласованного таймлайна")

    @staticmethod
    def _duration(path: Path) -> float:
        return float(probe_video(path).get("duration_seconds") or 0)
