from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from .audio_censor import build_volume_filter, find_mute_intervals, needs_word_alignment
from .progress_tracker import PENDING, ProgressTracker, montage_plan
from .secrets_env import load_secrets_env, get_software_defaults, media_repo_dir

MONAGE_DIR = Path(__file__).resolve().parent
INSTAPOSTER_DIR = MONAGE_DIR / "instaposter"
MODELS_DIR = MONAGE_DIR / "models"
FONTS_DIR = MONAGE_DIR / "fonts"
ANIMATIONS_DIR = MONAGE_DIR / "animations"
FACE_MODEL = MODELS_DIR / "face_landmarker.task"
DEFAULT_FONT = FONTS_DIR / "Comfortaa.ttf"
SYSTEM_FONT = Path("/System/Library/Fonts/Helvetica.ttc")

PYTHON = sys.executable

_internet_cache: dict[str, Any] = {"t": 0.0, "ok": False}


class MontageCancelled(Exception):
    """Raised inside worker threads when the user cancels the job.

    Unlike a real failure this must never mark the job as ``error``: the app
    layer checks the per-job cancel flag and reports ``cancelled`` instead.
    """


# Registry of live subprocesses per engine job id.  The render pipeline runs
# in a worker thread blocked in ``Popen.wait()``; cancelling the asyncio task
# alone cannot stop it, so ``cancel_job_procs`` kills the processes and the
# blocked waits return immediately.
_JOB_PROCS: dict[str, set[subprocess.Popen]] = {}
_JOB_PROCS_LOCK = threading.Lock()


def register_job_proc(job_id: str | None, proc: subprocess.Popen) -> None:
    if not job_id:
        return
    with _JOB_PROCS_LOCK:
        _JOB_PROCS.setdefault(job_id, set()).add(proc)


def unregister_job_proc(job_id: str | None, proc: subprocess.Popen) -> None:
    if not job_id:
        return
    with _JOB_PROCS_LOCK:
        procs = _JOB_PROCS.get(job_id)
        if procs is not None:
            procs.discard(proc)
            if not procs:
                _JOB_PROCS.pop(job_id, None)


def cancel_job_procs(job_id: str) -> int:
    """Kill every tracked subprocess of *job_id* (plus ``<job_id>_mirror``).

    Returns the number of processes signalled.
    """
    with _JOB_PROCS_LOCK:
        targets = [key for key in _JOB_PROCS if key in (job_id, f"{job_id}_mirror")]
        procs = [proc for key in targets for proc in _JOB_PROCS.pop(key, set())]
    killed = 0
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.kill()
                killed += 1
        except Exception:
            pass
    for proc in procs:
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
    return killed


def log(type_: str, message: str) -> None:
    print(f"[montage] {type_}: {message}", file=sys.stderr)


def _url_open(url: str, timeout: float = 6.0) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 500
    except Exception:
        return False


def internet_available(force: bool = False) -> bool:
    global _internet_cache
    now = time.time()
    if not force and now - _internet_cache["t"] < 30:
        return _internet_cache["ok"]
    ok = _url_open("https://opencode.ai/") or _url_open("https://www.google.com/")
    _internet_cache = {"t": now, "ok": ok}
    return ok


def opencode_bin() -> str:
    return os.environ.get("OPENCODE_BIN", "") or shutil.which("opencode") or "opencode"


def face_model_ready() -> bool:
    return FACE_MODEL.exists()


def opencode_ready() -> bool:
    return shutil.which("opencode") is not None or Path(opencode_bin()).exists()


def media_repo_ready() -> bool:
    return (media_repo_dir() / ".git").is_dir()


def animation_ids() -> list[dict[str, Any]]:
    items = []
    if ANIMATIONS_DIR.is_dir():
        for manifest in sorted(ANIMATIONS_DIR.glob("*/manifest.json")):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except Exception:
                continue
            items.append({"id": manifest.parent.name, "name": data.get("name", manifest.parent.name)})
    return items


def font_options() -> list[dict[str, str]]:
    if not DEFAULT_FONT.exists():
        return []
    items = [{"id": DEFAULT_FONT.name, "name": "Comfortaa (встроен)", "path": str(DEFAULT_FONT)}]
    if SYSTEM_FONT.exists():
        items.append({"id": "system", "name": "Системный (Helvetica)", "path": str(SYSTEM_FONT)})
    return items


def montage_status() -> dict[str, Any]:
    load_secrets_env()
    sw = get_software_defaults()
    online = internet_available()
    return {
        "online": online,
        "opencode": opencode_ready(),
        "bigpickle_enabled": online and opencode_ready(),
        "face_model": face_model_ready(),
        "font": DEFAULT_FONT.exists(),
        "fonts": font_options(),
        "media_repo": media_repo_ready(),
        "animations": animation_ids(),
        "publishers": {
            "instagram_reels": sw["instagram_ready"],
            "threads": sw["threads_ready"],
            "youtube": sw["youtube_ready"],
            "telegram": sw["telegram_ready"],
        },
    }


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path,
    log_path: Path,
    line_cb: Callable[[str], None] | None = None,
    timeout: int = 3600,
    env: dict[str, str] | None = None,
    job_id: str | None = None,
) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        register_job_proc(job_id, proc)
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                if line_cb:
                    try:
                        line_cb(line)
                    except Exception:
                        pass
            proc.stdout.close()
            return proc.wait()
        finally:
            unregister_job_proc(job_id, proc)


def usable_video(path: Path | str | None) -> bool:
    """True when *path* points to a non-empty file with a real video stream."""
    if not path:
        return False
    candidate = Path(path)
    try:
        if not candidate.is_file() or candidate.stat().st_size <= 0:
            return False
    except OSError:
        return False
    try:
        probe = ffprobe(candidate)
    except Exception:
        return False
    for stream in probe.get("streams") or []:
        if stream.get("codec_type") == "video" and int(stream.get("width") or 0) > 0:
            return True
    return False


def run_ffmpeg_progress(
    cmd: list[str],
    *,
    duration: float,
    on_progress: Callable[[float], None] | None = None,
    cwd: Path | None = None,
    log_path: Path | None = None,
    timeout: int = 1800,
    job_id: str | None = None,
    cancel_cb: Callable[[], bool] | None = None,
) -> int:
    """Run an ffmpeg command and report real progress from ``-progress pipe:1``.

    FFmpeg emits ``out_time_us``/``out_time_ms`` in **microseconds** (the ``ms``
    name is a long-standing quirk), so we divide by 1_000_000 — never by 1000 —
    and clamp against the probed ``duration``.  Reading happens on a worker
    thread so a hung encoder cannot block the UI forever.  When ``cancel_cb``
    reports cancellation the encoder is killed and ``MontageCancelled`` is
    raised.
    """
    ff = cmd[0] if cmd else (shutil.which("ffmpeg") or "ffmpeg")
    full = [ff, "-hide_banner", "-nostats", "-loglevel", "error",
            "-progress", "pipe:1"] + list(cmd[1:])
    log_file = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("a", encoding="utf-8", errors="replace")
    stderr_target = log_file if log_file is not None else subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            full, cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE, stderr=stderr_target,
            text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        if log_file is not None:
            log_file.close()
        raise RuntimeError("ffmpeg не найден")
    assert proc.stdout is not None
    register_job_proc(job_id, proc)
    seen_end = {"done": False}

    def _reader() -> None:
        for line in proc.stdout:
            line = line.strip()
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key in ("out_time_us", "out_time_ms", "out_time_us_hi"):
                try:
                    micros = int(value)
                except (TypeError, ValueError):
                    continue
                if micros < 0:
                    micros = 0
                seconds = micros / 1_000_000.0
                if on_progress is not None:
                    pct = 0.0 if duration <= 0 else min(99.5, seconds / duration * 100.0)
                    try:
                        on_progress(pct)
                    except Exception:
                        pass
            elif key == "progress" and value == "end":
                seen_end["done"] = True
                if on_progress is not None:
                    try:
                        on_progress(100.0)
                    except Exception:
                        pass
        try:
            proc.stdout.close()
        except Exception:
            pass

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    deadline = time.monotonic() + max(1, timeout)
    try:
        # Poll in short slices instead of one long wait so a user cancel is
        # honoured within a second even if nobody kills the process externally.
        code: int | None = None
        while code is None:
            try:
                code = proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                if cancel_cb is not None:
                    try:
                        cancelled = bool(cancel_cb())
                    except Exception:
                        cancelled = False
                    if cancelled:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                        try:
                            proc.wait(timeout=10)
                        except Exception:
                            pass
                        raise MontageCancelled("Отменено пользователем")
                if time.monotonic() >= deadline:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=10)
                    except Exception:
                        pass
                    raise RuntimeError("ffmpeg превысил лимит времени")
                continue
    except BaseException:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        raise
    finally:
        unregister_job_proc(job_id, proc)
    reader.join(timeout=5)
    if log_file is not None:
        log_file.close()
    return code


def build_words_srt(segments: list[dict[str, Any]]) -> str:
    def fmt(t: float) -> str:
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int(round((t - int(t)) * 1000))
        if ms == 1000:
            s += 1
            ms = 0
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines: list[str] = []
    for i, seg in enumerate(segments, 1):
        start = float(seg.get("start") or 0)
        end = float(seg.get("end") or start)
        if end <= start:
            continue
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        lines.extend([str(i), f"{fmt(start)} --> {fmt(end)}", text, ""])
    return "\n".join(lines).strip() + "\n"


def build_word_timing_srt(segments: list[dict[str, Any]]) -> str:
    timed_words = [
        {
            "start": word.get("start"),
            "end": word.get("end"),
            "text": word.get("word") or word.get("text"),
        }
        for segment in segments
        for word in (segment.get("word_timestamps") or segment.get("words") or [])
        if isinstance(word, dict)
    ]
    return build_words_srt(timed_words) if timed_words else ""


def format_timecode(t: float) -> str:
    t = max(0.0, float(t))
    m = int(t // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    if ms >= 1000:
        ms -= 1000
        s += 1
    return f"{m:02d}:{s:02d}.{ms:03d}"


def ffprobe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=width,height,r_frame_rate,codec_type", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()[-400:]}")
    return json.loads(result.stdout)


CAMERA_MODES = ("off", "hold", "live")

# Legacy UI values that predate render.camera; "scale" never existed in the
# renderer ("reflect"/"replicate"/"black"/"clamp" do).  Saved jobs store
# "scale", so normalize it to "clamp" (no artificial fill, eyes may drift).
_EDGE_MODE_ALIASES = {"scale": "clamp"}


def normalize_edge_mode(raw: Any) -> str:
    mode = str(raw or "clamp").lower()
    mode = _EDGE_MODE_ALIASES.get(mode, mode)
    if mode not in {"reflect", "replicate", "black", "clamp"}:
        return "clamp"
    return mode


def resolve_camera_options(options: dict[str, Any]) -> dict[str, Any]:
    """Resolve virtual-camera settings from montage params (testable).

    Backward compatibility: jobs saved before ``camera_mode`` existed only
    carry ``face_tracking`` — that means eye hold.  Returns the normalized
    camera block plus pipeline flags (``do_track``, ``do_zoom_stage``).
    """
    options = options or {}
    raw_mode = options.get("camera_mode")
    if raw_mode in (None, ""):
        mode = "hold" if options.get("face_tracking") else "off"
    else:
        mode = str(raw_mode).lower()
    if mode not in CAMERA_MODES:
        mode = "off"

    def _num(key: str, default: float, low: float, high: float) -> float:
        try:
            value = float(options.get(key, default))
        except (TypeError, ValueError):
            value = float(default)
        return min(high, max(low, value))

    camera = {
        "mode": mode,
        "eye_x": 0.50,
        "eye_y": 0.42,
        "follow_strength": _num("follow_strength", 0.30, 0.0, 1.0),
        "lag_seconds": _num("lag_seconds", 0.40, 0.0, 3.0),
        "min_zoom": _num("min_zoom", 1.12, 1.05, 1.25),
        "max_speed": _num("cam_max_speed", 0.35, 0.0, 2.0),
        "wobble": bool(options.get("camera_wobble", False)),
    }
    return {
        "camera": camera,
        "edge_mode": normalize_edge_mode(options.get("edge_mode")),
        "do_track": mode in {"hold", "live"},
        "do_zoom_stage": bool(options.get("autozoom", False)) or mode != "off",
    }


def write_static_tracking(
    tracking_path: Path,
    *,
    width: int,
    height: int,
    fps: float,
    duration: float,
) -> None:
    """Write a faceless tracking file so the zoom stage runs without MediaPipe.

    All samples report ``face_found=False``; with camera mode ``off`` the
    renderer ignores anchors and zooms around the static center.
    """
    fps = max(1.0, float(fps or 30.0))
    duration = max(0.5, float(duration or 0.0))
    step = max(1, int(round(fps / 10.0)))
    n = max(1, int(round(duration * fps / step)))
    samples = []
    for i in range(n):
        t = i * step / fps
        samples.append({
            "frame": i * step,
            "time": round(t, 6),
            "face_found": False,
            "confidence": 0.0,
            "left_eye": None,
            "right_eye": None,
            "eye_center": {"x": 0.5, "y": 0.42},
            "target": {"x": 0.5, "y": 0.42, "zoom": 1.0},
        })
    tracking_path.parent.mkdir(parents=True, exist_ok=True)
    tracking_path.write_text(json.dumps({
        "source": {"path": "", "width": width, "height": height,
                   "fps": fps, "duration": duration, "frames": int(duration * fps)},
        "settings": {"eye_y_offset": 0.06},
        "camera_targets": samples,
        "_static_anchors": True,
    }, ensure_ascii=False), encoding="utf-8")


def inject_camera_config(
    zoom_config_path: Path,
    camera_cfg: dict[str, Any],
    edge_mode: str,
    autozoom_on: bool,
) -> None:
    """Inject per-job camera settings into a generated zoom config.

    The generator stays neutral; this makes config and CLI agree.  When the
    semantic autozoom is off, its timeline is flattened to a single default
    event so only the constant camera floor (``max(default, min_zoom)``)
    applies — otherwise the toggle would be a no-op whenever a camera mode
    runs the zoom stage.  An explicit user ``zoom_timeline_override`` is kept:
    it is applied by the caller before this runs and counts as intent.
    """
    try:
        cfg = json.loads(zoom_config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Не удалось прочитать настройки камеры: {exc}")
    render = cfg.get("render")
    if not isinstance(render, dict):
        render = {}
        cfg["render"] = render
    if not autozoom_on:
        zoom_cfg = render.get("zoom")
        if isinstance(zoom_cfg, dict):
            default_zoom = zoom_cfg.get("default_zoom", 1.0)
            try:
                default_zoom = max(1.0, float(default_zoom))
            except (TypeError, ValueError):
                default_zoom = 1.0
            zoom_cfg["timeline"] = [{"time": 0.0, "zoom": default_zoom}]
    render["camera"] = dict(camera_cfg)
    render["edge_mode"] = edge_mode
    try:
        zoom_config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        raise RuntimeError(f"Не удалось записать настройки камеры: {exc}")


def ensure_tracking_has_anchors(tracking_path: Path) -> bool:
    """apply_face_zoom_v5 requires >=1 face-anchor. If the video had no detected faces,
    synthesize center-anchored targets so zoom/render still works (camera stays centered)."""
    try:
        data = json.loads(tracking_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    targets = data.get("camera_targets") or []
    if any(item.get("face_found") for item in targets):
        return True
    source = data.get("source", {}) or {}
    fps = float(source.get("fps") or 30)
    duration = float(source.get("duration") or 0) or (len(targets) * 3.0 / 10.0)
    step = max(1, int(round(fps / 10.0)))
    n = max(1, int(round(duration * fps / step)))
    synthetic = []
    for i in range(n):
        t = i * step / fps
        synthetic.append({
            "frame": i * step,
            "time": round(t, 6),
            "face_found": True,
            "confidence": 0.0,
            "left_eye": {"x": 0.5, "y": 0.36},
            "right_eye": {"x": 0.55, "y": 0.36},
            "eye_center": {"x": 0.525, "y": 0.36},
            "target": {"x": 0.525, "y": 0.42, "zoom": 1.0},
        })
    data["camera_targets"] = synthetic
    data["_synthetic_anchors"] = True
    tracking_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return True


def _manifest_fps(animation_id: str) -> float:
    try:
        manifest = json.loads(
            (ANIMATIONS_DIR / animation_id / "manifest.json").read_text(encoding="utf-8"))
        return max(0.1, float(manifest.get("fps") or 10.0))
    except (OSError, ValueError):
        return 10.0


def timeline_animation_layers(entries: list) -> tuple[list, list]:
    """Build render layers for timeline animations.

    Base playback rate = fps of the first animation, so all of them run at
    the same speed by default; per-item ``speed`` (%) scales from it.  A
    single animation keeps its own fps at 100% (backward compatible).
    Returns (layers, used_for_meta).
    """
    def _layer(entry: dict[str, Any], index: int, base_fps: float) -> dict[str, Any] | None:
        animation_id = str(entry.get("id") or "")
        if not animation_id or Path(animation_id).name != animation_id:
            return None
        if not (ANIMATIONS_DIR / animation_id / "manifest.json").exists():
            raise RuntimeError(f"Анимация {animation_id} не найдена в {ANIMATIONS_DIR}")
        try:
            speed = float(entry.get("speed", 100.0))
        except (TypeError, ValueError):
            speed = 100.0
        speed = min(400.0, max(10.0, speed))
        return {
            "uid": f"timeline-animation-{index}",
            "animation_id": animation_id,
            "start": max(0.0, float(entry.get("start") or 0.0)),
            "end": 86400.0,
            "size": max(20, min(1200, int(entry.get("item_size") or 72))),
            "z_index": -1 - index,
            "motion": "progress",
            "offset_x": int(entry.get("offset_x") or 0),
            "fps": round(max(0.1, base_fps * speed / 100.0), 3),
            "speed": speed,
        }

    valid = [entry for entry in (entries or [])
             if isinstance(entry, dict) and str(entry.get("id") or "")]
    base_fps = 10.0
    if valid:
        base_fps = _manifest_fps(str(valid[0]["id"]))
    layers, used, seen = [], [], set()
    for index, entry in enumerate(valid[:20]):
        layer = _layer(entry, index, base_fps)
        if layer is None or layer["animation_id"] in seen:
            continue
        seen.add(layer["animation_id"])
        layers.append(layer)
        used.append({
            "id": layer["animation_id"], "start": layer["start"],
            "offset_x": layer["offset_x"], "item_size": layer["size"],
            "speed": layer["speed"], "fps": layer["fps"],
        })
    return layers, used


def render_montage(
    *,
    job_id: str,
    workdir: Path,
    source_video: Path,
    srt_text: str,
    options: dict[str, Any],
    progress_cb: Callable[[float, str, str], None],
    log_cb: Callable[[str], None],
    report_cb: Callable[[dict[str, Any]], None] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    load_secrets_env()
    workdir.mkdir(parents=True, exist_ok=True)
    transcript = Path(workdir) / f"{job_id}_words.srt"
    transcript.write_text(srt_text, encoding="utf-8")

    # Diagnostic mode: collect every ffmpeg/worker command for later audit.
    # Activated by AUTOMONTAGE_DIAGNOSTIC=1 or AUTOMONTAGE_KEEP_INTERMEDIATES=1.
    _diag = os.environ.get("AUTOMONTAGE_DIAGNOSTIC", "").strip().lower() in ("1", "true", "yes") \
        or os.environ.get("AUTOMONTAGE_KEEP_INTERMEDIATES", "").strip().lower() in ("1", "true", "yes")
    ffmpeg_commands: list[dict[str, Any]] = []
    def _record_cmd(label: str, cmd: list) -> None:
        if _diag:
            ffmpeg_commands.append({"label": label, "cmd": [str(x) for x in cmd]})

    # Stage B.1: intermediate pix_fmt switch (default stays yuv420p until accepted).
    _inter_pix_fmt = str(os.environ.get("AUTOMONTAGE_INTERMEDIATE_PIX_FMT", "yuv420p").strip().lower())
    if _inter_pix_fmt not in ("yuv420p", "yuv444p"):
        print(f"[engine] unsupported AUTOMONTAGE_INTERMEDIATE_PIX_FMT={_inter_pix_fmt!r}, falling back to yuv420p")
        _inter_pix_fmt = "yuv420p"

    def _sdr_color_args(video: Path) -> list[str]:
        # Copy color metadata only for SDR (bt709) sources. HDR/BT.2020/PQ/HLG
        # tags are not copied onto 8-bit SDR output without tone mapping.
        try:
            import subprocess as _sp
            out = _sp.check_output(
                [shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=color_primaries,color_transfer,color_space,color_range",
                 "-of", "json", str(video)],
                text=True, encoding="utf-8", errors="replace", timeout=30,
            )
            streams = json.loads(out or "{}").get("streams") or []
            if not streams:
                return []
            st = streams[0]
            cp = (st.get("color_primaries") or "").lower()
            ct = (st.get("color_transfer") or "").lower()
            cs = (st.get("color_space") or "").lower()
            cr = (st.get("color_range") or "").lower()
            if not (cp in ("", "bt709") and ct in ("", "bt709") and cs in ("", "bt709")
                    and cr in ("", "tv", "mpeg")):
                return []
            args_l: list[str] = []
            if cp:
                args_l += ["-color_primaries", cp]
            if ct:
                args_l += ["-color_trc", ct]
            if cs:
                args_l += ["-colorspace", cs]
            if cr:
                args_l += ["-color_range", cr]
            return args_l
        except Exception:
            return []

    # ---- Stage: normalise source rotation (iPhone .mov autorotate) ----
    # Apple stores orientation in the Display Matrix (side_data / displaymatrix),
    # so probe BOTH tags.rotate AND the side_data rotation before deciding.
    source_video = Path(source_video)
    _rover: dict[str, Any] = {}
    _duration = 0.0
    try:
        _probe = json.loads(subprocess.check_output(
            [shutil.which("ffprobe") or "ffprobe", "-v", "error",
             "-show_streams", "-show_format", "-of", "json", str(source_video)],
            text=True, encoding="utf-8", errors="replace", timeout=90,
        ))
        try:
            _duration = float((_probe.get("format") or {}).get("duration") or 0.0)
        except (TypeError, ValueError):
            _duration = 0.0
        for _st in _probe.get("streams") or []:
            if _st.get("codec_type") != "video":
                continue
            _rot: int | None = None
            if isinstance(_st.get("tags"), dict):
                try:
                    rotate_tag = _st["tags"].get("rotate")
                    _rot = int(rotate_tag) if rotate_tag not in (None, "") else None
                except (TypeError, ValueError):
                    _rot = None
            if _rot is None:
                for _sd in _st.get("side_data_list") or []:
                    if _sd.get("side_data_type") in ("Display Matrix", "displaymatrix"):
                        _rot = _sd.get("rotation")
                        try:
                            _rot = int(_rot)
                        except (TypeError, ValueError):
                            _rot = None
                        if _rot is not None:
                            break
            if _rot is None:
                # Display Matrix without explicit rotation -> parse displaymatrix
                # determinant (rows 0,1 vs 4,5) if present.
                for _sd in _st.get("side_data_list") or []:
                    _dm = _sd.get("displaymatrix") if isinstance(_sd.get("displaymatrix"), str) else ""
                    if _dm:
                        try:
                            nums = [int(p) for p in _dm.replace("x", " ").split() if p.lstrip("-").isdigit()]
                            if len(nums) >= 4:
                                _det = nums[0] * nums[3] - nums[1] * nums[2]
                                _rot = 0 if _det == 0 else (90 if _det > 0 else 270)
                        except Exception:
                            _rot = None
                        if _rot is not None:
                            break
            _rover = {"rotate": int(_rot or 0) % 360, "w": int(_st.get("width") or 0), "h": int(_st.get("height") or 0)}
            break
    except Exception:
        _rover = {}

    options = dict(options)
    options["_rotation_applied"] = _rover.get("rotate") in (90, 270)
    tracker = ProgressTracker(job_id, "montage", montage_plan(options),
                              label=str(options.get("title") or "Автомонтаж"))

    def _emit() -> None:
        snap = tracker.snapshot()
        if report_cb is not None:
            try:
                report_cb(snap)
            except Exception:
                pass
        progress_cb(snap["progress"], snap.get("active_stage") or snap["status"],
                    snap.get("detail", ""))

    def _throw_if_cancelled() -> None:
        if cancel_cb is not None:
            try:
                cancelled = bool(cancel_cb())
            except Exception:
                cancelled = False
            if cancelled:
                raise MontageCancelled("Отменено пользователем")

    def _start(key: str, detail: str = "") -> None:
        _throw_if_cancelled()
        tracker.start(key, detail)
        _emit()

    def _upd(key: str, progress: float | None, detail: str = "") -> None:
        tracker.update(key, progress, detail)
        _emit()

    def _done(key: str, detail: str = "") -> None:
        tracker.complete(key, detail)
        _emit()

    def _skip(key: str, detail: str = "") -> None:
        if key in tracker.stage_keys():
            tracker.skip(key, detail)
            _emit()

    _start("prepare", "Проверка и подготовка исходника...")
    _done("prepare", "Исходник готов")

    if _rover.get("rotate") in (90, 270):
        _start("rotate", "Нормализация ориентации (автоповорот)...")
        _norm = workdir / "normalized_source.mp4"
        _src_rot = int(_rover.get("rotate") or 0)
        _vw, _vh = int(_rover.get("w") or 0), int(_rover.get("h") or 0)
        if _src_rot in (90, 270) and _vw and _vh:
            _slot = f"{_vh}x{_vw}"
            _cmd = [
                shutil.which("ffmpeg") or "ffmpeg", "-y", "-noautorotate", "-display_rotation:v:0", "0", "-i", str(source_video),
                "-map", "0:v:0", "-map", "0:a:0?", "-vf", f"transpose={1 if _src_rot == 270 else 2},scale={_slot}:flags=lanczos",
                "-c:v", "libx264", "-preset", "fast", "-crf", "0", "-pix_fmt", _inter_pix_fmt,
            ] + _sdr_color_args(source_video) + [
                "-c:a", "copy",
                "-movflags", "+faststart",
                str(_norm),
            ]
            _record_cmd("rotate", _cmd)
            _code = run_ffmpeg_progress(_cmd, duration=_duration, cwd=workdir,
                                        log_path=workdir / "rotate.log",
                                        on_progress=lambda p: _upd("rotate", p),
                                        timeout=1800, job_id=job_id, cancel_cb=cancel_cb)
            if _code == 0 and _norm.exists():
                source_video = _norm
                _done("rotate", "Ориентация нормализована")
            else:
                _done("rotate", "Поворот не применён")
        else:
            _done("rotate", "Поворот не требуется")
    elif source_video.suffix.lower() == ".mov" and _rover.get("rotate") not in (90, 270):
        # Portrait iPhone .mov without explicit rotation tag: keep as-is (no rotation -> already upright)
        pass

    do_mirror = bool(options.get("mirror_horizontal", False))
    if do_mirror:
        _start("mirror", "Отражение исходного видео по горизонтали...")
        mirrored_source = workdir / "mirrored_source.mp4"
        mirror_cmd = [
            shutil.which("ffmpeg") or "ffmpeg", "-y", "-i", str(source_video),
            "-map", "0:v:0", "-map", "0:a:0?", "-vf", "hflip",
            "-c:v", "libx264", "-preset", "fast", "-crf", "0", "-pix_fmt", _inter_pix_fmt,
        ] + _sdr_color_args(source_video) + [
            "-c:a", "copy", "-metadata:s:v:0", "rotate=0", "-movflags", "+faststart",
            str(mirrored_source),
        ]
        _record_cmd("mirror", mirror_cmd)
        code = run_ffmpeg_progress(mirror_cmd, duration=_duration, cwd=workdir,
                                   log_path=workdir / "mirror.log",
                                   on_progress=lambda p: _upd("mirror", p),
                                   timeout=1800, job_id=job_id, cancel_cb=cancel_cb)
        if code != 0 or not mirrored_source.exists():
            raise RuntimeError("Не удалось отразить исходное видео")
        source_video = mirrored_source
        _done("mirror", "Исходное видео отражено")

    subtitle_mode = str(options.get("subtitle_mode") or "words")
    subtitle_position = str(options.get("subtitle_position") or "custom")
    animation_cfg = options.get("animation") or {}
    overlays_cfg = options.get("overlays") if isinstance(options.get("overlays"), list) else []
    zoom_override = options.get("zoom_timeline_override")
    resolved_camera = resolve_camera_options(options)
    camera_cfg = resolved_camera["camera"]
    edge_mode = resolved_camera["edge_mode"]
    do_track = resolved_camera["do_track"]
    do_zoom = resolved_camera["do_zoom_stage"]
    log_cb(f"Камера: {camera_cfg['mode']}; автозум: {'вкл' if options.get('autozoom') else 'выкл'}")
    do_subtitles = bool(options.get("subtitles", True))
    use_bigpickle = bool(options.get("bigpickle", True)) and internet_available()

    log_cb(f"BigPickle: {use_bigpickle} (интернет {'есть' if internet_available() else 'нет'})")
    ai_model = str(options.get("ai_model") or "").strip() or os.getenv("BIGPICKLE_MODEL", "opencode/big-pickle").strip()
    log_cb(f"Модель ИИ: {ai_model}")

    tracking_path = workdir / "face_tracking.json"
    zoom_out = workdir / "zoom.mp4"

    # ---- Stage: generate zoom/subtitle configs ----
    _start("configs", "Генерация конфигов зума и подсветки (BigPickle)...")
    env = dict(os.environ)
    env["USE_BIGPICKLE"] = "1" if use_bigpickle else "0"
    if ai_model and ai_model != env.get("BIGPICKLE_MODEL"):
        env["BIGPICKLE_MODEL"] = ai_model
    code = run_cmd(
        [PYTHON, "-u", str(MONAGE_DIR / "generate_configs_from_transcript_srt.py"), str(transcript)],
        cwd=workdir, log_path=workdir / "configs.log", timeout=900,
        line_cb=lambda line: _upd("configs", None, line.strip()[:120]),
        env=env, job_id=job_id,
    )
    if code != 0:
        raise RuntimeError("Не удалось сгенерировать конфиги зума/субтитров")
    output_for = {
        "zoom_timeline_config.json": str(zoom_out),
        "subtitle_timeline_config.json": str(workdir / "sub_sentences.mp4"),
        "subtitle_timeline_config_words.json": str(workdir / "sub_words.mp4"),
        "subtitle_timeline_config_phrases.json": str(workdir / "sub_phrases.mp4"),
    }
    for config_name, out_mp4 in output_for.items():
        config_path = workdir / config_name
        if not config_path.exists():
            continue
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        render = cfg.get("render") or {}
        render["video"] = str(source_video.resolve())
        render["output"] = out_mp4
        if config_name.startswith("subtitle"):
            override_y = options.get("subtitle_y")
            if isinstance(override_y, (int, float)) and 0 <= override_y <= 1:
                position_y = float(override_y)
            else:
                position_y = {"bottom": 0.78, "center": 0.5, "top": 0.18}.get(subtitle_position, 0.78)
            override_x = options.get("subtitle_x")
            if isinstance(override_x, (int, float)) and 0 <= override_x <= 1:
                position_x = float(override_x)
            else:
                position_x = 0.12
            render["position"] = {"x": position_x, "y": position_y, "anchor": "left"}
            if isinstance(render.get("style"), dict):
                scale = options.get("subtitle_scale")
                base_font = float(render["style"].get("font_size") or 64)
                if isinstance(scale, (int, float)) and scale > 0:
                    render["style"]["font_size"] = int(round(base_font * max(0.5, min(2.5, float(scale)))))
            if options.get("subtitle_box") is not None:
                render.setdefault("box", {})["enabled"] = bool(options["subtitle_box"])
        cfg["render"] = render
        config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    zoom_config_path = workdir / "zoom_timeline_config.json"
    manual_timeline = False
    if zoom_override not in (None, [], "") and zoom_config_path.exists():
        cfg = json.loads(zoom_config_path.read_text(encoding="utf-8"))
        events = []
        for entry in zoom_override if isinstance(zoom_override, list) else []:
            if not isinstance(entry, dict):
                continue
            try:
                time_val = float(entry.get("time") or 0)
                percent = int(entry.get("percent") or 0)
            except (TypeError, ValueError):
                continue
            events.append({"timecode": format_timecode(time_val), "percent": percent})
        if events:
            cfg["render"]["zoom"]["timeline"] = events
            zoom_config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            manual_timeline = True

    # The generator stays neutral about camera behaviour: per-job camera and
    # edge settings are injected here so config and CLI always agree.
    if zoom_config_path.exists():
        inject_camera_config(zoom_config_path, camera_cfg, edge_mode,
                             bool(options.get("autozoom", False)) or manual_timeline)

    _done("configs", "Конфиги готовы")

    if do_track:
        _start("faces", "Анализ лиц и глаз (MediaPipe)...")
        if not face_model_ready():
            raise RuntimeError("Модель face_landmarker.task не найдена — она скачивается один раз при наличии интернета")
        cmd = [
            PYTHON, "-u", str(MONAGE_DIR / "face_tracking.py"),
            "--input", str(source_video),
            "--output", str(tracking_path),
            "--model", str(FACE_MODEL),
            "--target-fps", "10",
            "--sample-step", "3",
            "--roi-mode", "upper",
            "--roi-upscale", "2.5",
        ]
        last_line: list[str] = ["Анализ лиц..."]
        code = run_cmd(cmd, cwd=workdir, log_path=workdir / "tracking.log", job_id=job_id,
                       line_cb=lambda line: (last_line.clear(), last_line.append("Анализ лиц: " + line.strip()[:100])))
        if code != 0:
            raise RuntimeError("Анализ лиц не удался")
        ensure_tracking_has_anchors(tracking_path)
        _done("faces", "Анализ лиц завершён")
    elif do_zoom:
        # Zoom without face tracking: the renderer still needs a tracking
        # file, so write static center anchors without MediaPipe.
        if tracking_path.exists():
            tracking_path.unlink()
        _start("faces", "Подготовка неподвижной опорной точки...")
        try:
            probe = ffprobe(source_video)
            streams = probe.get("streams") or []
            video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
            try:
                num, den = str(video_stream.get("r_frame_rate") or "30/1").split("/")
                static_fps = float(num) / float(den) if float(den) else 30.0
            except (ValueError, ZeroDivisionError):
                static_fps = 30.0
            write_static_tracking(
                tracking_path,
                width=int(video_stream.get("width") or 0),
                height=int(video_stream.get("height") or 0),
                fps=static_fps,
                duration=float((probe.get("format") or {}).get("duration") or 0),
            )
        except Exception as exc:
            raise RuntimeError(f"Не удалось подготовить опорную точку: {exc}")
        _done("faces", "Неподвижная опорная точка готова")
    else:
        if tracking_path.exists():
            tracking_path.unlink()

    if do_zoom:
        _start("zoom", "Применение автозума...")
        tracking_arg = str(tracking_path) if tracking_path.exists() else ""
        cmd = [
            PYTHON, "-u", str(MONAGE_DIR / "apply_face_zoom_v5.py"),
            "--config", str(workdir / "zoom_timeline_config.json"),
            "--video", str(source_video),
            "--output", str(zoom_out),
            "--zoom-source", "timeline",
            "--edge-mode", edge_mode,
        ] + ([ "--tracking", tracking_arg] if tracking_arg else [])
        _record_cmd("zoom", cmd)
        def zoom_line(line: str) -> None:
            if "processed" in line:
                match = [p for p in line.split() if "/" in p]
                if match:
                    current, total = match[-1].split("/")
                    try:
                        frac = int(current) / max(1, int(total))
                        _upd("zoom", frac * 100, line.strip()[:140])
                    except Exception:
                        pass
        code = run_cmd(cmd, cwd=workdir, log_path=workdir / "zoom.log", line_cb=zoom_line, job_id=job_id)
        if code != 0:
            raise RuntimeError("Автозум не удался")
        _done("zoom", "Автозум готов")
        base_video = zoom_out
    else:
        _skip("zoom", "Автозум отключён")
        base_video = source_video

    # ---- Stage: subtitles ----
    # Overlay compositing deliberately runs after mirroring and BEFORE
    # subtitles: only the source video is flipped; overlay artwork always
    # keeps its original orientation, and subtitles are drawn last (on top).
    used_overlays: list[dict[str, Any]] = []
    for index, raw in enumerate(overlays_cfg[:20]):
        if not isinstance(raw, dict):
            continue
        start = max(0.0, float(raw.get("start") or 0.0))
        end = max(start + 0.05, float(raw.get("end") if raw.get("end") is not None else 86400.0))
        overlay = {
            "uid": str(raw.get("uid") or f"overlay-{index + 1}"),
            "start": start,
            "end": end,
            "x": max(0.0, min(1.0, float(raw.get("x") if raw.get("x") is not None else 0.5))),
            "y": max(0.0, min(1.0, float(raw.get("y") if raw.get("y") is not None else 0.5))),
            "size": max(20, min(1200, int(raw.get("size") or 180))),
            "z_index": int(raw.get("z_index") if raw.get("z_index") is not None else index),
            "motion": "static",
            "offset_x": 0,
        }
        overlay_kind = str(raw.get("kind") or "")
        if overlay_kind == "image":
            image_path = Path(str(raw.get("image_path") or "")).resolve()
            try:
                image_path.relative_to(workdir.resolve())
            except ValueError:
                raise RuntimeError("Файл наложения находится вне рабочей директории")
            if not image_path.is_file():
                raise RuntimeError(f"Изображение наложения не найдено: {image_path.name}")
            overlay.update({
                "kind": "image",
                "image_path": str(image_path),
                "name": str(raw.get("name") or image_path.name),
            })
        else:
            animation_id = str(raw.get("animation_id") or raw.get("id") or "")
            if Path(animation_id).name != animation_id or not (ANIMATIONS_DIR / animation_id / "manifest.json").exists():
                raise RuntimeError(f"Анимация {animation_id} не найдена в {ANIMATIONS_DIR}")
            overlay.update({"kind": "animation", "animation_id": animation_id})
        used_overlays.append(overlay)

    used_animation: dict[str, Any] | None = None
    used_animations: list[dict[str, Any]] = []
    render_layers = list(used_overlays)

    timeline_entries = [entry for entry in
                        (options.get("animations") if isinstance(options.get("animations"), list) else [])
                        if isinstance(entry, dict)]
    if not timeline_entries and bool(animation_cfg.get("enabled")) and str(animation_cfg.get("id") or ""):
        # Legacy single-animation jobs predate the list.
        timeline_entries = [animation_cfg]
    animation_layers, used_animations = timeline_animation_layers(timeline_entries)
    render_layers.extend(animation_layers)
    if used_animations:
        used_animation = dict(used_animations[0])

    # Visual layers composite BEFORE subtitles on purpose: subtitles are drawn
    # last so no overlay or animation can ever cover them.
    layered_video = base_video
    if render_layers:
        _start("animation", f"Рендер визуальных слоёв: {len(render_layers)}...")
        anim_out = workdir / "with_visual_layers.mp4"
        overlay_config = workdir / "overlay_timeline_config.json"
        overlay_config.write_text(json.dumps({
            "video": str(base_video.resolve()),
            "output": str(anim_out.resolve()),
            "animations_dir": str(ANIMATIONS_DIR.resolve()),
            "overlays": render_layers,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        cmd = [
            PYTHON, "-u", str(MONAGE_DIR / "apply_timeline_animation.py"),
            "--config", str(overlay_config),
        ]

        _record_cmd("animation", cmd)
        def anim_line(line: str) -> None:
            if "processed" in line:
                try:
                    current, total = [p for p in line.split() if "/" in p][0].split("/")
                    frac = int(current) / max(1, int(total))
                    _upd("animation", frac * 100, line.strip()[:140])
                except Exception:
                    pass
        code = run_cmd(cmd, cwd=workdir, log_path=workdir / "animation.log", line_cb=anim_line, job_id=job_id)
        if code != 0:
            raise RuntimeError("Рендер визуальных слоёв не удался")
        layered_video = anim_out
        _done("animation", "Визуальные слои готовы")
    else:
        _skip("animation", "Нет слоёв для наложения")

    outputs: dict[str, Path] = {}
    if do_subtitles:
        selected_font = str(options.get("subtitle_font") or "")
        allowed_fonts = {item["path"] for item in font_options()}
        font = Path(selected_font) if selected_font in allowed_fonts else (DEFAULT_FONT if DEFAULT_FONT.exists() else None)
        configs = {
            "sentences": ("subtitle_timeline_config.json", "sub_sentences.mp4"),
            "words": ("subtitle_timeline_config_words.json", "sub_words.mp4"),
            "phrases": ("subtitle_timeline_config_phrases.json", "sub_phrases.mp4"),
        }
        config_name, out_name = configs.get(subtitle_mode, configs["sentences"])
        out_mp4 = workdir / out_name
        _start("subtitles", "Наложение цветных субтитров...")
        cmd = [
            PYTHON, "-u", str(MONAGE_DIR / "apply_auto_subtitles_v2.py"),
            "--config", str(workdir / config_name),
            "--video", str(layered_video),
            "--output", str(out_mp4),
        ]
        if font:
            cmd += ["--font", str(font)]
        def sub_line(line: str) -> None:
            if "progress:" in line or "frame=" in line:
                try:
                    prefix = line.split("(")[-1].split("%")[0].strip()
                    source_pct = max(0.0, min(100.0, float(prefix)))
                    _upd("subtitles", source_pct, line.strip()[:140])
                except Exception:
                    _upd("subtitles", None, line.strip()[:140])
        _record_cmd("subtitles", cmd)
        code = run_cmd(cmd, cwd=workdir, log_path=workdir / "subtitles.log", line_cb=sub_line, job_id=job_id)
        if code != 0:
            raise RuntimeError("Наложение субтитров не удалось")
        outputs[subtitle_mode] = out_mp4
        _done("subtitles", "Субтитры готовы")
    else:
        _skip("subtitles", "Субтитры отключены")
        outputs["video"] = layered_video

    final_mp4 = outputs.get(subtitle_mode) or outputs.get("video")
    if final_mp4 is None or not Path(final_mp4).exists():
        raise RuntimeError("Не создан итоговый файл")
    final_mp4 = Path(final_mp4)

    if not usable_video(final_mp4):
        raise RuntimeError("Не создан пригодный итоговый файл")

    blur_banned_words = bool(options.get("blur_banned_words", False))
    censor_srt_text = str(options.get("audio_censor_srt") or srt_text)
    muted_intervals = find_mute_intervals(censor_srt_text) if blur_banned_words else []
    if muted_intervals:
        _start("audio_censor", f"Заглушение нежелательных слов: {len(muted_intervals)}")
        probe = ffprobe(final_mp4)
        if any(stream.get("codec_type") == "audio" for stream in probe.get("streams") or []):
            censored_mp4 = workdir / "censored_audio.mp4"
            censor_cmd = [
                shutil.which("ffmpeg") or "ffmpeg", "-y", "-i", str(final_mp4),
                "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                "-af", build_volume_filter(muted_intervals),
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(censored_mp4),
            ]
            _record_cmd("audio_censor", censor_cmd)
            censor_proc = subprocess.Popen(censor_cmd, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.PIPE, cwd=str(workdir))
            register_job_proc(job_id, censor_proc)
            try:
                while True:
                    try:
                        _, censor_stderr = censor_proc.communicate(timeout=1.0)
                        break
                    except subprocess.TimeoutExpired:
                        _throw_if_cancelled()
                        continue
            except MontageCancelled:
                try:
                    censor_proc.kill()
                except Exception:
                    pass
                raise
            finally:
                unregister_job_proc(job_id, censor_proc)
            if censor_proc.returncode != 0 or not censored_mp4.is_file():
                detail = (censor_stderr or b"").decode("utf-8", errors="replace")[-400:]
                raise RuntimeError(f"Не удалось заглушить нежелательные слова: {detail}")
            final_mp4 = censored_mp4
            log_cb(f"Нежелательные слова заглушены: {len(muted_intervals)} интервалов")
            _done("audio_censor", f"Заглушено слов: {len(muted_intervals)}")
        else:
            muted_intervals = []
            _skip("audio_censor", "В видео нет звуковой дорожки")
    else:
        _skip("audio_censor", "Заглушение не требуется")

    # ---- Master export source -------------------------------------------
    # The fully effected stream (rotate/transpose, mirror, zoom, overlays,
    # subtitles and the optional audio censoring) already exists as
    # ``final_mp4``; recording it as the master source adds no extra encode
    # pass.  ``copy`` mode additionally places a byte-exact duplicate into
    # ``<workdir>/master.mp4`` (gated by a free-disk-space check); ``none``
    # disables master tracking entirely.
    master_mode = str(os.environ.get("AUTOMONTAGE_MASTER_MODE") or "inplace").strip().lower()
    if master_mode not in ("inplace", "copy", "none"):
        print(f"[master] unknown AUTOMONTAGE_MASTER_MODE \"{master_mode}\", fallback: inplace", file=sys.stderr)
        master_mode = "inplace"
    master_video_name = ""
    if master_mode == "copy":
        try:
            needed_bytes = int(Path(final_mp4).stat().st_size) + 64 * 1024 * 1024
            free_bytes = int(shutil.disk_usage(workdir).free)
        except Exception:
            needed_bytes, free_bytes = 0, 0
        if free_bytes >= needed_bytes:
            try:
                master_out = workdir / "master.mp4"
                shutil.copy2(final_mp4, master_out)
                master_video_name = master_out.name
            except Exception as exc:
                print(f"[master] copy failed, fallback: inplace ({exc})", file=sys.stderr)
        else:
            print(f"[master] not enough disk space for copy, fallback: inplace "
                  f"(free {free_bytes} < needed {needed_bytes})", file=sys.stderr)
        if not master_video_name:
            master_mode = "inplace"
    if master_mode == "inplace":
        master_video_name = Path(final_mp4).name

    delivery_mp4 = workdir / "final.mp4"
    delivery_cmd = [
        shutil.which("ffmpeg") or "ffmpeg", "-y", "-i", str(final_mp4),
        "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx265", "-tag:v", "hvc1",
        "-preset", "medium", "-crf", "14",
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(delivery_mp4),
    ]
    preview_mp4 = workdir / "preview.mp4"
    preview_cmd = [
        shutil.which("ffmpeg") or "ffmpeg", "-y", "-i", str(final_mp4),
        "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264",
        "-profile:v", "high", "-level:v", "4.1", "-preset", "medium", "-crf", "12",
        "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(preview_mp4),
    ]
    _record_cmd("finalize_hevc", delivery_cmd)
    _record_cmd("preview_h264", preview_cmd)
    _start("finalize", "Кодирование финального файла (H.265)...")
    _start("preview", "Кодирование версии для просмотра (H.264)...")

    def _late_final(p: float) -> None:
        _upd("finalize", p)

    def _late_preview(p: float) -> None:
        _upd("preview", p)

    with ThreadPoolExecutor(max_workers=2) as executor:
        delivery_future = executor.submit(
            run_ffmpeg_progress, delivery_cmd, duration=_duration, cwd=workdir,
            log_path=workdir / "finalize.log", on_progress=_late_final, timeout=1800,
            job_id=job_id, cancel_cb=cancel_cb)
        preview_future = executor.submit(
            run_ffmpeg_progress, preview_cmd, duration=_duration, cwd=workdir,
            log_path=workdir / "preview.log", on_progress=_late_preview, timeout=1800,
            job_id=job_id, cancel_cb=cancel_cb)
        try:
            delivery_code = delivery_future.result()
        except MontageCancelled:
            raise
        except Exception:
            delivery_code = 1
        try:
            preview_code = preview_future.result()
        except MontageCancelled:
            raise
        except Exception:
            preview_code = 1
    if delivery_code == 0 and usable_video(delivery_mp4):
        _done("finalize", "Финальный файл готов")
    elif usable_video(final_mp4):
        # Delivery encode failed but the previous full-quality stream is a
        # usable result — keep it, but never claim an unqualified success.
        delivery_mp4 = final_mp4
        warning = "Финальное кодирование H.265 не удалось, используется исходный поток"
        tracker.complete("finalize", warning, warning=warning)
        _emit()
    else:
        raise RuntimeError("Не удалось создать итоговый файл: кодирование завершилось без пригодного результата")
    if preview_code == 0 and usable_video(preview_mp4):
        _done("preview", "Preview готов")
    elif usable_video(delivery_mp4):
        preview_mp4 = delivery_mp4
        warning = "Превью не создано, для просмотра используется финальный файл"
        tracker.complete("preview", warning, warning=warning)
        _emit()
    else:
        raise RuntimeError("Не удалось создать ни финальный файл, ни превью")

    timeline_data: dict[str, Any] = {
        "subtitle_position": subtitle_position,
        "subtitle_x": options.get("subtitle_x"),
        "subtitle_y": options.get("subtitle_y"),
        "subtitle_scale": options.get("subtitle_scale"),
        "subtitle_mode": subtitle_mode,
    }
    try:
        zoom_cfg = json.loads((workdir / "zoom_timeline_config.json").read_text(encoding="utf-8"))
        events = (zoom_cfg.get("render") or {}).get("zoom", {}).get("timeline") or []
        timeline_data["zoom"] = [
            {"timecode": e.get("timecode") if isinstance(e, dict) else None,
             "time": (float(e.get("time")) if isinstance(e, dict) and e.get("time") is not None else None),
             "percent": int(e.get("percent") or 0) if isinstance(e, dict) else 0}
            for e in events if isinstance(e, dict)
        ]
    except Exception:
        pass
    if used_overlays:
        timeline_data["overlays"] = [
            {key: value for key, value in overlay.items() if key != "image_path"}
            for overlay in used_overlays
        ]
    if used_animation:
        timeline_data["animation"] = used_animation

    meta: dict[str, Any] = {
        "job_id": job_id,
        "result_video": str(Path(delivery_mp4).resolve()),
        "preview_video": str(Path(preview_mp4).resolve()),
        "subtitle_mode": subtitle_mode,
        "face_tracking": camera_cfg["mode"] != "off",
        "camera_mode": camera_cfg["mode"],
        "camera": {key: camera_cfg[key] for key in
                   ("mode", "follow_strength", "lag_seconds", "min_zoom", "wobble")},
        "autozoom": options.get("autozoom", False),
        "zoom_stage": do_zoom,
        "mirror_horizontal": do_mirror,
        "subtitles": do_subtitles,
        "bigpickle": use_bigpickle,
        "blur_banned_words": blur_banned_words,
        "blurred_word_intervals": len(muted_intervals),
        "master_video": master_video_name,
        "master_mode": master_mode,
        "animations": list(animation_ids()),
        "timeline": timeline_data,
    }
    try:
        probe = ffprobe(Path(final_mp4))
        meta["duration"] = float(probe.get("format", {}).get("duration") or 0)
    except Exception:
        meta["duration"] = 0
    for key in tracker.stage_keys():
        if tracker.stage(key).status == PENDING:
            tracker.skip(key, "Этап не выполнялся")
    _done("verify", "Проверка результата")
    warnings = tracker.warnings()
    meta["warnings"] = warnings
    tracker.finish("Рендер завершён с предупреждениями" if warnings else "Рендер завершён")
    _emit()
    # Diagnostic mode: persist collected ffmpeg commands.
    if _diag and ffmpeg_commands:
        try:
            (workdir / "ffmpeg_commands.json").write_text(
                json.dumps(ffmpeg_commands, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
    return meta


# ---------------- Publishers ----------------

def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _transcode_to_fit(source: Path, output: Path, target_bytes: int, duration: float,
                      progress_cb: Callable[[float, str], None],
                      job_id: str | None = None,
                      cancel_cb: Callable[[], bool] | None = None) -> None:
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    audio_bitrate = 160_000
    vbr = max(400_000, int(target_bytes * 8 * 0.96 / max(0.1, duration)) - audio_bitrate)
    for attempt in range(2):
        output.unlink(missing_ok=True)
        bitrate = max(400_000, int(vbr * (0.88 ** attempt)))
        cmd = [ffmpeg, "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
               "-c:v", "libx264", "-preset", "medium", "-b:v", str(bitrate),
               "-maxrate", str(int(bitrate * 1.08)), "-bufsize", str(int(bitrate * 2.16)),
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
               "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(output)]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace")
        register_job_proc(job_id, proc)
        assert proc.stdout is not None
        last = -1
        try:
            for line in proc.stdout:
                if cancel_cb is not None:
                    try:
                        cancelled = bool(cancel_cb())
                    except Exception:
                        cancelled = False
                    if cancelled:
                        raise MontageCancelled("Отменено пользователем")
                key, _, value = line.strip().partition("=")
                if key not in {"out_time_us", "out_time_ms"}:
                    continue
                try:
                    percent = min(99, max(1, int(float(value) / 1_000_000 / max(0.1, duration) * 100)))
                except (ValueError, ZeroDivisionError):
                    continue
                if percent != last:
                    last = percent
                    progress_cb(percent, f"Сжатие для публикации: {percent}%")
            code = proc.wait()
        except MontageCancelled:
            try:
                proc.kill()
            except Exception:
                pass
            raise
        finally:
            unregister_job_proc(job_id, proc)
        if code != 0 or not output.exists():
            raise RuntimeError("ffmpeg transcode for publishing failed")
        if output.stat().st_size <= target_bytes:
            progress_cb(100, "Файл готов для публикации")
            return
    raise RuntimeError("cannot fit file under target size")


def _publish_targets(
    *,
    job_id: str,
    workdir: Path,
    source_video: Path,
    mirrored_source_video: Path | None,
    original_video: Path,
    transcript_text: str,
    targets: list[dict[str, Any]],
    progress_cb: Callable[[float, str, str], None],
    log_cb: Callable[[str], None],
    step_cb: Callable[[str, str, str, str], None] | None = None,
    prepared_targets: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    load_secrets_env()
    sw = get_software_defaults()
    results: dict[str, Any] = {}

    def emit_step(key: str, label: str, status: str = "running", detail: str = "") -> None:
        """Report a named publish sub-step (spec section 10)."""
        if step_cb is not None:
            step_cb(key, label, status, detail)

    reel = _load_module("publish_reel", INSTAPOSTER_DIR / "publish_reel_github.py")
    trial_mod = _load_module("publish_trial", INSTAPOSTER_DIR / "publish_trial_reel_github.py")
    threads_mod = _load_module("publish_threads", INSTAPOSTER_DIR / "publish_threads_carousel_github.py")

    for index, target in enumerate(targets):
        kind = str(target.get("kind") or "")
        label = str(target.get("label") or kind)
        base = index * 100 / max(1, len(targets))
        span = 100 / max(1, len(targets))
        result_key = str(target.get("id") or kind)
        prepared = None
        if prepared_targets is not None:
            prepared = prepared_targets.get(result_key) or {}
        if prepared is not None:
            # Publishing only uses the file that the preparation stage produced;
            # it never transcodes/normalises/mirrors here (spec: publish = send).
            candidate = prepared.get("video_path")
            target_source = Path(str(candidate)) if candidate else None
        else:
            target_source = mirrored_source_video if bool(target.get("mirrored")) else source_video
        if target_source is None or not Path(target_source).exists():
            preparation_error = str(target.get("preparation_error") or "").strip()
            emit_step("file", "Проверка готового файла", "failed",
                      preparation_error or "Видео-версия не подготовлена")
            if preparation_error:
                raise RuntimeError(f"Не удалось подготовить видео для цели {label}: {preparation_error}")
            raise RuntimeError(f"Не подготовлена видео-версия для цели: {label}")
        emit_step("file", "Проверка готового файла", "completed")

        def sub_progress(p: float, detail: str) -> None:
            progress_cb(min(99, base + span * p / 100), kind, detail)

        log_cb(f"publish target: {label}")
        progress_cb(base + 1, kind, f"Публикация: {label}")

        if kind in {"instagram", "trial"}:
            if not sw["instagram_ready"]:
                emit_step("auth", "Проверка авторизации", "failed", "Не настроен доступ к Instagram")
                if not sw.get("github_ready"):
                    raise RuntimeError(
                        "Instagram: для загрузки видео в GitHub Raw не задан GITHUB_TOKEN"
                    )
                raise RuntimeError("Instagram: не настроен INSTAGRAM_ACCESS_TOKEN / IG_USER_ID")
            emit_step("auth", "Проверка авторизации", "completed")
            from .instagram_publisher import InstagramPublisher
            instagram = InstagramPublisher(
                INSTAPOSTER_DIR, shutil.which("ffmpeg") or "ffmpeg",
                workdir / "publish.log", log_cb,
            )
            variant = "trial" if kind == "trial" else "normal"
            staged = Path(target_source)
            emit_step("prepare", "Проверка готового файла", "completed", Path(staged).name)
            graph_ip, raw_ip = instagram.resolve_hosts()
            emit_step("upload", "Загрузка видео", "running")
            sub_progress(25, "Загрузка в GitHub Raw...")
            _, raw_url = instagram.upload_to_github(staged)
            time.sleep(3)
            instagram.verify_public_video(raw_url, raw_ip, lambda p, d: sub_progress(25 + p * 0.2, d))
            emit_step("upload", "Загрузка видео", "completed")
            cover_url = ""
            prepared_cover = (prepared or {}).get("cover_path")
            if prepared_cover and Path(str(prepared_cover)).is_file():
                sub_progress(44, "Загрузка обложки в GitHub Raw...")
                _, cover_url = instagram.upload_cover_file(Path(str(prepared_cover)))
                time.sleep(3)
            caption = str(target.get("caption") or target.get("text") or "")
            emit_step("processing", "Обработка на стороне площадки", "running")
            container = instagram.create_container(variant, graph_ip, raw_url, caption, cover_url)
            instagram.wait_container(variant, graph_ip, container, lambda p, d: sub_progress(45 + p * 0.5, d))
            emit_step("processing", "Обработка на стороне площадки", "completed")
            publish_started_at = datetime.now(timezone.utc)
            emit_step("publish", "Подтверждение публикации", "running")
            try:
                media_id = instagram.publish_container(variant, graph_ip, container)
                info = instagram.media_info(variant, graph_ip, media_id)
            except Exception as publish_exc:
                log_cb(
                    "media_publish вернул ошибку; проверяю, не была ли публикация "
                    "фактически создана Meta"
                )
                info = instagram.find_recent_publication(
                    variant,
                    graph_ip,
                    caption,
                    publish_started_at - timedelta(minutes=2),
                )
                if not info or not str(info.get("id") or "").strip():
                    raise RuntimeError(
                        "AMBIGUOUS_MEDIA_PUBLISH: Meta могла опубликовать Reel, "
                        "но media_id не подтверждён; автоматический повтор запрещён. "
                        f"Исходная ошибка: {publish_exc}"
                    ) from publish_exc
                media_id = str(info["id"])
                log_cb(f"media_publish восстановлен по списку публикаций: {media_id}")
            emit_step("publish", "Подтверждение публикации", "completed")
            permalink = str(info.get("permalink") or "")
            emit_step("link", "Получение ссылки", "completed", permalink or media_id)
            published_result = {"media_id": media_id, "permalink": permalink, "kind": label}
            try:
                from .social_stats import archive_instagram_reel
                archive_dir = archive_instagram_reel(
                    media=info,
                    account_id=str(reel.IG_USER_ID),
                    transcript=transcript_text,
                    source_video=original_video,
                    published_video=staged,
                    post_text=caption,
                    job_id=job_id,
                    variant=result_key,
                )
                published_result["archive_dir"] = str(archive_dir)
                log_cb(f"socialmediastats: {media_id} сохранён в {archive_dir}")
            except Exception as exc:
                published_result["archive_error"] = str(exc)
                log_cb(f"socialmediastats archive failed: {media_id}: {exc}")
            results[result_key] = published_result
            progress_cb(base + span, kind, f"Instagram готов: {permalink or media_id}")

        elif kind == "youtube":
            if not sw["youtube_ready"]:
                emit_step("auth", "Проверка авторизации", "failed", "YouTube не авторизован")
                raise RuntimeError("YouTube: OAuth token.json не настроен")
            from .youtube_publisher import YouTubePublisher
            youtube = YouTubePublisher(INSTAPOSTER_DIR, Path(sys.executable), workdir / "youtube.log")
            youtube.check_auth()
            emit_step("auth", "Проверка авторизации", "completed")
            youtube_source = Path(target_source)
            emit_step("prepare", "Проверка готового файла", "completed", Path(youtube_source).name)
            title = str(target.get("title") or target.get("text") or "Мой ролик")
            thumbnail_path = None
            prepared_thumbnail = (prepared or {}).get("thumbnail_path")
            if prepared_thumbnail and Path(str(prepared_thumbnail)).is_file():
                thumbnail_path = Path(str(prepared_thumbnail))
            upload_ready_called: list[bool] = [False]
            def upload_ready() -> None:
                upload_ready_called[0] = True
            def yt_progress(p: float, detail: str) -> None:
                sub_progress(25 + p * 0.75, detail)
            emit_step("upload", "Загрузка видео", "running")
            info = youtube.upload(youtube_source, title, yt_progress, upload_ready, thumbnail_path)
            emit_step("upload", "Загрузка видео", "completed")
            emit_step("publish", "Подтверждение публикации", "completed", "Видео загружено")
            emit_step("link", "Получение ссылки", "completed", info["shorts_url"])
            results[result_key] = {"video_id": info["video_id"], "shorts_url": info["shorts_url"], "watch_url": info["watch_url"], "kind": label}
            progress_cb(base + span, kind, f"YouTube готов: {info['shorts_url']}")

        elif kind == "threads":
            if not sw["threads_ready"]:
                emit_step("auth", "Проверка авторизации", "failed", "Не настроен доступ к Threads")
                if not sw.get("github_ready"):
                    raise RuntimeError(
                        "Threads: для загрузки видео в GitHub Raw не задан GITHUB_TOKEN"
                    )
                raise RuntimeError("Threads: не настроен THREADS_ACCESS_TOKEN / THREADS_USER_ID")
            emit_step("auth", "Проверка авторизации", "completed")
            from .instagram_publisher import InstagramPublisher
            instagram = InstagramPublisher(
                INSTAPOSTER_DIR, shutil.which("ffmpeg") or "ffmpeg",
                workdir / "publish.log", log_cb,
            )
            staged = Path(target_source)
            sub_progress(10, "Проверка готового файла...")
            emit_step("prepare", "Проверка готового файла", "completed", Path(staged).name)
            graph_ip = threads_mod.resolve_host_via_doh(threads_mod.THREADS_HOST)
            raw_ip = threads_mod.resolve_host_via_doh(threads_mod.RAW_HOST)
            emit_step("upload", "Загрузка видео", "running")
            sub_progress(35, "Загрузка в GitHub Raw...")
            _, raw_url = reel.publish_video_to_github(staged)
            time.sleep(3)
            sub_progress(45, "Проверка HTTPS-ссылки...")
            reel.check_public_video(raw_url, raw_ip)
            emit_step("upload", "Загрузка видео", "completed")
            text = str(target.get("text") or target.get("caption") or "")
            container = threads_mod.threads_request(graph_ip, "POST", "me/threads", {
                "media_type": "VIDEO",
                "video_url": raw_url,
                "text": text,
                "access_token": threads_mod.ACCESS_TOKEN,
            })
            container_id = str(container["id"])
            emit_step("processing", "Обработка на стороне площадки", "running")
            sub_progress(55, "Ожидание обработки Threads...")
            threads_mod.wait_container(graph_ip, container_id, timeout_seconds=180)
            emit_step("processing", "Обработка на стороне площадки", "completed")
            emit_step("publish", "Подтверждение публикации", "running")
            published = threads_mod.threads_request(graph_ip, "POST", "me/threads_publish", {
                "creation_id": container_id,
                "access_token": threads_mod.ACCESS_TOKEN,
            })
            thread_id = str(published["id"])
            emit_step("publish", "Подтверждение публикации", "completed")
            info = threads_mod.get_thread_info(graph_ip, thread_id)
            permalink = str(info.get("permalink") or "")
            emit_step("link", "Получение ссылки", "completed", permalink or thread_id)
            results[result_key] = {"thread_id": thread_id, "permalink": permalink, "kind": label}
            progress_cb(base + span, kind, f"Threads готов: {permalink or thread_id}")

        elif kind == "telegram":
            bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
            if not bot_token or not chat_id:
                emit_step("auth", "Проверка авторизации", "failed", "Не настроен доступ к Telegram")
                raise RuntimeError("Telegram: не настроен TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
            emit_step("auth", "Проверка авторизации", "completed")
            tg_file = Path(target_source)
            sub_progress(15, "Проверка готового файла...")
            emit_step("prepare", "Проверка готового файла", "completed", Path(tg_file).name)
            caption = str(target.get("caption") or target.get("text") or "")
            curl = shutil.which("curl") or "curl"
            cmd = [curl, "-sS", "-X", "POST",
                   f"https://api.telegram.org/bot{bot_token}/sendVideo",
                   "-F", f"chat_id={chat_id}",
                   "-F", "supports_streaming=true",
                   "-F", f"caption={caption}",
                   "-F", "video=@%s;type=video/mp4" % str(tg_file)]
            emit_step("upload", "Загрузка видео", "running")
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
            if proc.returncode != 0:
                raise RuntimeError(f"Telegram send failed: {proc.stderr[-400:]}")
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError:
                raise RuntimeError(f"Telegram bad response: {proc.stdout[:400]}")
            if not data.get("ok"):
                raise RuntimeError(f"Telegram error: {json.dumps(data, ensure_ascii=False)[:400]}")
            emit_step("upload", "Загрузка видео", "completed")
            result = data.get("result", {}) or {}
            message_id = str(result.get("message_id") or "")
            emit_step("publish", "Подтверждение публикации", "completed")
            emit_step("link", "Получение ссылки", "completed", f"message_id={message_id}")
            results[result_key] = {"message_id": message_id, "kind": label, "ok": True}
            progress_cb(base + span, kind, f"Telegram готов: message_id={message_id}")

        else:
            raise RuntimeError(f"unsupported publish target: {kind}")

    return {"results": results}


def publish_job(
    *,
    job_id: str,
    workdir: Path,
    source_video: Path,
    mirrored_source_video: Path | None = None,
    original_video: Path | None = None,
    transcript_text: str = "",
    targets: list[dict[str, Any]],
    progress_cb: Callable[[float, str, str], None],
    log_cb: Callable[[str], None],
    target_progress_cb: Callable[[str, float, str, str], None] | None = None,
    confirmed_cb: Callable[[str, dict[str, Any]], None] | None = None,
    target_step_cb: Callable[[str, str, str, str, str], None] | None = None,
    prepared_targets: dict[str, dict[str, Any]] | None = None,
    cancel_cb: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    instagram_priority = {("trial", False): 0, ("trial", True): 1, ("instagram", False): 2, ("instagram", True): 3}
    instagram_targets = sorted(
        [target for target in targets if str(target.get("kind") or "") in {"trial", "instagram"}],
        key=lambda target: instagram_priority.get(
            (str(target.get("kind") or ""), bool(target.get("mirrored"))), 99
        ),
    )
    youtube_targets = sorted(
        [target for target in targets if str(target.get("kind") or "") == "youtube"],
        key=lambda target: bool(target.get("mirrored")),
    )
    other_targets = [
        target for target in targets
        if str(target.get("kind") or "") not in {"trial", "instagram", "youtube"}
    ]
    lanes = [("instagram", instagram_targets + other_targets), ("youtube", youtube_targets)]
    lanes = [(name, lane_targets) for name, lane_targets in lanes if lane_targets]
    state = {name: 0.0 for name, _ in lanes}
    weights = {name: len(lane_targets) for name, lane_targets in lanes}
    total_weight = max(1, sum(weights.values()))
    state_lock = threading.Lock()

    def update_lane(name: str, percent: float, stage: str, detail: str) -> None:
        with state_lock:
            state[name] = max(state[name], max(0.0, min(100.0, percent)))
            overall = sum(state[key] * weights[key] for key in state) / total_weight
        progress_cb(overall, stage, detail)

    def run_lane(name: str, lane_targets: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, str]]:
        lane_results: dict[str, Any] = {}
        lane_errors: dict[str, str] = {}
        count = max(1, len(lane_targets))
        for index, target in enumerate(lane_targets):
            if cancel_cb is not None:
                try:
                    cancelled = bool(cancel_cb())
                except Exception:
                    cancelled = False
                if cancelled:
                    raise MontageCancelled("Отменено пользователем")
            target_id = str(target.get("id") or target.get("kind") or f"target_{index}")

            def target_progress(percent: float, stage: str, detail: str) -> None:
                if target_progress_cb:
                    target_progress_cb(target_id, percent, detail, "publishing")
                update_lane(name, (index + percent / 100) * 100 / count, stage, detail)

            def target_step(step_key: str, step_label: str, status: str, detail: str) -> None:
                if target_step_cb:
                    target_step_cb(target_id, step_key, step_label, status, detail)

            try:
                result = _publish_targets(
                    job_id=job_id,
                    workdir=workdir,
                    source_video=source_video,
                    mirrored_source_video=mirrored_source_video,
                    original_video=original_video or source_video,
                    transcript_text=transcript_text,
                    targets=[target],
                    progress_cb=target_progress,
                    log_cb=log_cb,
                    step_cb=target_step,
                    prepared_targets=prepared_targets,
                )
                lane_results.update(result["results"])
                if confirmed_cb:
                    confirmed_cb(target_id, dict(result["results"][target_id]))
                if target_progress_cb:
                    target_progress_cb(target_id, 100, "Опубликовано", "done")
            except Exception as exc:
                lane_errors[target_id] = str(exc)
                if target_progress_cb:
                    outcome = "unknown" if str(exc).startswith("AMBIGUOUS_MEDIA_PUBLISH:") else "error"
                    target_progress_cb(target_id, 100, str(exc), outcome)
                log_cb(f"publish target failed: {target_id}: {exc}")
            update_lane(name, (index + 1) * 100 / count, target_id, f"Цель завершена: {target_id}")
        return lane_results, lane_errors

    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(2, len(lanes))) as executor:
        futures = [executor.submit(run_lane, name, lane_targets) for name, lane_targets in lanes]
        for future in futures:
            lane_results, lane_errors = future.result()
            results.update(lane_results)
            errors.update(lane_errors)
    return {"results": results, "errors": errors}
