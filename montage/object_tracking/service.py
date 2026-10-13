"""Object-tracking sessions (runs in the FastAPI process, no heavy imports).

Heavy SAM work happens in ``sam_worker.py`` under the isolated venv as a
subprocess.  This module owns: environment probing, session lifecycle,
prompt validation, worker supervision (progress/cancel), time estimates,
confirmation gating and reuse checks.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from . import coords as coords_module
from . import storage as storage_module

PACKAGE_DIR = Path(__file__).resolve().parent
WORKER_SCRIPT = PACKAGE_DIR / "sam_worker.py"

SAM_VENV = Path(os.environ.get("SAM2_VENV") or Path.home() / ".venvs" / "sam2")
SAM_PYTHON = Path(os.environ.get("SAM2_PYTHON") or SAM_VENV / "bin" / "python")
SAM_CHECKPOINT = Path(os.environ.get("SAM2_CHECKPOINT") or
                      SAM_VENV / "checkpoints" / "sam2.1_hiera_small.pt")
SAM_MODEL_CFG = os.environ.get("SAM2_MODEL_CFG") or "configs/sam2.1/sam2.1_hiera_s.yaml"

# The main app interpreter runs as x86_64 under Rosetta while the sam2 venv
# is arm64: a translated parent spawns the x86_64 slice of a universal
# binary, which then cannot load arm64 torch.  Forcing arm64 fixes it.
_SAM_PREFIX: list[str] = []
if platform.machine() != "arm64" and sys.platform == "darwin":
    _SAM_PREFIX = ["arch", "-arm64"]

# Measured on Apple M1/MPS, hiera_small (probe 2026-10-13); recalibrated from
# real runs when available.  Never presented as a guarantee.
DEFAULT_SEC_PER_FRAME = 1.6

SESSION_TTL_SECONDS = 7 * 24 * 3600

_WORKER_PROCS: dict[str, "subprocess.Popen"] = {}
_WORKER_LOCK = threading.Lock()


def sessions_root(base: Path) -> Path:
    root = Path(base) / "object_tracking_sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def sam_status() -> dict[str, Any]:
    """Environment probe (no model load): venv, sam2 import, checkpoint."""
    info: dict[str, Any] = {"ok": False, "device": None, "reason": "",
                            "checkpoint": str(SAM_CHECKPOINT)}
    if not SAM_PYTHON.is_file():
        info["reason"] = "изолированное окружение SAM 2 не установлено"
        return info
    probe = subprocess.run(
        _SAM_PREFIX + [str(SAM_PYTHON), "-c",
         "import torch, sam2; "
         "print('mps' if torch.backends.mps.is_available() else 'cpu')"],
        capture_output=True, text=True, timeout=120)
    if probe.returncode != 0:
        info["reason"] = "sam2/torch недоступны в изолированном окружении"
        info["detail"] = (probe.stderr or "").strip()[-300:]
        return info
    info["device"] = (probe.stdout or "").strip().split()[-1]
    if not SAM_CHECKPOINT.is_file():
        info["reason"] = "checkpoint sam2.1_hiera_small не скачан"
        return info
    info["ok"] = True
    return info


def allowed_source(path: Path, extra_roots: list[Path]) -> Optional[Path]:
    """Resolve *path* strictly inside permitted roots (no arbitrary access)."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        return None
    if resolved.suffix.lower() not in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
        return None
    for root in extra_roots:
        try:
            if resolved.is_relative_to(Path(root).resolve()) and resolved.is_file():
                return resolved
        except OSError:
            continue
    return None


def create_session(root: Path, source: Path, fps: float = 6.0) -> dict[str, Any]:
    session_id = uuid.uuid4().hex[:12]
    session_dir = Path(root) / session_id
    storage_module.ensure_session(session_dir)
    payload = {
        "session_id": session_id,
        "schema_version": storage_module.SCHEMA_VERSION,
        "source": str(source),
        "fingerprint": storage_module.video_fingerprint(source),
        "analysis_fps": float(fps),
        "status": "selecting",
        "created_at": time.time(),
        "sec_per_frame": DEFAULT_SEC_PER_FRAME,
    }
    storage_module.write_selection(session_dir, payload)
    return {"session_id": session_id, "session_dir": str(session_dir)}


def get_session(root: Path, session_id: str) -> Optional[Path]:
    if not session_id or not session_id.replace("_", "").replace("-", "").isalnum():
        return None
    session_dir = Path(root) / Path(session_id).name
    if not session_dir.is_dir():
        return None
    return session_dir


def validate_prompts(raw: Any) -> tuple[list[dict[str, Any]], str]:
    """Validate user prompts; returns (prompts, error)."""
    if not isinstance(raw, list) or not raw:
        return [], "укажите объект точкой или прямоугольником"
    prompts: list[dict[str, Any]] = []
    for entry in raw[:8]:
        if not isinstance(entry, dict):
            continue
        try:
            frame_idx = max(0, int(entry.get("frame_idx", 0)))
        except (TypeError, ValueError):
            continue
        points = []
        for point in (entry.get("points") or [])[:16]:
            try:
                x, y, label = float(point[0]), float(point[1]), int(point[2])
            except (TypeError, ValueError, IndexError):
                continue
            if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 and label in (-1, 1):
                points.append([x, y, label])
        box = None
        if entry.get("box"):
            box = coords_module.normalize_box(entry["box"])
        if points or box:
            prompts.append({"frame_idx": frame_idx, "points": points, "box": box})
    if not prompts:
        return [], "укажите объект точкой или прямоугольником"
    return prompts, ""


def estimate_seconds(num_frames: int, sec_per_frame: float) -> float:
    return max(1.0, float(num_frames) * max(0.1, float(sec_per_frame or 0)))


def span_frame_count(span: dict[str, Any], src_fps: float, analysis_fps: float) -> int:
    start = max(0, int(span.get("start_frame", 0)))
    end = max(start, int(span.get("end_frame", start)))
    ratio = min(1.0, float(analysis_fps or 6.0) / max(1.0, float(src_fps or 30.0)))
    return max(1, int(round((end - start + 1) * ratio)))


def run_worker(session_dir: Path, task: dict[str, Any],
               on_line: Optional[Callable[[dict[str, Any]], None]] = None,
               timeout: Optional[float] = None) -> dict[str, Any]:
    """Spawn the isolated worker synchronously; stream JSON lines to *on_line*.

    Registers the process for cancellation.  MPS matmul has a hard abort bug
    with SAM 2 memory in bfloat16, so an abnormal MPS death is retried once
    on CPU automatically (reported via a ``device_fallback`` event).
    Returns the terminal summary.
    """
    summary = _run_worker_once(session_dir, task, on_line, timeout)
    if (summary.get("type") not in {"done", "cancelled"}
            and str(task.get("device") or "auto") in {"auto", "mps"}):
        retry = dict(task)
        retry["device"] = "cpu"
        if on_line is not None:
            try:
                on_line({"type": "device_fallback", "from": "mps", "to": "cpu"})
            except Exception:
                pass
        summary = _run_worker_once(session_dir, retry, on_line, timeout)
    if summary.get("type") == "done" and task.get("sec_per_frame_guess"):
        try:
            elapsed = float(summary.get("elapsed") or 0)
            done = max(1, int(summary.get("done") or 1))
            recalibrated = max(0.2, elapsed / done)
            selection = storage_module.read_selection(session_dir) or {}
            selection["sec_per_frame"] = round(recalibrated, 2)
            storage_module.write_selection(session_dir, selection)
        except Exception:
            pass
    return summary


def _run_worker_once(session_dir: Path, task: dict[str, Any],
                     on_line: Optional[Callable[[dict[str, Any]], None]] = None,
                     timeout: Optional[float] = None) -> dict[str, Any]:
    session_dir = Path(session_dir)
    files = storage_module.ensure_session(session_dir)
    task_path = files["logs"] / f"task_{int(time.time())}.json"
    task_path.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
    env = dict(os.environ)
    env.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    proc = subprocess.Popen(
        _SAM_PREFIX + [str(SAM_PYTHON), "-u", str(WORKER_SCRIPT), str(session_dir), str(task_path)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", env=env)
    with _WORKER_LOCK:
        _WORKER_PROCS[str(session_dir)] = proc
    summary: dict[str, Any] = {"type": "error", "message": "no output"}
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            summary = event
            if on_line is not None:
                try:
                    on_line(event)
                except Exception:
                    pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            summary = {"type": "error", "message": "worker timeout"}
    finally:
        with _WORKER_LOCK:
            _WORKER_PROCS.pop(str(session_dir), None)
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
    return summary


def cancel_worker(session_dir: Path) -> bool:
    """Request cancellation: flag file (worker polls) + hard kill fallback."""
    session_dir = Path(session_dir)
    try:
        (session_dir / "cancel.flag").write_text("cancel", encoding="utf-8")
    except OSError:
        pass
    with _WORKER_LOCK:
        proc = _WORKER_PROCS.get(str(session_dir))
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        return True
    return False


def mark_samples_suspicious(session_dir: Path) -> int:
    """Re-scan stored samples with geometric consistency; count suspicious."""
    samples = list(storage_module.iter_samples(session_dir))
    if not samples:
        return 0
    flagged = 0
    previous: Optional[dict[str, Any]] = None
    rewritten: list[dict[str, Any]] = []
    for sample in samples:
        if previous is not None:
            report = coords_module.check_consistency(previous, sample)
            if report["suspicious"]:
                sample = dict(sample)
                sample["suspicious"] = True
                sample["suspicion_reasons"] = report["reasons"]
                flagged += 1
        rewritten.append(sample)
        if sample.get("visible"):
            previous = sample
    if flagged:
        storage_module.clear_tracking(session_dir)
        for sample in rewritten:
            storage_module.append_sample(session_dir, sample)
    return flagged


def confirm_session(session_dir: Path) -> tuple[bool, str]:
    """Gate: only a completed, non-empty trajectory may be used for montage."""
    selection = storage_module.read_selection(session_dir) or {}
    samples = [s for s in storage_module.iter_samples(session_dir) if s.get("visible")]
    if not samples:
        return False, "нет завершённого анализа: запустите отслеживание"
    if selection.get("status") not in {"tracked", "corrected"}:
        return False, "подтвердите объект и завершите анализ"
    selection["status"] = "confirmed"
    storage_module.write_selection(session_dir, selection)
    return True, ""


def session_summary(session_dir: Path) -> dict[str, Any]:
    selection = storage_module.read_selection(session_dir) or {}
    samples = list(storage_module.iter_samples(session_dir))
    visible = [s for s in samples if s.get("visible")]
    suspicious = [s for s in samples if s.get("suspicious")]
    return {
        "status": selection.get("status", "selecting"),
        "samples": len(samples),
        "visible": len(visible),
        "suspicious": len(suspicious),
        "sec_per_frame": selection.get("sec_per_frame", DEFAULT_SEC_PER_FRAME),
    }


__all__ = [
    "SAM_VENV", "SAM_PYTHON", "SAM_CHECKPOINT", "SAM_MODEL_CFG",
    "DEFAULT_SEC_PER_FRAME", "SESSION_TTL_SECONDS",
    "sessions_root", "sam_status", "allowed_source", "create_session",
    "get_session", "validate_prompts", "estimate_seconds", "span_frame_count",
    "run_worker", "cancel_worker", "mark_samples_suspicious",
    "confirm_session", "session_summary",
]
