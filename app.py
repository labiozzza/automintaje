import os, json, uuid, asyncio, subprocess, time, re, tempfile, hashlib, urllib.request, importlib, threading, gc, math, io, sys, secrets, sqlite3, ipaddress
import shutil
import traceback
import urllib.parse
from pathlib import Path
from typing import Optional, Any
from datetime import datetime
from fractions import Fraction

from fastapi import FastAPI, UploadFile, File, Form, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

import dev_queue
import publication_queue
import publication_queue

import whisper
from pyannote.audio import Pipeline as DiarizationPipeline
import torch
import soundfile as sf
import numpy as np
import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

DEVICE = "cpu"

app = FastAPI(title="Transcriber API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

# In-memory job storage
jobs = {}
job_queues = {}
PUBLICATION_QUEUE_WAKE: Optional[asyncio.Event] = None
PUBLICATION_QUEUE_TASK: Optional[asyncio.Task] = None
PUBLICATION_QUEUE_LOOP: Optional[asyncio.AbstractEventLoop] = None

# Per-job publish locks (single-process uvicorn limited). All job state lives
# in memory, so this app is designed for exactly one server process; running
# uvicorn with --workers > 1 (or any multi-process deployment) would require a
# cross-process lock (e.g. a DB row / Redis / file lock) instead of these.
publish_locks: dict[str, asyncio.Lock] = {}


def _publish_lock(job_id: str) -> asyncio.Lock:
    lock = publish_locks.get(job_id)
    if lock is None:
        lock = asyncio.Lock()
        publish_locks[job_id] = lock
    return lock
whisper_models = {}
WHISPER_PROGRESS_LOCK = threading.Lock()
WHISPER_JOB_LOCK = threading.RLock()
DIARIZATION_JOB_LOCK = threading.RLock()
MODEL_IDLE_SECONDS = 300
whisper_last_used = 0.0
diarization_last_used = 0.0
whisper_idle_timer = None
diarization_idle_timer = None
diarization_pipeline = None
PROJECTS = {}
PROJECTS_LOCK = asyncio.Lock()
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".3gp", ".ts"}
TRANSCRIPTION_STATE_DIR = BASE_DIR / "transcription_jobs"
TRANSCRIPTION_STATE_DIR.mkdir(exist_ok=True)
PROJECTS_PATH = BASE_DIR / "projects.json"


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(temporary, path)


def _save_transcription_job(job_id: str) -> None:
    job = jobs.get(job_id)
    if not isinstance(job, dict) or job.get("kind") == "montage":
        return
    _write_json_atomic(TRANSCRIPTION_STATE_DIR / f"{job_id}.json", job)


def _save_projects() -> None:
    _write_json_atomic(PROJECTS_PATH, {"projects": list(PROJECTS.values())})


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_whisper_model(root: str, name: str, progress_cb=None) -> Optional[str]:
    """Докачиваемое (resume) скачивание модели whisper с прогрессом.
    Возвращает путь к локальному файлу модели либо None."""
    url = whisper._MODELS.get(name)
    if url is None:
        return None
    expected = url.split("/")[-2]
    target = os.path.join(root, name + ".pt")
    part = target + ".part"

    if os.path.isfile(target):
        if _file_sha256(target) == expected:
            return target
        os.replace(target, part)

    start = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"User-Agent": "Mozilla/5.0 (whisper-mirror-downloader)"}
    if start:
        headers["Range"] = f"bytes={start}-"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp, open(part, "ab") as out:
        total = start + int(resp.headers.get("Content-Length") or 0)
        downloaded = start
        last_pct = -1
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            downloaded += len(chunk)
            if total > 0:
                pct = int(downloaded * 100 / total)
            else:
                pct = int(downloaded * 100 / (start or 1))
            if pct != last_pct and progress_cb:
                last_pct = pct
                progress_cb(pct, downloaded, total)
        if progress_cb:
            progress_cb(100, downloaded, total)
    if _file_sha256(part) != expected:
        raise RuntimeError(f"Скачивание модели {name}: контрольная сумма не совпала")
    os.replace(part, target)
    return target


def load_whisper_model(name: str, progress_cb=None, load_cb=None):
    if name not in whisper_models:
        if whisper_models:
            loaded = ", ".join(whisper_models)
            print(f"[transcriber] Unloading Whisper model(s): {loaded}")
            whisper_models.clear()
            gc.collect()
        print(f"[transcriber] Checking whisper model '{name}'...")
        root = os.path.expanduser("~/.cache/whisper")
        path = ensure_whisper_model(root, name, progress_cb)
        if load_cb:
            load_cb()
        print(f"[transcriber] Loading whisper model '{name}'...")
        whisper_models[name] = whisper.load_model(path or name, device=DEVICE)
        print(f"[transcriber] Whisper '{name}' loaded on {DEVICE}")
    return whisper_models[name]


def run_whisper_job(name, audio_path, model_progress, model_load_start, transcription_start, transcription_progress, **kwargs):
    global whisper_last_used
    with WHISPER_JOB_LOCK:
        _cancel_idle_timer("whisper")
        model = load_whisper_model(name, model_progress, model_load_start)
        transcription_start()
        try:
            return transcribe_with_progress(model, audio_path, transcription_progress, **kwargs)
        finally:
            whisper_last_used = time.monotonic()
            _schedule_idle_unload("whisper")


def unload_whisper_models() -> None:
    global whisper_last_used
    with WHISPER_JOB_LOCK:
        _cancel_idle_timer("whisper")
        if whisper_models:
            loaded = ", ".join(whisper_models)
            print(f"[transcriber] Unloading Whisper model(s): {loaded}")
            whisper_models.clear()
            gc.collect()
        whisper_last_used = 0.0


def unload_diarization_pipeline() -> None:
    global diarization_pipeline, diarization_last_used
    with DIARIZATION_JOB_LOCK:
        _cancel_idle_timer("diarization")
        if diarization_pipeline is not None:
            print("[transcriber] Unloading pyannote diarization pipeline")
            diarization_pipeline = None
            gc.collect()
        diarization_last_used = 0.0


def _cancel_idle_timer(kind: str) -> None:
    global whisper_idle_timer, diarization_idle_timer
    timer = whisper_idle_timer if kind == "whisper" else diarization_idle_timer
    if timer is not None:
        timer.cancel()
    if kind == "whisper":
        whisper_idle_timer = None
    else:
        diarization_idle_timer = None


def _schedule_idle_unload(kind: str, delay: float = MODEL_IDLE_SECONDS) -> None:
    global whisper_idle_timer, diarization_idle_timer
    _cancel_idle_timer(kind)
    timer = threading.Timer(delay, _unload_if_idle, args=(kind,))
    timer.daemon = True
    if kind == "whisper":
        whisper_idle_timer = timer
    else:
        diarization_idle_timer = timer
    timer.start()


def _unload_if_idle(kind: str) -> None:
    lock = WHISPER_JOB_LOCK if kind == "whisper" else DIARIZATION_JOB_LOCK
    with lock:
        last_used = whisper_last_used if kind == "whisper" else diarization_last_used
        remaining = MODEL_IDLE_SECONDS - (time.monotonic() - last_used)
        if last_used and remaining > 0:
            _schedule_idle_unload(kind, remaining)
            return
        if kind == "whisper":
            unload_whisper_models()
        else:
            unload_diarization_pipeline()


def run_diarization_job(audio_path: str, kwargs: dict, start_cb):
    global diarization_last_used
    with DIARIZATION_JOB_LOCK:
        _cancel_idle_timer("diarization")
        pipeline = load_diarization_pipeline()
        start_cb()
        try:
            return pipeline(audio_path, **kwargs)
        finally:
            diarization_last_used = time.monotonic()
            _schedule_idle_unload("diarization")


def transcribe_with_progress(model, audio_path: str, progress_cb, **kwargs):
    """Expose Whisper's internal audio-frame progress without changing Whisper itself."""
    transcribe_module = importlib.import_module("whisper.transcribe")
    original_tqdm = transcribe_module.tqdm.tqdm

    class ProgressTqdm(original_tqdm):
        def update(self, amount=1):
            result = super().update(amount)
            total = float(self.total or 0)
            if total:
                progress_cb(min(1.0, float(self.n) / total))
            return result

    with WHISPER_PROGRESS_LOCK:
        transcribe_module.tqdm.tqdm = ProgressTqdm
        try:
            return model.transcribe(audio_path, verbose=False, **kwargs)
        finally:
            transcribe_module.tqdm.tqdm = original_tqdm


def load_diarization_pipeline():
    global diarization_pipeline
    if diarization_pipeline is None:
        load_secrets_env()
        print("[transcriber] Loading pyannote/speaker-diarization-3.1...")
        diarization_pipeline = DiarizationPipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1", use_auth_token=os.environ.get("HF_TOKEN")
        )
        diarization_pipeline.to(torch.device(DEVICE))
        print(f"[transcriber] Diarization pipeline loaded on {DEVICE}")
    return diarization_pipeline


def convert_to_wav(input_path: str, output_path: str) -> bool:
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return result.returncode == 0


def fmt_srt_time(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int((t - int(t)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def fmt_vtt_time(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int((t - int(t)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


async def broadcast_progress(job_id: str, progress: float, stage: str, detail: str = ""):
    job = jobs.get(job_id)
    if job:
        job["progress"] = progress
        job["stage"] = stage
        job["detail"] = detail
        q = job_queues.get(job_id)
        if q:
            try:
                await q.put({
                    "type": "progress",
                    "progress": progress,
                    "stage": stage,
                    "detail": detail,
                })
            except Exception:
                pass


async def broadcast_operation(job_id: str, snapshot: dict):
    """Push a normalised operation snapshot (see montage/progress_tracker.py)."""
    job = jobs.get(job_id)
    if job:
        job["operation"] = snapshot
        # Persist only meaningful transitions (status / active stage / stage
        # status changes) so a server restart can restore the last known state
        # without writing to disk on every progress tick (spec 4.3).
        signature = _operation_signature(snapshot)
        if _OPERATION_SAVE_SIGNATURES.get(job_id) != signature:
            _OPERATION_SAVE_SIGNATURES[job_id] = signature
            try:
                _save_montage_job(job_id)
            except Exception:
                pass
        q = job_queues.get(job_id)
        if q:
            try:
                await q.put({"type": "operation", "operation": snapshot})
            except Exception:
                pass


def _operation_signature(snapshot: dict) -> tuple:
    stages = snapshot.get("stages") or []
    return (
        snapshot.get("status"),
        snapshot.get("active_stage"),
        tuple((s.get("key"), s.get("status")) for s in stages),
    )


_OPERATION_SAVE_SIGNATURES: dict[str, tuple] = {}


def mark_operation_failed(job_id: str, error: str) -> Optional[dict]:
    """Record a coherent failed operation state for *job_id* (spec 1.2)."""
    job = jobs.get(job_id)
    if not job:
        return None
    snap = failed_snapshot(job.get("operation"), error)
    job["operation"] = snap
    _OPERATION_SAVE_SIGNATURES[job_id] = _operation_signature(snap)
    return snap


async def process_job(job_id: str, params: dict):
    job = jobs[job_id]
    try:
        job["status"] = "processing"
        wav_path = job["wav_path"]
        original_name = job["original_name"]

        # Stage 1: Conversion
        await broadcast_progress(job_id, 5, "conversion", "Конвертация в WAV...")
        wav_output = str(UPLOAD_DIR / f"{job_id}.wav")
        is_wav = wav_path.lower().endswith(".wav")
        if is_wav:
            wav_output = wav_path
            await broadcast_progress(job_id, 15, "conversion", "Файл уже в формате WAV")
        else:
            success = await asyncio.to_thread(convert_to_wav, wav_path, wav_output)
            if not success:
                job["status"] = "error"
                job["error"] = "Ошибка конвертации файла"
                await broadcast_progress(job_id, 0, "error", "Ошибка конвертации")
                _save_transcription_job(job_id)
                return

        await broadcast_progress(job_id, 15, "conversion", "Конвертация завершена")

        # Stage 2: Transcription
        model_name = params.get("model", "small")
        language = params.get("language", "ru")
        mode = params.get("mode", "words")

        loop = asyncio.get_running_loop()

        def model_progress(pct, downloaded, total):
            mb = downloaded / (1 << 20)
            total_mb = total / (1 << 20)
            asyncio.run_coroutine_threadsafe(
                broadcast_progress(
                    job_id, 20 + pct * 0.28, "model",
                    f"Скачивание модели {model_name}: {mb:.0f} / {total_mb:.0f} MB ({pct}%)"
                ),
                loop
            )

        def model_load_start():
            asyncio.run_coroutine_threadsafe(
                broadcast_progress(
                    job_id, 48, "model",
                    f"Загрузка модели {model_name} в память..."
                ),
                loop
            )

        await broadcast_progress(job_id, 20, "model", f"Проверка модели {model_name}...")
        transcription_span = 12 if params.get("diarization", False) else 38

        def transcription_start():
            asyncio.run_coroutine_threadsafe(
                broadcast_progress(job_id, 50, "transcription", "Транскрибация аудио..."),
                loop,
            )

        def transcription_progress(ratio: float):
            pct = 50 + ratio * transcription_span
            asyncio.run_coroutine_threadsafe(
                broadcast_progress(job_id, pct, "transcription", f"Транскрибация: {ratio * 100:.1f}%"),
                loop,
            )

        result = await asyncio.to_thread(
            run_whisper_job, model_name, wav_output, model_progress, model_load_start,
            transcription_start, transcription_progress,
            language=language, word_timestamps=True
        )
        segments = result["segments"]

        transcription_done = 63 if params.get("diarization", False) else 89
        await broadcast_progress(job_id, transcription_done, "transcription", f"Транскрибация: {len(segments)} сегментов")

        # Stage 3: Diarization
        do_diarization = params.get("diarization", False)
        num_speakers = params.get("num_speakers", None)
        diarization = None

        if do_diarization:
            await broadcast_progress(job_id, 65, "diarization", "Загрузка модели диаризации...")
            diarization_kwargs = {}
            if num_speakers:
                diarization_kwargs["num_speakers"] = int(num_speakers)

            def diarization_start():
                asyncio.run_coroutine_threadsafe(
                    broadcast_progress(job_id, 70, "diarization", "Диаризация..."),
                    loop,
                )

            diarization = await asyncio.to_thread(
                run_diarization_job, wav_output, diarization_kwargs, diarization_start
            )
            await broadcast_progress(job_id, 85, "diarization", "Диаризация завершена")

        # Stage 4: Merge
        await broadcast_progress(job_id, 90, "merge", "Объединение результатов...")

        merged = await asyncio.to_thread(merge_results, segments, diarization, mode)
        job["result"] = merged
        job["status"] = "done"
        job["progress"] = 100

        await broadcast_progress(job_id, 100, "done", "Готово!")
        _save_transcription_job(job_id)
        if await register_video_project(job_id):
            await broadcast_progress(job_id, 100, "done",
                                     "Готово! Ролик добавлен в проекты (Автомонтаж).")

    except Exception as e:
        import traceback
        job["status"] = "error"
        job["error"] = str(e)
        job["traceback"] = traceback.format_exc()
        await broadcast_progress(job_id, 0, "error", str(e))
        _save_transcription_job(job_id)


def merge_results(segments, diarization, mode):
    result_segments = []

    if diarization is not None:
        for seg in segments:
            start, end = seg["start"], seg["end"]

            if mode == "words" and "words" in seg:
                word_groups = []
                current_group = {"words": [], "word_timestamps": [], "speaker": None, "start": start, "end": start}

                for word_info in seg["words"]:
                    w_start = word_info["start"]
                    w_end = word_info["end"]
                    w_text = word_info["word"]

                    speaker = find_speaker(w_start, w_end, diarization)

                    if speaker != current_group["speaker"] and current_group["words"]:
                        current_group["end"] = w_start
                        current_group["text"] = " ".join(current_group["words"])
                        word_groups.append(current_group)
                        current_group = {"words": [], "word_timestamps": [], "speaker": speaker, "start": w_start, "end": w_start}

                    current_group["words"].append(w_text.strip())
                    current_group["word_timestamps"].append(word_info)
                    current_group["end"] = w_end

                if current_group["words"]:
                    current_group["text"] = " ".join(current_group["words"])
                    word_groups.append(current_group)

                result_segments.extend(word_groups)
            else:
                speaker = find_speaker(start, end, diarization)
                result_segments.append({
                    "start": start,
                    "end": end,
                    "text": seg["text"].strip(),
                    "speaker": speaker,
                    "words": seg.get("words") or [],
                })
    else:
        for seg in segments:
            if mode == "words" and "words" in seg:
                for wi in seg["words"]:
                    result_segments.append({
                        "start": wi["start"],
                        "end": wi["end"],
                        "text": wi["word"].strip(),
                        "speaker": None,
                    })
            else:
                result_segments.append({
                    "start": seg["start"],
                    "end": seg["end"],
                    "text": seg["text"].strip(),
                    "speaker": None,
                    "words": seg.get("words") or [],
                })

    return result_segments


def find_speaker(start, end, diarization):
    best_speaker = "SPEAKER_00"
    best_overlap = 0
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        overlap_start = max(start, turn.start)
        overlap_end = min(end, turn.end)
        overlap = max(0, overlap_end - overlap_start)
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = speaker
    return best_speaker


def export_srt(segments, show_speakers=True, show_timecodes=True):
    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        if show_timecodes:
            lines.append(f"{fmt_srt_time(seg['start'])} --> {fmt_srt_time(seg['end'])}")
        speaker = f"[{seg['speaker']}] " if show_speakers and seg.get('speaker') else ""
        lines.append(f"{speaker}{seg['text']}")
        lines.append("")
    return "\n".join(lines)


def export_vtt(segments, show_speakers=True, show_timecodes=True):
    lines = ["WEBVTT", ""]
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        if show_timecodes:
            lines.append(f"{fmt_vtt_time(seg['start'])} --> {fmt_vtt_time(seg['end'])}")
        speaker = f"[{seg['speaker']}] " if show_speakers and seg.get('speaker') else ""
        lines.append(f"{speaker}{seg['text']}")
        lines.append("")
    return "\n".join(lines)


def export_txt(segments, show_speakers=True):
    lines = []
    for seg in segments:
        speaker = f"[{seg['speaker']}] " if show_speakers and seg.get('speaker') else ""
        lines.append(f"{speaker}{seg['text']}")
    return "\n".join(lines)


def export_dialogue(segments, show_timecodes=True):
    lines = []
    current_speaker = None
    for seg in segments:
        sp = seg.get('speaker')
        if sp and sp != current_speaker:
            current_speaker = sp
            lines.append("")
            lines.append(f"═══ {sp} ═══")
        tc = f"[{fmt_srt_time(seg['start'])}] " if show_timecodes else ""
        lines.append(f"{tc}{seg['text']}")
    return "\n".join(lines).strip()


def export_json(segments, show_timecodes=True):
    data = []
    for seg in segments:
        entry = {"text": seg["text"]}
        if seg.get("speaker"):
            entry["speaker"] = seg["speaker"]
        if show_timecodes:
            entry["start"] = seg["start"]
            entry["end"] = seg["end"]
        data.append(entry)
    return json.dumps(data, ensure_ascii=False, indent=2)


def export_tsv(segments, show_speakers=True, show_timecodes=True):
    lines = []
    headers = []
    if show_timecodes:
        headers.extend(["start", "end"])
    if show_speakers:
        headers.append("speaker")
    headers.append("text")
    lines.append("\t".join(headers))

    for seg in segments:
        row = []
        if show_timecodes:
            row.extend([str(seg['start']), str(seg['end'])])
        if show_speakers:
            row.append(seg.get('speaker') or '')
        row.append(seg['text'])
        lines.append("\t".join(row))
    return "\n".join(lines)


# ========== ROUTES ==========

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(
        (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/manifest.json")
async def manifest():
    return FileResponse(path=str(BASE_DIR / "static" / "manifest.json"), media_type="application/manifest+json")


@app.get("/service-worker.js")
async def service_worker():
    return FileResponse(
        path=str(BASE_DIR / "static" / "sw.js"), media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    job_id = str(uuid.uuid4())[:8]
    ext = Path(file.filename or "file").suffix or ".wav"
    file_path = UPLOAD_DIR / f"{job_id}{ext}"

    content = await file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    jobs[job_id] = {
        "id": job_id,
        "original_name": file.filename,
        "wav_path": str(file_path),
        "upload_path": str(file_path),
        "status": "uploaded",
        "progress": 0,
        "stage": "",
        "detail": "",
        "result": None,
        "error": None,
        "created_at": datetime.now().isoformat(),
    }
    _save_transcription_job(job_id)

    return {"job_id": job_id, "filename": file.filename}


async def register_video_project(job_id: str) -> Optional[str]:
    job = jobs.get(job_id)
    if not job:
        return None
    name = str(job.get("original_name") or "")
    ext = Path(name).suffix.lower()
    src = job.get("upload_path") or job.get("wav_path") or ""
    if ext not in VIDEO_EXTS or not src or not Path(src).exists():
        return None
    if job.get("status") != "done" or not job.get("result"):
        return None
    pid = "p_" + job_id
    async with PROJECTS_LOCK:
        PROJECTS[pid] = {
            "id": pid,
            "name": name,
            "video_path": str(Path(src).resolve()),
            "transcript_job_id": job_id,
            "status": "ready",
            "created_at": job.get("created_at"),
        }
        _save_projects()
    return pid


# ---------------- Thumbs & Video preview ----------------

def _video_response(path: Path, request: Request, media_type: str):
    total = path.stat().st_size
    range_header = request.headers.get("range", "")
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    if not range_header:
        return FileResponse(str(path), media_type=media_type, filename=path.name, headers=headers)

    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match or (not match.group(1) and not match.group(2)):
        raise HTTPException(416, "Invalid byte range", headers={"Content-Range": f"bytes */{total}"})
    if match.group(1):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else total - 1
    else:
        suffix_size = int(match.group(2))
        start = max(0, total - suffix_size)
        end = total - 1
    if start >= total or start > end:
        raise HTTPException(416, "Invalid byte range", headers={"Content-Range": f"bytes */{total}"})
    end = min(end, total - 1)
    length = end - start + 1

    def stream():
        with path.open("rb") as source:
            source.seek(start)
            remaining = length
            while remaining > 0:
                chunk = source.read(min(1 << 20, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers.update({
        "Content-Range": f"bytes {start}-{end}/{total}",
        "Content-Length": str(length),
        "Content-Disposition": f'inline; filename="{path.name}"',
    })
    return StreamingResponse(stream(), status_code=206, media_type=media_type, headers=headers)

def _thumb_path(job_id: str) -> Path:
    return UPLOAD_DIR / f"{job_id}.thumb.jpg"


def _generate_thumb(video_path: Path, out_path: Path) -> bool:
    try:
        if not video_path.exists() or out_path.exists():
            return out_path.exists()
        subprocess.run(
            [shutil.which("ffmpeg") or "ffmpeg", "-y", "-i", str(video_path),
             "-vframes", "1", "-vf", "scale=405:-1", "-q:v", "3",
             str(out_path)],
            capture_output=True, timeout=30,
        )
        return out_path.exists() and out_path.stat().st_size > 100
    except Exception:
        return False


@app.get("/api/thumb/{job_id}")
async def serve_thumb(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    video_src = job.get("upload_path") or job.get("video_path") or ""
    if not video_src or not Path(video_src).exists():
        raise HTTPException(404, "Video not available")
    thumb = _thumb_path(job_id)
    if not thumb.exists():
        ok = await asyncio.to_thread(_generate_thumb, Path(video_src), thumb)
        if not ok:
            raise HTTPException(500, "Thumb generation failed")
    return FileResponse(str(thumb), media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})


@app.get("/api/video/{job_id}")
async def serve_video(job_id: str, request: Request):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    video_src = job.get("upload_path") or job.get("video_path") or ""
    video = Path(video_src)
    if not video.exists():
        raise HTTPException(404, "Video not available")
    media = {
        ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
        ".webm": "video/webm", ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
    }.get(video.suffix.lower(), "video/mp4")
    return _video_response(video, request, media)


@app.post("/api/projects/link")
async def link_transcript_to_project(
    transcript_job_id: str = Form(...),
):
    job = jobs.get(transcript_job_id)
    if not job:
        raise HTTPException(404, "Задание транскрибации не найдено")
    if job.get("status") != "done" or not job.get("result"):
        raise HTTPException(400, "Транскрибация ещё не завершена")
    async with PROJECTS_LOCK:
        existing = next(
            (p for p in PROJECTS.values()
             if p.get("transcript_job_id") == transcript_job_id), None)
    if existing:
        return {"project_id": existing["id"], "status": "linked"}
    pid = await register_video_project(transcript_job_id)
    if not pid:
        raise HTTPException(400, "Видео недоступно — проект не создан")
    return {"project_id": pid, "status": "created"}


@app.get("/api/projects")
async def list_projects():
    projects = []
    for pid, pr in PROJECTS.items():
        entry = dict(pr)
        trj = jobs.get(pr.get("transcript_job_id")) if pr.get("transcript_job_id") else None
        entry["transcript_status"] = trj.get("status") if trj else "missing"
        result = trj.get("result") if trj else None
        entry["segments"] = len(result) if isinstance(result, list) else 0
        entry["video_ok"] = bool(pr.get("video_path")) and Path(pr["video_path"]).exists()
        projects.append(entry)
    projects.sort(key=lambda p: p.get("created_at") or "", reverse=True)
    return {"projects": projects}


class DevelopmentQueueNoteInput(BaseModel):
    text: str = Field(..., min_length=1, max_length=dev_queue.MAX_NOTE_CHARS)


def _require_loopback_development_queue(request: Request) -> None:
    client = request.client
    if client is None:
        raise HTTPException(403, "Очередь разработки доступна только на этом компьютере")
    try:
        address = ipaddress.ip_address(client.host)
    except ValueError as exc:
        raise HTTPException(403, "Очередь разработки доступна только на этом компьютере") from exc
    if getattr(address, "ipv4_mapped", None) is not None:
        address = address.ipv4_mapped
    if not address.is_loopback:
        raise HTTPException(403, "Очередь разработки доступна только на этом компьютере")


def _development_queue_call(operation, *args):
    try:
        return operation(*args)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(503, "Хранилище очереди временно недоступно") from exc


@app.get("/api/development-queue")
def list_development_queue(request: Request):
    _require_loopback_development_queue(request)
    return {"notes": _development_queue_call(dev_queue.list_notes)}


@app.post("/api/development-queue", status_code=201)
def create_development_queue_note(payload: DevelopmentQueueNoteInput, request: Request):
    _require_loopback_development_queue(request)
    return {"note": _development_queue_call(dev_queue.create_note, payload.text)}


@app.patch("/api/development-queue/{note_id}")
def update_development_queue_note(
    note_id: str, payload: DevelopmentQueueNoteInput, request: Request
):
    _require_loopback_development_queue(request)
    note = _development_queue_call(dev_queue.update_note, note_id, payload.text)
    if note is None:
        raise HTTPException(404, "Заметка не найдена")
    return {"note": note}


@app.delete("/api/development-queue/{note_id}")
def delete_development_queue_note(note_id: str, request: Request):
    _require_loopback_development_queue(request)
    deleted = _development_queue_call(dev_queue.delete_note, note_id)
    if not deleted:
        raise HTTPException(404, "Заметка не найдена")
    return {"ok": True}


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str):
    async with PROJECTS_LOCK:
        if project_id in PROJECTS:
            del PROJECTS[project_id]
            _save_projects()
    return {"ok": True}


@app.post("/api/transcribe/{job_id}")
async def start_transcription(
    job_id: str,
    model: str = Form("small"),
    language: str = Form("ru"),
    mode: str = Form("words"),
    diarization: bool = Form(False),
    num_speakers: Optional[int] = Form(None),
):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")

    params = {
        "model": model,
        "language": language,
        "mode": mode,
        "diarization": diarization,
        "num_speakers": num_speakers,
    }
    jobs[job_id]["params"] = params
    jobs[job_id]["status"] = "processing"
    _save_transcription_job(job_id)

    asyncio.create_task(process_job(job_id, params))
    return {"job_id": job_id, "status": "processing"}


@app.get("/api/status/{job_id}")
async def get_status(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    return {
        "id": job["id"],
        "status": job["status"],
        "progress": job["progress"],
        "stage": job["stage"],
        "detail": job["detail"],
        "error": job.get("error"),
    }


@app.get("/api/result/{job_id}")
async def get_result(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    if job["status"] != "done":
        raise HTTPException(400, "Job not completed")
    return {"segments": job["result"], "original_name": job["original_name"]}


@app.patch("/api/result/{job_id}")
async def save_result(job_id: str, request: Request):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.get("status") != "done" or not isinstance(job.get("result"), list):
        raise HTTPException(400, "Job not completed")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Некорректный JSON")
    texts = payload.get("texts") if isinstance(payload, dict) else None
    if not isinstance(texts, list) or len(texts) != len(job["result"]):
        raise HTTPException(400, "Количество сегментов не совпадает с транскрипцией")
    if any(not isinstance(text, str) for text in texts):
        raise HTTPException(400, "Текст каждого сегмента должен быть строкой")

    normalized = [text.replace("\r\n", "\n").replace("\r", "\n").strip() for text in texts]
    if any("\x00" in text or len(text) > 20_000 for text in normalized):
        raise HTTPException(400, "Один из сегментов содержит недопустимый текст")
    if sum(len(text) for text in normalized) > 1_000_000:
        raise HTTPException(400, "Транскрипция слишком большая")
    if not any(normalized):
        raise HTTPException(400, "Транскрипция не может быть пустой")

    job["result"] = [
        {**segment, "text": text}
        for segment, text in zip(job["result"], normalized)
    ]
    job["transcript_updated_at"] = datetime.now().isoformat()
    _save_transcription_job(job_id)
    return {"segments": job["result"], "saved": True}


@app.get("/api/export/{job_id}/{format_name}")
async def export_file(
    job_id: str,
    format_name: str,
    speakers: bool = True,
    timecodes: bool = True,
):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    if job["status"] != "done":
        raise HTTPException(400, "Job not completed")

    segments = job["result"]
    original = Path(job["original_name"]).stem

    exporters = {
        "srt": lambda: export_srt(segments, speakers, timecodes),
        "vtt": lambda: export_vtt(segments, speakers, timecodes),
        "txt": lambda: export_txt(segments, speakers),
        "json": lambda: export_json(segments, timecodes),
        "tsv": lambda: export_tsv(segments, speakers, timecodes),
        "dialogue": lambda: export_dialogue(segments, timecodes),
    }

    if format_name not in exporters:
        raise HTTPException(400, f"Unknown format: {format_name}")

    content = exporters[format_name]()
    ext = format_name if format_name != "dialogue" else "txt"
    media_type = "text/plain"
    if format_name == "json":
        media_type = "application/json"

    tmp_path = OUTPUT_DIR / f"{job_id}.{ext}"
    tmp_path.write_text(content, encoding="utf-8")

    return FileResponse(
        path=str(tmp_path),
        filename=f"{original}.{ext}",
        media_type=media_type,
    )


# ========== MONTAGE (Автомонтаж) ==========

MONTAGE_WORK = BASE_DIR / "montage_work"
MONTAGE_WORK.mkdir(exist_ok=True)

from montage.secrets_env import load_secrets_env
load_secrets_env()
from montage import engine as montage_engine
from montage.silence_trim import (
    BIN_MS as SILENCE_BIN_MS,
    MAX_CHANNELS as SILENCE_MAX_CHANNELS,
    MAX_CUTS as SILENCE_MAX_CUTS,
    MAX_DURATION_SECONDS as SILENCE_MAX_DURATION_SECONDS,
    PcmAccumulator,
    ceil_fraction,
    effective_frame_intervals,
    normalize_cut_list,
    parse_frame_rate,
    retained_frame_segments,
)
from montage.color_pipeline import probe_color_profile
from montage import cover_generator
from montage.progress_tracker import failed_snapshot, ProgressTracker, Stage
from montage.silence_trim import SAMPLE_RATE as SILENCE_SAMPLE_RATE

SILENCE_ANALYSIS_BASE_DIR = MONTAGE_WORK / ".silence_analysis"
SILENCE_PROCESS_NAMESPACE = f"{os.getpid()}-{uuid.uuid4().hex}"
SILENCE_ANALYSIS_DIR = SILENCE_ANALYSIS_BASE_DIR / SILENCE_PROCESS_NAMESPACE
SILENCE_ANALYSIS_TTL_SECONDS = 2 * 60 * 60
SILENCE_SOURCE_MAX_BYTES = 2 * 1024 * 1024 * 1024
SILENCE_ANALYSIS_MAX_SESSIONS = 2
SILENCE_WORKSPACE_HEADROOM_BYTES = 128 * 1024 * 1024
SILENCE_RESPONSE_MAX_BYTES = 8 * 1024 * 1024
SILENCE_WORKER_MEMORY_LIMIT_BYTES = 64 * 1024 * 1024
try:
    _silence_timeout_env = int(os.environ.get("AUTOMONTAGE_SILENCE_ANALYSIS_TIMEOUT_SECONDS", "1800"))
except (TypeError, ValueError):
    _silence_timeout_env = 1800
SILENCE_ANALYSIS_TIMEOUT_SECONDS = max(60, min(30 * 60, _silence_timeout_env))
_SILENCE_SESSIONS: dict[str, dict[str, Any]] = {}
_SILENCE_ANALYSIS_LOCK = asyncio.Lock()
_SILENCE_PROCESSES: dict[str, asyncio.subprocess.Process] = {}
_SILENCE_CLEANUP_TASK: Optional[asyncio.Task] = None
_SILENCE_PROGRESS: dict[str, dict[str, Any]] = {}


def _silence_request_id(request: Request) -> str:
    raw = str(request.headers.get("x-analysis-request") or "").strip()
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError):
        return uuid.uuid4().hex


def _record_silence_progress(
    request_id: str,
    *,
    status: str,
    phase: str,
    percent: Optional[float],
    detail: str,
    wait_seconds: Optional[int] = None,
) -> None:
    now = time.time()
    for key, value in list(_SILENCE_PROGRESS.items()):
        if now - float(value.get("updated_at", 0)) > SILENCE_ANALYSIS_TTL_SECONDS:
            _SILENCE_PROGRESS.pop(key, None)
    item = {
        "request_id": request_id,
        "status": status,
        "phase": phase,
        "percent": None if percent is None else max(0.0, min(100.0, float(percent))),
        "detail": str(detail or ""),
        "updated_at": now,
    }
    if wait_seconds is not None:
        item["wait_seconds"] = max(0, int(wait_seconds))
    _SILENCE_PROGRESS[request_id] = item


def _silence_problem(
    request: Request,
    status: int,
    code: str,
    detail: str,
    recovery: str,
    *,
    operation: str,
    context: Optional[dict[str, Any]] = None,
    side_effects: str = "none",
    headers: Optional[dict[str, str]] = None,
) -> JSONResponse:
    """Return a safe RFC 9457 response for synchronous silence endpoints."""
    request_progress_id = str(request.headers.get("x-analysis-request") or "").strip()
    try:
        request_progress_id = str(uuid.UUID(request_progress_id))
    except (ValueError, AttributeError):
        request_progress_id = ""
    request_id = request_progress_id or uuid.uuid4().hex
    if request_progress_id in _SILENCE_PROGRESS and _SILENCE_PROGRESS[request_progress_id].get("status") == "running":
        _record_silence_progress(
            request_progress_id, status="error", phase=operation, percent=None, detail=detail,
        )
    body = {
        "type": f"urn:automontage:silence:{code}",
        "title": code.replace("_", " ").title(),
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "code": code,
        "operation": operation,
        "context": context or {},
        "recovery": recovery,
        "side_effects": side_effects,
        "request_id": request_id,
    }
    return JSONResponse(status_code=status, content=body, media_type="application/problem+json", headers=headers)


def _silence_safe_filename(value: str) -> tuple[str, str]:
    decoded = urllib.parse.unquote(str(value or "video.mp4"))
    name = Path(decoded.replace("\\", "/")).name[:180] or "video.mp4"
    ext = Path(name).suffix.lower()
    if ext not in VIDEO_EXTS:
        ext = ".mp4"
        name = Path(name).stem[:160] + ext
    return name, ext


async def _expire_silence_sessions() -> None:
    now = time.time()
    async with _SILENCE_ANALYSIS_LOCK:
        expired = [
            analysis_id for analysis_id, session in _SILENCE_SESSIONS.items()
            if float(session.get("expires_at", 0)) <= now
        ]
        for analysis_id in expired:
            session = _SILENCE_SESSIONS.pop(analysis_id, None) or {}
            shutil.rmtree(str(session.get("directory") or ""), ignore_errors=True)
        SILENCE_ANALYSIS_BASE_DIR.mkdir(parents=True, exist_ok=True)
        SILENCE_ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
        live_dirs = {str(session.get("directory") or "") for session in _SILENCE_SESSIONS.values()}
        for directory in SILENCE_ANALYSIS_DIR.iterdir():
            if not directory.is_dir() or str(directory) in live_dirs:
                continue
            # Session metadata is intentionally in-memory; an orphan after
            # restart cannot be resumed safely and must not accumulate.
            shutil.rmtree(directory, ignore_errors=True)
        if SILENCE_ANALYSIS_BASE_DIR.resolve() != SILENCE_ANALYSIS_DIR.resolve():
            for worker_dir in SILENCE_ANALYSIS_BASE_DIR.iterdir():
                if not worker_dir.is_dir() or worker_dir.resolve() == SILENCE_ANALYSIS_DIR.resolve():
                    continue
                try:
                    owner_pid = int(worker_dir.name.split("-", 1)[0])
                except ValueError:
                    owner_pid = 0
                owner_alive = False
                if owner_pid > 0 and owner_pid != os.getpid():
                    try:
                        os.kill(owner_pid, 0)
                        owner_alive = True
                    except ProcessLookupError:
                        owner_alive = False
                    except PermissionError:
                        owner_alive = True
                if not owner_alive:
                    # Old un-namespaced sessions and dead workers cannot be
                    # resumed by this process and are safe to expire here.
                    shutil.rmtree(worker_dir, ignore_errors=True)


async def _reserve_silence_session(
    filename: str, extension: str, project_id: Optional[str], request_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    await _expire_silence_sessions()
    async with _SILENCE_ANALYSIS_LOCK:
        if len(_SILENCE_SESSIONS) >= SILENCE_ANALYSIS_MAX_SESSIONS:
            return None
        analysis_id = secrets.token_urlsafe(32)
        directory = SILENCE_ANALYSIS_DIR / uuid.uuid4().hex
        directory.mkdir(parents=True, exist_ok=False)
        session = {
            "analysis_id": analysis_id,
            "directory": str(directory),
            "source_path": str(directory / ("source" + extension)),
            "filename": filename,
            "project_id": project_id,
            "created_at": time.time(),
            "expires_at": time.time() + SILENCE_ANALYSIS_TTL_SECONDS,
            "status": "analyzing",
            "request_id": request_id,
        }
        _SILENCE_SESSIONS[analysis_id] = session
        return dict(session)


async def _discard_silence_session(analysis_id: str) -> None:
    async with _SILENCE_ANALYSIS_LOCK:
        session = _SILENCE_SESSIONS.pop(analysis_id, None)
    if session:
        shutil.rmtree(str(session.get("directory") or ""), ignore_errors=True)


def _free_space_for_silence_copy(bytes_needed: int) -> bool:
    try:
        return shutil.disk_usage(MONTAGE_WORK).free >= bytes_needed + SILENCE_WORKSPACE_HEADROOM_BYTES
    except OSError:
        return False


def _copy_project_video_snapshot(source: Path, target: Path, progress_callback=None) -> tuple[int, str]:
    source = source.resolve(strict=True)
    if not source.is_file():
        raise FileNotFoundError("project video unavailable")
    before = source.stat()
    if before.st_size > SILENCE_SOURCE_MAX_BYTES:
        raise OverflowError("source too large")
    if not _free_space_for_silence_copy(before.st_size):
        raise OSError("workspace capacity")
    digest = hashlib.sha256()
    copied = 0
    with source.open("rb") as input_file, target.open("xb") as output_file:
        while True:
            chunk = input_file.read(1024 * 1024)
            if not chunk:
                break
            copied += len(chunk)
            if copied > SILENCE_SOURCE_MAX_BYTES:
                raise OverflowError("source too large")
            digest.update(chunk)
            output_file.write(chunk)
            if progress_callback is not None:
                progress_callback(copied, before.st_size)
    after = source.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or copied != before.st_size:
        raise RuntimeError("source changed during snapshot")
    return copied, digest.hexdigest()


async def _copy_raw_upload(request: Request, target: Path, request_id: str) -> tuple[int, str]:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            announced = int(content_length)
        except ValueError:
            raise ValueError("invalid content length")
        if announced > SILENCE_SOURCE_MAX_BYTES:
            raise OverflowError("source too large")
        if not _free_space_for_silence_copy(announced):
            raise OSError("workspace capacity")
    digest = hashlib.sha256()
    total = 0
    announced = int(content_length) if content_length else 0
    _record_silence_progress(
        request_id, status="running", phase="receiving", percent=0.0 if announced else None,
        detail="Приём исходного видео",
    )
    with target.open("xb") as output_file:
        async for chunk in request.stream():
            if not chunk:
                continue
            total += len(chunk)
            if total > SILENCE_SOURCE_MAX_BYTES:
                raise OverflowError("source too large")
            if not _free_space_for_silence_copy(len(chunk)):
                raise OSError("workspace capacity")
            digest.update(chunk)
            output_file.write(chunk)
            _record_silence_progress(
                request_id, status="running", phase="receiving",
                percent=(min(99.0, total * 100.0 / announced) if announced else None),
                detail=(f"Загружено {total // (1024 * 1024)} МБ" if not announced else "Приём исходного видео"),
            )
    if total == 0:
        raise ValueError("empty upload")
    if content_length and total != int(content_length):
        raise ValueError("incomplete upload")
    return total, digest.hexdigest()


def _probe_silence_source(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError("source probe failed")
    data = json.loads(result.stdout)
    streams = data.get("streams") if isinstance(data.get("streams"), list) else []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    if not isinstance(video, dict):
        raise ValueError("no video stream")
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    raw_duration = (data.get("format") or {}).get("duration") or video.get("duration") or 0
    duration = float(raw_duration)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("invalid media duration")
    if duration > SILENCE_MAX_DURATION_SECONDS:
        raise OverflowError("source duration too long")
    try:
        frame_rate = parse_frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        fps = f"{frame_rate.numerator}/{frame_rate.denominator}"
    except ValueError:
        fps = "30/1"
    return {
        "duration_ms": int(round(duration * 1000.0)),
        "fps": fps,
        "audio": audio,
    }


def _silence_worker_rss_bytes(pid: int) -> Optional[int]:
    try:
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(int(pid))],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2,
        )
        value = int(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else 0
        return value * 1024 if value > 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


async def _decode_silence_audio(
    path: Path,
    channels: int,
    analysis_id: str,
    *,
    request_id: Optional[str] = None,
    source_duration_ms: int = 0,
) -> dict[str, Any]:
    command = [
        shutil.which("ffmpeg") or "ffmpeg", "-nostdin", "-v", "error",
        "-threads", "1", "-filter_threads", "1", "-i", str(path),
        "-map", "0:a:0", "-vn", "-c:a", "pcm_f32le", "-ar", str(SILENCE_SAMPLE_RATE),
        "-f", "f32le", "pipe:1",
    ]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _SILENCE_PROCESSES[analysis_id] = process
    accumulator = PcmAccumulator(channels)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SILENCE_ANALYSIS_TIMEOUT_SECONDS
    memory_limit_reached = asyncio.Event()
    last_progress_update = 0.0

    async def drain_stderr() -> bytes:
        captured = bytearray()
        while True:
            chunk = await process.stderr.read(8192)
            if not chunk:
                return bytes(captured)
            if len(captured) < 64 * 1024:
                captured.extend(chunk[:64 * 1024 - len(captured)])

    stderr_task = asyncio.create_task(drain_stderr())

    async def monitor_worker_memory() -> None:
        while process.returncode is None:
            rss = await asyncio.to_thread(_silence_worker_rss_bytes, process.pid)
            if rss is not None and rss > SILENCE_WORKER_MEMORY_LIMIT_BYTES:
                memory_limit_reached.set()
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                return
            await asyncio.sleep(0.5)

    memory_task = asyncio.create_task(monitor_worker_memory())
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            chunk = await asyncio.wait_for(process.stdout.read(64 * 1024), timeout=remaining)
            if not chunk:
                break
            accumulator.feed(chunk)
            if len(accumulator.envelope) > 720_000:
                raise OverflowError("analysis envelope too large")
            now = loop.time()
            if request_id and now - last_progress_update >= 0.2:
                decoded_ms = accumulator.total_samples * 1000.0 / (SILENCE_SAMPLE_RATE * channels)
                percent = min(99.0, decoded_ms * 100.0 / source_duration_ms) if source_duration_ms > 0 else None
                decoded_text = f"Обработано {decoded_ms / 1000.0:.1f} из {source_duration_ms / 1000.0:.1f} с" \
                    if source_duration_ms > 0 else "Декодирование аудио"
                _record_silence_progress(
                    request_id, status="running", phase="decoding", percent=percent, detail=decoded_text,
                )
                last_progress_update = now
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        code = await asyncio.wait_for(process.wait(), timeout=remaining)
        stderr = await stderr_task
        if memory_limit_reached.is_set():
            raise MemoryError("audio decoder exceeded its memory budget")
        if code != 0:
            print(f"[silence-analysis] ffmpeg failed for request {analysis_id[:8]}: {stderr.decode('utf-8', errors='replace')[-500:]}", file=sys.stderr)
            raise RuntimeError("audio decode failed")
        result = accumulator.finish()
        if len(result["envelope_dbfs_x100"]) > 720_000:
            raise OverflowError("analysis envelope too large")
        return result
    except BaseException:
        if process.returncode is None:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except Exception:
                pass
        if not stderr_task.done():
            stderr_task.cancel()
        raise
    finally:
        for task in (stderr_task, memory_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stderr_task, memory_task, return_exceptions=True)
        _SILENCE_PROCESSES.pop(analysis_id, None)


async def _analysis_session(analysis_id: str) -> Optional[dict[str, Any]]:
    await _expire_silence_sessions()
    async with _SILENCE_ANALYSIS_LOCK:
        session = _SILENCE_SESSIONS.get(str(analysis_id or ""))
        if not session or session.get("status") != "ready":
            return None
        return dict(session)


async def _silence_capacity_wait_seconds() -> int:
    async with _SILENCE_ANALYSIS_LOCK:
        expiries = [float(session.get("expires_at", 0)) for session in _SILENCE_SESSIONS.values()]
    if not expiries:
        return 1
    return max(1, int(math.ceil(min(expiries) - time.time())))


async def _handoff_silence_session(analysis_id: str, target_dir: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    async with _SILENCE_ANALYSIS_LOCK:
        session = _SILENCE_SESSIONS.get(str(analysis_id or ""))
        if not session or session.get("status") != "ready" or float(session.get("expires_at", 0)) <= time.time():
            return None, "analysis_expired"
        source = Path(str(session.get("source_path") or ""))
        if not source.is_file():
            print(f"[silence-handoff] snapshot missing for session {str(analysis_id)[:8]}", file=sys.stderr)
            return None, "snapshot_missing"
        actual_hash = await asyncio.to_thread(_file_sha256, str(source))
        expected_hash = str(session.get("source_sha256") or "")
        if actual_hash != expected_hash:
            print(
                f"[silence-handoff] snapshot hash mismatch for session {str(analysis_id)[:8]} "
                f"(expected {expected_hash[:12]}, got {actual_hash[:12]})",
                file=sys.stderr,
            )
            return None, "snapshot_hash_mismatch"
        target = target_dir / ("source" + source.suffix.lower())
        os.replace(str(source), str(target))
        handoff = dict(session)
        handoff["source_path"] = str(target)
        handoff["status"] = "consumed"
        _SILENCE_SESSIONS.pop(str(analysis_id), None)
        shutil.rmtree(str(session.get("directory") or ""), ignore_errors=True)
        return handoff, None


@app.on_event("startup")
async def _cleanup_orphaned_silence_analysis_sessions() -> None:
    """Analysis sessions are process-local; remove unresumable snapshots on restart."""
    global _SILENCE_CLEANUP_TASK
    await _expire_silence_sessions()
    if _SILENCE_CLEANUP_TASK is None or _SILENCE_CLEANUP_TASK.done():
        _SILENCE_CLEANUP_TASK = asyncio.create_task(_silence_cleanup_loop())


async def _silence_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(300)
        await _expire_silence_sessions()


@app.on_event("shutdown")
async def _stop_silence_cleanup_loop() -> None:
    global _SILENCE_CLEANUP_TASK
    task = _SILENCE_CLEANUP_TASK
    _SILENCE_CLEANUP_TASK = None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


def _uploaded_video(job_id: str) -> Optional[Path]:
    candidates = [
        path for path in UPLOAD_DIR.glob(f"{job_id}.*")
        if path.is_file() and path.suffix.lower() in VIDEO_EXTS
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _recover_transcription_projects() -> None:
    """Загружает постоянный каталог и один раз импортирует старые файлы."""
    for path in sorted(TRANSCRIPTION_STATE_DIR.glob("*.json")):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(job, dict) or not job.get("id"):
            continue
        if job.get("status") == "processing":
            job["status"] = "error"
            job["error"] = "Транскрибация была прервана перезапуском сервера"
            job["stage"] = "error"
            job["detail"] = job["error"]
            _write_json_atomic(path, job)
        jobs[str(job["id"])] = job

    montage_sources: dict[str, dict[str, Any]] = {}
    for path in sorted(MONTAGE_WORK.glob("*/job_state.json"), key=lambda item: item.stat().st_mtime):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        transcript_id = str(state.get("transcript_job_id") or "")
        if transcript_id and state.get("srt_text"):
            montage_sources[transcript_id] = state

    recovered_projects: dict[str, dict[str, Any]] = {}
    video_paths = sorted(
        (path for path in UPLOAD_DIR.iterdir() if path.is_file() and path.suffix.lower() in VIDEO_EXTS),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for video_path in video_paths:
        job_id = video_path.stem
        state = montage_sources.get(job_id, {})
        job = jobs.get(job_id)
        if not isinstance(job, dict):
            segments: list[dict[str, Any]] = []
            srt_text = str(state.get("srt_text") or "")
            output_srt = OUTPUT_DIR / f"{job_id}.srt"
            output_json = OUTPUT_DIR / f"{job_id}.json"
            if not srt_text and output_srt.is_file():
                srt_text = output_srt.read_text(encoding="utf-8", errors="replace")
            if output_json.is_file():
                try:
                    value = json.loads(output_json.read_text(encoding="utf-8"))
                    if isinstance(value, list):
                        segments = value
                except Exception:
                    pass
            created_at = datetime.fromtimestamp(video_path.stat().st_mtime).isoformat()
            original_name = str(state.get("original_name") or video_path.name)
            wav_path = UPLOAD_DIR / f"{job_id}.wav"
            job = {
                "id": job_id,
                "original_name": original_name,
                "wav_path": str(wav_path if wav_path.is_file() else video_path),
                "upload_path": str(video_path.resolve()),
                "status": "done" if segments else "uploaded",
                "progress": 100 if segments else 0,
                "stage": "done" if segments else "",
                "detail": "Восстановлено с диска" if segments else "Транскрипция не найдена",
                "result": segments or None,
                "error": None,
                "created_at": created_at,
                "recovered": True,
            }
            jobs[job_id] = job
            _save_transcription_job(job_id)
        recovered_projects[f"p_{job_id}"] = {
            "id": f"p_{job_id}",
            "name": str(job.get("original_name") or video_path.name),
            "video_path": str(video_path.resolve()),
            "transcript_job_id": job_id,
            "status": "ready" if job.get("status") == "done" else "transcript_missing",
            "created_at": job.get("created_at") or datetime.fromtimestamp(video_path.stat().st_mtime).isoformat(),
            "recovered": True,
        }

    catalog_exists = PROJECTS_PATH.is_file()
    if catalog_exists:
        try:
            payload = json.loads(PROJECTS_PATH.read_text(encoding="utf-8"))
            stored = payload.get("projects") if isinstance(payload, dict) else None
            if isinstance(stored, list):
                for project in stored:
                    if isinstance(project, dict) and project.get("id"):
                        PROJECTS[str(project["id"])] = project
        except Exception:
            catalog_exists = False
    if not catalog_exists:
        PROJECTS.update(recovered_projects)
        _save_projects()

app.mount("/animation-assets", StaticFiles(directory=str(montage_engine.ANIMATIONS_DIR)), name="animation-assets")
app.mount("/font-assets", StaticFiles(directory=str(montage_engine.FONTS_DIR)), name="font-assets")


_RENDER_CLEANUP_FILES = (
    "normalized_source.mp4",
    "mirrored_source.mp4",
    "zoom.mp4",
    "sub_sentences.mp4",
    "sub_words.mp4",
    "sub_phrases.mp4",
    "with_visual_layers.mp4",
    "with_animation.mp4",
    "censored_audio.mp4",
    "face_tracking.json",
    "zoom_timeline_config.json",
    "subtitle_timeline_config.json",
    "subtitle_timeline_config_words.json",
    "subtitle_timeline_config_phrases.json",
    "overlay_timeline_config.json",
)


def _safe_montage_workdir(job_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(job_id)):
        raise ValueError("Недопустимый идентификатор задания")
    root = MONTAGE_WORK.resolve()
    workdir = MONTAGE_WORK / str(job_id)
    if workdir.is_symlink() or workdir.resolve(strict=False).parent != root:
        raise ValueError("Каталог задания находится вне montage_work")
    return workdir


def _protected_montage_paths(job: dict, workdir: Path) -> set[Path]:
    protected = set()
    result = job.get("result") or {}
    # The master source is never released by the cleanup routines: publishing
    # on one platform must not destroy the source needed by other platforms
    # (spec B.2).  Its lifetime is governed solely by the master retention
    # policy (_retire_masters), and only for fully settled jobs.
    keys = ["result_video", "preview_video", "master_video"]
    for key in keys:
        raw = result.get(key)
        if not raw:
            continue
        path = Path(str(raw))
        if not path.is_absolute():
            path = workdir / path
        protected.add(Path(os.path.abspath(str(path))))
    return protected


def _cleanup_path_is_protected(path: Path, protected: set[Path], recursive: bool) -> bool:
    normalized = Path(os.path.abspath(str(path)))
    for item in protected:
        if item == normalized:
            return True
        if recursive:
            try:
                item.relative_to(normalized)
                return True
            except ValueError:
                pass
    return False


def _remove_cleanup_path(path: Path, recursive: bool) -> None:
    if path.is_symlink():
        path.unlink()
    elif recursive and path.is_dir():
        shutil.rmtree(path)
    elif not recursive and path.is_file():
        path.unlink()


def _write_cleanup_log(workdir: Path, summary: dict) -> None:
    line = json.dumps(
        {"timestamp": datetime.now().isoformat(), **summary},
        ensure_ascii=False,
        default=str,
    )
    try:
        with (workdir / "cleanup.log").open("a", encoding="utf-8") as log:
            log.write(line + "\n")
    except Exception as exc:
        print(f"[montage-cleanup] could not write log for {workdir.name}: {exc}", file=sys.stderr)
    try:
        print(f"[montage-cleanup] {workdir.name}: {line}")
    except Exception:
        pass


def _cleanup_allowed_paths(job_id: str, job: dict, phase: str, candidates: list[tuple[Path, bool]]) -> dict:
    try:
        workdir = _safe_montage_workdir(job_id)
    except Exception as exc:
        summary = {"phase": phase, "removed": [], "protected": [], "errors": [str(exc)]}
        print(f"[montage-cleanup] {job_id}: {json.dumps(summary, ensure_ascii=False)}", file=sys.stderr)
        return summary

    summary = {"phase": phase, "removed": [], "protected": [], "errors": []}
    try:
        protected = _protected_montage_paths(job, workdir)
    except Exception as exc:
        summary["errors"].append(f"метаданные результата: {exc}")
        _write_cleanup_log(workdir, summary)
        return summary
    for path, recursive in candidates:
        try:
            if path.parent != workdir:
                summary["errors"].append(f"{path.name}: путь вне каталога задания")
                continue
            if _cleanup_path_is_protected(path, protected, recursive):
                summary["protected"].append(path.name)
                continue
            if not path.exists() and not path.is_symlink():
                continue
            _remove_cleanup_path(path, recursive)
            if not path.exists() and not path.is_symlink():
                summary["removed"].append(path.name)
        except Exception as exc:
            summary["errors"].append(f"{path.name}: {exc}")
    _write_cleanup_log(workdir, summary)
    return summary


def _diagnostic_cleanup_keep(job_id: str, phase: str) -> Optional[dict]:
    """In diagnostic mode keep intermediates for quality audit.

    Returns the cleanup summary that the caller must return, or ``None``
    when normal cleanup should proceed.
    """
    if os.environ.get("AUTOMONTAGE_KEEP_INTERMEDIATES", "").strip().lower() not in ("1", "true", "yes"):
        return None
    try:
        workdir = _safe_montage_workdir(job_id)
        _write_cleanup_log(workdir, {"phase": phase, "removed": [], "protected": [],
                                     "errors": [], "kept": "diagnostic mode"})
    except Exception:
        pass
    return {"phase": phase, "removed": [], "protected": [], "errors": [],
            "kept": "diagnostic mode (AUTOMONTAGE_KEEP_INTERMEDIATES)"}


def _cleanup_render_artifacts(job_id: str, job: dict) -> dict:
    kept = _diagnostic_cleanup_keep(job_id, "render")
    if kept:
        return kept
    # The unified pipeline keeps ``status`` at ``processing`` while the optional
    # mirror / preparation phases run, so render artefacts are cleaned up as soon
    # as the main result exists (status ``processing`` or ``done``).
    if job.get("status") not in ("processing", "done") or not job.get("result"):
        return {"phase": "render", "removed": [], "protected": [], "errors": []}
    try:
        workdir = _safe_montage_workdir(job_id)
    except Exception as exc:
        summary = {"phase": "render", "removed": [], "protected": [], "errors": [str(exc)]}
        print(f"[montage-cleanup] {job_id}: {json.dumps(summary, ensure_ascii=False)}", file=sys.stderr)
        return summary
    try:
        candidates = [(workdir / name, False) for name in _RENDER_CLEANUP_FILES]
        return _cleanup_allowed_paths(job_id, job, "render", candidates)
    except Exception as exc:
        summary = {"phase": "render", "removed": [], "protected": [], "errors": [str(exc)]}
        _write_cleanup_log(workdir, summary)
        return summary


def _cleanup_publish_artifacts(job_id: str, job: dict) -> dict:
    kept = _diagnostic_cleanup_keep(job_id, "publish")
    if kept:
        return kept
    if job.get("status") != "done" or job.get("stage") != "published" or job.get("publish_errors"):
        return {"phase": "publish", "removed": [], "protected": [], "errors": []}
    try:
        workdir = _safe_montage_workdir(job_id)
    except Exception as exc:
        summary = {"phase": "publish", "removed": [], "protected": [], "errors": [str(exc)]}
        print(f"[montage-cleanup] {job_id}: {json.dumps(summary, ensure_ascii=False)}", file=sys.stderr)
        return summary
    try:
        candidates = [
            (workdir / "publish_mirrored", True),
            (workdir / "staging", True),
            (workdir / "youtube_staging", True),
            (workdir / f"{job_id}_tg.mp4", False),
        ]
        summary = _cleanup_allowed_paths(job_id, job, "publish", candidates)
        _maybe_retire_masters()
        return summary
    except Exception as exc:
        summary = {"phase": "publish", "removed": [], "protected": [], "errors": [str(exc)]}
        _write_cleanup_log(workdir, summary)
        return summary


_MASTER_RETENTION_LOCK = threading.Lock()
_MASTER_RETENTION_MAX_AGE_DAYS_DEFAULT = 7.0
_MASTER_RETENTION_MAX_TOTAL_BYTES_DEFAULT = 2 * 1024 * 1024 * 1024
_MASTER_RETENTION_MIN_FREE_BYTES_DEFAULT = 1 * 1024 * 1024 * 1024

# Master files currently read by in-flight platform preparations (job ids).
# Guarded by _MASTER_RETENTION_LOCK, so the retention sweep's snapshot and
# every (de)registration are linearized against each other: a job registered
# before a sweep decides can never lose its master mid-preparation, and a
# sweep that already completed its removal runs strictly before the next
# registration claim (the fallback to result_video then equals the ordinary
# missing-master case, not a downgrade during an active preparation).
_PREP_ACTIVE_MASTER_JOBS: set = set()


def _register_prep_master(job_id: str) -> None:
    """Claim retention protection for the job's master while it is read.

    Blocking so that a concurrently running sweep finishes (its decision
    window fully closed) before the claim takes effect; call from a worker
    thread (never the event loop) via ``asyncio.to_thread``.
    """
    with _MASTER_RETENTION_LOCK:
        _PREP_ACTIVE_MASTER_JOBS.add(str(job_id))


def _release_prep_master(job_id: str) -> None:
    """Drop the claim; safe to call twice and on every exit path."""
    job_id = str(job_id)
    if _MASTER_RETENTION_LOCK.acquire(blocking=False):
        try:
            _PREP_ACTIVE_MASTER_JOBS.discard(job_id)
        finally:
            _MASTER_RETENTION_LOCK.release()
        return
    # A sweep is running right now and already snapshotted the registry; the
    # atomic discard is safe in either position and the next sweep re-reads
    # the registry in sync (under the lock) and applies the policy then.
    _PREP_ACTIVE_MASTER_JOBS.discard(job_id)


def _retention_env_value(name: str, default: float) -> float:
    """Numeric env override for the master retention policy (non-negative)."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        return float(default)
    return max(value, 0.0)


def _master_retention_settings() -> tuple[float, int, int]:
    """Age/size/free-space limits that decide when a settled master may go.

    ``AUTOMONTAGE_MASTER_MAX_AGE_DAYS`` (0 disables age pruning),
    ``AUTOMONTAGE_MASTER_MAX_TOTAL_BYTES`` (0 disables the total-size ceiling)
    and ``AUTOMONTAGE_MASTER_MIN_FREE_BYTES`` (0 disables the low-space prune).
    """
    max_age_days = _retention_env_value(
        "AUTOMONTAGE_MASTER_MAX_AGE_DAYS", _MASTER_RETENTION_MAX_AGE_DAYS_DEFAULT)
    max_total_bytes = _retention_env_value(
        "AUTOMONTAGE_MASTER_MAX_TOTAL_BYTES", _MASTER_RETENTION_MAX_TOTAL_BYTES_DEFAULT)
    min_free_bytes = _retention_env_value(
        "AUTOMONTAGE_MASTER_MIN_FREE_BYTES", _MASTER_RETENTION_MIN_FREE_BYTES_DEFAULT)
    return max_age_days, int(max_total_bytes), int(min_free_bytes)


def _retire_masters() -> dict:
    """Apply the master retention policy across every persisted montage job.

    Only fully settled jobs (published, no errors and no unknown outcomes) may
    lose their master; unfinished, partially published or errored jobs always
    keep it so retry / second-platform publishing still uses the same quality
    source.  Deletes run oldest-first by age, then for low free disk space,
    then when the total master volume exceeds its ceiling.
    """
    if not _MASTER_RETENTION_LOCK.acquire(blocking=False):
        return {"phase": "master_retention", "removed": [], "errors": [],
                "skipped": "already running"}
    try:
        max_age_days, max_total_bytes, min_free_bytes = _master_retention_settings()
        summary: dict = {"phase": "master_retention", "removed": [], "kept": [],
                         "errors": []}
        candidates: list[dict] = []
        for state_path in sorted(MONTAGE_WORK.glob("*/job_state.json")):
            try:
                job = json.loads(state_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if job.get("kind") != "montage":
                continue
            settled = (
                job.get("stage") == "published"
                and not (job.get("publish_errors") or {})
                and not (job.get("publish_unknown") or {})
            )
            if not settled:
                continue
            path = _master_video_path(job)
            if path is None:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            candidates.append({
                "job_id": job.get("id"),
                "path": path,
                "size": int(stat.st_size),
                "mtime": float(stat.st_mtime),
            })

        # TOCTOU guard: a master may be resolved or read by an in-flight
        # platform preparation right now. The registry is guarded by the same
        # lock this sweep holds, so the snapshot is synchronized with every
        # registration; such jobs keep their master this round.
        active_prep_jobs = set(_PREP_ACTIVE_MASTER_JOBS)
        deferred = [entry for entry in candidates
                    if str(entry.get("job_id") or "") in active_prep_jobs]
        for entry in deferred:
            candidates.remove(entry)
            summary["kept"].append(
                f"{entry['job_id']}/{entry['path'].name} (prep in flight)")

        def _remove(entry: dict, reason: str) -> bool:
            label = f"{entry['job_id']}/{entry['path'].name}"
            try:
                entry["path"].unlink()
            except FileNotFoundError:
                pass
            except Exception as exc:
                summary["errors"].append(f"{label}: {exc}")
                return False
            summary["removed"].append(f"{label} ({reason})")
            return True

        now = time.time()
        if max_age_days > 0:
            deadline = now - max_age_days * 86400.0
            for entry in list(candidates):
                if entry["mtime"] < deadline and _remove(entry, f"age>{max_age_days}d"):
                    candidates.remove(entry)

        if min_free_bytes > 0 and candidates:
            try:
                free_bytes = int(shutil.disk_usage(MONTAGE_WORK).free)
            except Exception:
                free_bytes = 0
            for entry in sorted(candidates, key=lambda item: (item["mtime"], str(item["job_id"]))):
                if free_bytes >= min_free_bytes:
                    break
                if _remove(entry, f"low free space ({free_bytes} bytes)"):
                    candidates.remove(entry)
                    free_bytes += entry["size"]

        if max_total_bytes > 0 and candidates:
            total = sum(entry["size"] for entry in candidates)
            for entry in sorted(candidates, key=lambda item: (item["mtime"], str(item["job_id"]))):
                if total <= max_total_bytes:
                    break
                if _remove(entry, f"total size {total} > {max_total_bytes}"):
                    candidates.remove(entry)
                    total -= entry["size"]

        for entry in candidates:
            summary["kept"].append(f"{entry['job_id']}/{entry['path'].name}")
        if summary["removed"] or summary["errors"]:
            print(f"[master-retention] {json.dumps(summary, ensure_ascii=False)}", file=sys.stderr)
        return summary
    finally:
        _MASTER_RETENTION_LOCK.release()


def _maybe_retire_masters() -> None:
    """Run the retention sweep without blocking a running event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        try:
            _retire_masters()
        except Exception as exc:
            print(f"[master-retention] sweep failed: {exc}", file=sys.stderr)
        return

    async def _run() -> None:
        try:
            await asyncio.to_thread(_retire_masters)
        except Exception as exc:
            print(f"[master-retention] background sweep failed: {exc}", file=sys.stderr)

    loop.create_task(_run())


def _run_cleanup_safely(cleanup, job_id: str, job: dict) -> dict:
    try:
        return cleanup(job_id, job)
    except Exception as exc:
        summary = {
            "phase": "publish" if cleanup is _cleanup_publish_artifacts else "render",
            "removed": [],
            "protected": [],
            "errors": [str(exc)],
        }
        try:
            workdir = _safe_montage_workdir(job_id)
            _write_cleanup_log(workdir, summary)
        except Exception:
            pass
        return summary


def _save_montage_job(job_id: str) -> None:
    job = jobs.get(job_id)
    if not job or job.get("kind") != "montage":
        return
    workdir = _safe_montage_workdir(job_id)
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / "job_state.json"
    temporary = workdir / ".job_state.json.tmp"
    temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(temporary, target)


# --------------------------------------------------------------------------
# Mirrored render variant (task spec section 9).
#
# The "mirrored" publishing variant used to be rendered inside
# ``process_publish``.  Publishing must never render/encode (spec 9.1), so the
# variant is now prepared by the montage pipeline: either together with the
# main render (checkbox "prepare_mirrored") or as a separate montage operation
# triggered from the UI.  The ready file is stored on the job together with a
# signature so a stale variant (produced with different montage parameters) is
# never silently reused (spec 9.5).
# --------------------------------------------------------------------------

_MIRROR_VARIANT_DIR = "variants_mirrored"

_MONTAGE_SIGNATURE_KEYS = (
    "mirror_horizontal", "face_tracking", "autozoom", "subtitles",
    "subtitle_mode", "subtitle_position", "subtitle_x", "subtitle_y",
    "subtitle_scale", "subtitle_box", "subtitle_font",
    "blur_banned_words", "edge_mode", "overlays", "animation", "animations",
    "zoom_timeline_override",
    "camera_mode", "follow_strength", "lag_seconds", "min_zoom",
    "camera_wobble",
)


def _validate_camera_mode(raw: Any, legacy_face_tracking: bool = False) -> str:
    mode = str(raw or "").strip().lower()
    if mode in {"off", "hold", "live"}:
        return mode
    # Old jobs only carry face_tracking=true: that means eye hold.
    if mode == "" and legacy_face_tracking:
        return "hold"
    if mode not in ("", "off"):
        raise HTTPException(400, f"camera_mode должен быть off, hold или live: {raw!r}")
    return "off"


def _validate_float_range(raw: Any, low: float, high: float,
                          default: float, name: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(400, f"{name} должен быть числом")
    if not (low <= value <= high):
        raise HTTPException(400, f"{name} должен быть от {low} до {high}")
    return value


def _validate_timeline_animations(raw: Any, legacy_id: str = "",
                                  legacy_start: float = 0.0,
                                  legacy_offset_x: int = 0,
                                  legacy_item_size: int = 72) -> list:
    """Validate the timeline animation list [{id, start, offset_x, item_size}].

    Falls back to the legacy single-animation fields when no list is given,
    so old clients keep working.
    """
    items = []
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            raise HTTPException(400, "animations должен быть JSON-списком")
        if not isinstance(parsed, list):
            raise HTTPException(400, "animations должен быть JSON-списком")
        items = parsed
    if not items and legacy_id:
        items = [{"id": legacy_id, "start": legacy_start,
                  "offset_x": legacy_offset_x, "item_size": legacy_item_size}]
    validated = []
    for entry in items[:20]:
        if not isinstance(entry, dict):
            continue
        animation_id = str(entry.get("id") or "")
        if not animation_id or Path(animation_id).name != animation_id:
            raise HTTPException(400, f"Некорректный id анимации: {animation_id!r}")
        if not (montage_engine.ANIMATIONS_DIR / animation_id / "manifest.json").is_file():
            raise HTTPException(400, f"Анимация не найдена: {animation_id}")
        try:
            start = max(0.0, float(entry.get("start") or 0.0))
        except (TypeError, ValueError):
            raise HTTPException(400, "start должен быть числом")
        try:
            offset_x = int(entry.get("offset_x") or 0)
        except (TypeError, ValueError):
            raise HTTPException(400, "offset_x должен быть целым")
        if not -1000 <= offset_x <= 1000:
            raise HTTPException(400, "offset_x должен быть от -1000 до 1000")
        try:
            item_size = int(entry.get("item_size") or 72)
        except (TypeError, ValueError):
            raise HTTPException(400, "item_size должен быть целым")
        try:
            speed = float(entry.get("speed", 100.0))
        except (TypeError, ValueError):
            raise HTTPException(400, "speed должен быть числом")
        if not 10.0 <= speed <= 400.0:
            raise HTTPException(400, "speed должен быть от 10 до 400")
        validated.append({"id": animation_id, "start": start,
                          "offset_x": offset_x,
                          "item_size": max(20, min(1200, item_size)),
                          "speed": speed})
    return validated


def _montage_signature(params: dict) -> str:
    relevant = {key: params.get(key) for key in _MONTAGE_SIGNATURE_KEYS}
    blob = json.dumps(relevant, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _mirrored_variant_signature(job: dict) -> str:
    options = dict(job.get("params") or {})
    options["mirror_horizontal"] = not bool(options.get("mirror_horizontal"))
    return _montage_signature(options)


def _mirror_variant_entry(job: dict) -> Optional[dict]:
    entry = (job.get("variants") or {}).get("mirrored")
    return entry if isinstance(entry, dict) else None


def mirror_variant_ready(job: dict) -> bool:
    """True only when a ready, non-stale mirrored file exists on disk."""
    entry = _mirror_variant_entry(job)
    if not entry or not entry.get("ready"):
        return False
    if entry.get("signature") != _mirrored_variant_signature(job):
        return False
    path = entry.get("path")
    return bool(path) and Path(str(path)).is_file()


def _public_mirror_variant(job: dict) -> Optional[dict]:
    state = job.get("mirror_variant")
    if not isinstance(state, dict):
        if mirror_variant_ready(job):
            return {"status": "done", "progress": 100, "detail": "Отражённая версия готова",
                    "error": "", "path": _mirror_variant_entry(job).get("path"), "operation": None}
        return None
    public = dict(state)
    public["ready"] = mirror_variant_ready(job)
    return public


async def _render_mirrored_variant(
    job_id: str, *, report_cb=None, progress_cb=None,
) -> tuple:
    """Render the alternate (mirrored) video for *job_id*.

    Reuses the montage pipeline with the mirror flag toggled; overlays are
    re-staged next to the alternate workdir so the render stays self-contained.
    Returns ``(path, alternate_options)``.
    """
    job = jobs[job_id]
    loop = asyncio.get_running_loop()
    workdir = MONTAGE_WORK / job_id
    alternate_options = dict(job.get("params") or {})
    alternate_options.pop("prepare_mirrored", None)
    alternate_options["mirror_horizontal"] = not bool(alternate_options.get("mirror_horizontal"))
    if job.get("audio_censor_srt"):
        alternate_options["audio_censor_srt"] = job["audio_censor_srt"]
    alternate_workdir = workdir / _MIRROR_VARIANT_DIR
    alternate_workdir.mkdir(parents=True, exist_ok=True)
    alternate_overlays = []
    overlay_root = (workdir / "overlays").resolve()
    for index, overlay in enumerate(alternate_options.get("overlays") or []):
        staged_overlay = dict(overlay)
        if str(staged_overlay.get("kind") or "") == "image":
            source_image = Path(str(staged_overlay.get("image_path") or "")).resolve()
            try:
                source_image.relative_to(overlay_root)
            except ValueError:
                raise RuntimeError("Файл наложения находится вне каталога задания")
            if not source_image.is_file():
                raise RuntimeError(f"Изображение наложения не найдено: {source_image.name}")
            staged_dir = alternate_workdir / "overlays"
            staged_dir.mkdir(parents=True, exist_ok=True)
            staged_image = staged_dir / f"overlay_{index + 1:02d}{source_image.suffix.lower()}"
            shutil.copy2(source_image, staged_image)
            staged_overlay["image_path"] = str(staged_image)
        alternate_overlays.append(staged_overlay)
    alternate_options["overlays"] = alternate_overlays

    def alt_progress(percent: float, stage: str, detail: str) -> None:
        if progress_cb is not None:
            progress_cb(percent, stage, detail)

    meta = await asyncio.to_thread(
        montage_engine.render_montage,
        job_id=f"{job_id}_mirror",
        workdir=alternate_workdir,
        source_video=Path(job["video_path"]),
        srt_text=job["srt_text"],
        options=alternate_options,
        progress_cb=alt_progress,
        report_cb=report_cb,
        cancel_cb=_montage_cancel_cb(job_id),
        log_cb=lambda message: asyncio.run_coroutine_threadsafe(
            _montage_announce(job_id, f"Отражённая версия: {message}"), loop
        ),
    )
    return Path(meta["result_video"]), alternate_options


async def process_mirror_variant(job_id: str) -> None:
    """Prepare the mirrored publish variant as its own montage operation."""
    job = jobs.get(job_id)
    if not job or job.get("kind") != "montage":
        return
    loop = asyncio.get_running_loop()
    signature = _mirrored_variant_signature(job)
    if mirror_variant_ready(job):
        previous = dict(job.get("mirror_variant") or {})
        previous.update(
            status="done", progress=100, detail="Отражённая версия готова",
            error="", path=_mirror_variant_entry(job).get("path"), signature=signature,
        )
        job["mirror_variant"] = previous
        _save_montage_job(job_id)
        return

    job["mirror_variant"] = {
        "status": "generating", "progress": 0,
        "detail": "Отражённая версия (мастер): рендер", "error": "",
        "path": None, "signature": signature, "operation": None,
    }
    _save_montage_job(job_id)
    await _montage_announce(job_id, "Отражённая версия (мастер): рендер...")

    def report(snapshot: dict) -> None:
        def store() -> None:
            state = dict(job.get("mirror_variant") or {})
            state["operation"] = snapshot
            state["progress"] = snapshot.get("progress", state.get("progress", 0))
            state["detail"] = snapshot.get("detail") or state.get("detail") or "Подготовка отражённой версии"
            if snapshot.get("status") in ("failed", "error"):
                state["status"] = "error"
            job["mirror_variant"] = state
        loop.call_soon_threadsafe(store)

    def progress(percent: float, stage: str, detail: str) -> None:
        def store() -> None:
            state = dict(job.get("mirror_variant") or {})
            try:
                value = max(0.0, min(100.0, float(percent)))
            except (TypeError, ValueError):
                return
            state["progress"] = max(float(state.get("progress") or 0), value)
            if detail:
                state["detail"] = detail
            job["mirror_variant"] = state
        loop.call_soon_threadsafe(store)

    try:
        path, options = await _render_mirrored_variant(job_id, report_cb=report, progress_cb=progress)
    except BaseException as exc:
        if _cancel_requested(job_id):
            state = dict(job.get("mirror_variant") or {})
            state.update(status="cancelled", error="Отменено пользователем",
                         detail="Отменено пользователем")
            job["mirror_variant"] = state
            _save_montage_job(job_id)
            await _montage_announce(job_id, "Отражённая версия: отменено пользователем")
            return
        state = dict(job.get("mirror_variant") or {})
        state.update(status="error", error=str(exc), detail=str(exc))
        state["operation"] = failed_snapshot(state.get("operation"), str(exc))
        job["mirror_variant"] = state
        _save_montage_job(job_id)
        await _montage_announce(job_id, f"Отражённая версия не подготовлена: {exc}")
        return
    variants = dict(job.get("variants") or {})
    variants["mirrored"] = {
        "path": str(path),
        "mirror_horizontal": bool(options.get("mirror_horizontal")),
        "signature": signature,
        "ready": True,
        "created_at": time.time(),
    }
    job["variants"] = variants
    state = dict(job.get("mirror_variant") or {})
    state.update(status="done", progress=100, detail="Отражённая версия готова",
                 error="", path=str(path))
    job["mirror_variant"] = state
    _save_montage_job(job_id)
    await _montage_announce(job_id, f"Отражённая версия готова: {path.name}")


# --------------------------------------------------------------------------
# Pre-publication preparation (task: "publish only sends ready files").
#
# Publishing must never run FFmpeg/PIL — no transcoding, FPS normalisation,
# mirroring or cover conversion.  Every local video/image transform happens
# here: during the montage stage or in the dedicated "prepare for publishing"
# operation.  The ready artefacts are stored on the job
# (``job["publish_prep"]``) together with their purpose, readiness, size/params,
# source signature and validation result.  A prepared file is reused while its
# signature matches the current source and montage parameters, so a single file
# serves several platforms (Instagram trial + feed share one ``instagram`` file).
# --------------------------------------------------------------------------

_PUBLISH_PREP_DIR = "publish_prep"

_TARGET_PURPOSE = {
    "instagram": "instagram",
    "trial": "instagram",
    "threads": "instagram",
    "youtube": "youtube",
    "telegram": "telegram",
}

_PURPOSE_LABELS = {
    "instagram": "Instagram",
    "youtube": "YouTube",
    "telegram": "Telegram",
}

_PREP_VIDEO_LABELS = {
    "instagram": "Подготовка видео для Instagram",
    "youtube": "Подготовка видео для YouTube",
    "telegram": "Подготовка видео для Telegram",
}

_PREP_COVER_PURPOSES = ("instagram", "youtube")

_PUBLISH_TARGET_SPECS = {
    "instagram_trial": ("trial", False, "Instagram пробное"),
    "instagram_trial_mirrored": ("trial", True, "Instagram пробное перевёрнутое"),
    "instagram_feed": ("instagram", False, "Instagram основная лента"),
    "instagram_feed_mirrored": ("instagram", True, "Instagram основная лента перевёрнутое"),
    "youtube": ("youtube", False, "YouTube"),
    "youtube_mirrored": ("youtube", True, "YouTube перевёрнутое"),
}


def _public_publish_ready(job: dict) -> dict:
    ready = {}
    for target_id, (kind, mirrored, _label) in _PUBLISH_TARGET_SPECS.items():
        purpose = _TARGET_PURPOSE.get(kind)
        ready[target_id] = bool(purpose) and _prepared_video_entry(job, purpose, mirrored) is not None
    return ready


def _prep_key(purpose: str, mirrored: bool) -> str:
    return f"{purpose}:{'mirror' if mirrored else 'main'}"


def _file_identity(path) -> str:
    try:
        stat = Path(path).stat()
        return f"{Path(path).resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    except OSError:
        return str(path)


def _platform_copy_probe(path) -> dict:
    """Cheap stream-set description of a file (used by platform remux fast paths)."""
    result = subprocess.run(
        [shutil.which("ffprobe") or "ffprobe", "-v", "error", "-show_entries",
         ("stream=index,codec_type,codec_name,profile,level,pix_fmt,avg_frame_rate,width,height,sample_rate,"
          "color_primaries,color_transfer,color_space,color_range:"
          "format=format_name,duration"),
         "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr.strip()[-400:]}")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if str(s.get("codec_type")) == "video"), {}) or {}
    audio = next((s for s in streams if str(s.get("codec_type")) == "audio"), {}) or {}
    fmt = data.get("format") or {}
    return {
        "video": video,
        "audio": audio,
        "format_name": str(fmt.get("format_name") or ""),
        "color_profile": probe_color_profile(path),
    }


def _mp4_copy_ready(probe: dict) -> bool:
    """True when the container and stream set can be remuxed for a platform as-is."""
    fmt = str(probe.get("format_name") or "")
    if not ("mp4" in fmt or "mov" in fmt):
        return False
    video = probe.get("video") or {}
    if str(video.get("codec_name") or "").lower() not in {"h264", "hevc"}:
        return False
    codec = str(video.get("codec_name") or "").lower()
    profile_name = str(video.get("profile") or "").lower()
    if not profile_name:
        return False
    if codec == "h264" and profile_name not in {"baseline", "constrained baseline", "main", "high"}:
        return False
    if codec == "hevc" and "main" not in profile_name:
        return False
    try:
        level = int(video.get("level") or 0)
    except (TypeError, ValueError):
        level = 0
    if level <= 0 or level > 153:
        return False
    if str(video.get("pix_fmt") or "").lower() != "yuv420p":
        return False
    width = video.get("width")
    height = video.get("height")
    if not isinstance(width, int) or not isinstance(height, int):
        return False
    if width <= 0 or width % 2 or height % 2:
        return False
    try:
        from fractions import Fraction
        fps = float(Fraction(str(video.get("avg_frame_rate") or "")))
    except (ValueError, ZeroDivisionError):
        return False
    if not 0 < fps <= 60:
        return False
    if (probe.get("color_profile") or {}).get("hdr"):
        return False
    audio = probe.get("audio") or {}
    if audio and str(audio.get("codec_name") or "").lower() != "aac":
        return False
    return True


PREP_EXPORT_PROFILE = "b3-3-color-preserving"


def _publish_prep_signature(job: dict, key: str, source_path) -> str:
    parts = [key, _file_identity(source_path), PREP_EXPORT_PROFILE]
    if key.endswith(":mirror"):
        parts.append(_mirrored_variant_signature(job))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _publish_prep_entries(job: dict) -> dict:
    entries = job.get("publish_prep")
    if not isinstance(entries, dict):
        entries = {}
        job["publish_prep"] = entries
    return entries


def _master_video_path(job: dict) -> Optional[Path]:
    """Path of the recorded master source (a fully effected pre-delivery stream)."""
    result = job.get("result") or {}
    name = str(result.get("master_video") or "")
    job_id = str(result.get("job_id") or "")
    if not name or not job_id:
        return None
    # Containment: both components come from persisted job state; flatten them
    # to bare names so a tampered record cannot escape the job workdir.
    job_id = Path(job_id).name
    name = Path(name).name
    if not name or not job_id:
        return None
    if name == Path(str(result.get("result_video") or "")).name:
        return None
    path = MONTAGE_WORK / job_id / name
    if not path.is_file():
        return None
    return path


def _prep_source_video(job: dict, mirrored: bool) -> Optional[Path]:
    """Current source for a variant: the master file or the main result.

    The master carries every finished effect (rotate/mirror, zoom, overlays,
    subtitles and muted audio) at the highest available quality; platform
    preparation reads it instead of the delivery encode.  When the master file
    is missing or unreadable the pipeline falls back to ``result_video`` and
    logs a warning (API contract stays unchanged).
    """
    if mirrored:
        if not mirror_variant_ready(job):
            return None
        path = Path(str(_mirror_variant_entry(job).get("path")))
        return path if path.is_file() else None
    master = _master_video_path(job)
    if master is not None:
        if montage_engine.usable_video(master):
            return master
        print(f"[montage-prep] {job.get('id')}: master unreadable "
              f"({master.name}), fallback to result_video", file=sys.stderr)
    result = job.get("result") or {}
    path = result.get("result_video")
    if not path:
        return None
    path = Path(str(path))
    return path if path.is_file() else None


def _prepared_video_entry(job: dict, purpose: str, mirrored: bool) -> Optional[dict]:
    """Return a ready, non-stale prepared video entry for purpose/mirrored."""
    key = _prep_key(purpose, mirrored)
    entry = _publish_prep_entries(job).get(key)
    if not isinstance(entry, dict) or not entry.get("ready"):
        return None
    source = _prep_source_video(job, mirrored)
    if source is None:
        return None
    if entry.get("signature") != _publish_prep_signature(job, key, source):
        return None
    path = entry.get("path")
    if not path or not Path(str(path)).is_file():
        return None
    return entry


def _prepared_cover_entry(job: dict, purpose: str, cover_path) -> Optional[dict]:
    """Return a ready prepared cover for *cover_path* (Instagram/YouTube)."""
    if not cover_path:
        return None
    cover_path = Path(str(cover_path))
    if not cover_path.is_file():
        return None
    key = f"cover:{purpose}"
    entry = _publish_prep_entries(job).get(key)
    if not isinstance(entry, dict) or not entry.get("ready"):
        return None
    if entry.get("signature") != _publish_prep_signature(job, key, cover_path):
        return None
    path = entry.get("path")
    if not path or not Path(str(path)).is_file():
        return None
    return entry


def _render_prepared_video(purpose: str, source, target_dir, stem: str,
                           mirrored: bool, progress, cancel_cb=None) -> Path:
    """Produce the platform-ready video (local transform; run off the event loop)."""
    source = Path(source)
    target_dir = Path(target_dir)
    if purpose == "youtube":
        from montage.youtube_publisher import YouTubePublisher
        publisher = YouTubePublisher(
            montage_engine.INSTAPOSTER_DIR, Path(sys.executable),
            target_dir / "youtube.log",
        )
        output = target_dir / f"{stem}_youtube.mp4"
        publisher.prepare_video(source, output, progress)
        return output
    if purpose == "telegram":
        try:
            duration = float(montage_engine.ffprobe(source).get("format", {}).get("duration") or 0) or 30.0
        except Exception:
            duration = 30.0
        output = target_dir / f"{stem}_tg.mp4"
        telegram_limit = 45 * 1024 * 1024
        try:
            copy_ready = source.stat().st_size <= telegram_limit and _mp4_copy_ready(_platform_copy_probe(source))
        except Exception:
            copy_ready = False
        if copy_ready:
            try:
                ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
                output.unlink(missing_ok=True)
                command = [
                    ffmpeg, "-y", "-i", str(source),
                    "-map", "0:v:0", "-map", "0:a:0?",
                    "-c", "copy",
                    "-movflags", "+faststart", str(output),
                ]
                process = subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
                try:
                    while True:
                        try:
                            code = process.wait(timeout=0.5)
                            break
                        except subprocess.TimeoutExpired:
                            if cancel_cb is not None:
                                try:
                                    cancelled_now = bool(cancel_cb())
                                except Exception:
                                    cancelled_now = False
                                if cancelled_now:
                                    process.terminate()
                                    try:
                                        process.wait(timeout=5)
                                    except subprocess.TimeoutExpired:
                                        process.kill()
                                        process.wait()
                                    raise montage_engine.MontageCancelled("Отменено пользователем")
                except BaseException:
                    if process.poll() is None:
                        process.terminate()
                    raise
                if code == 0 and output.exists():
                    progress(100, "Telegram: поток уже совместим — remux без перекодирования")
                    return output
            except montage_engine.MontageCancelled:
                raise
            except Exception:
                pass
        montage_engine._transcode_to_fit(source, output, telegram_limit, duration, progress,
                                         cancel_cb=cancel_cb)
        return output
    from montage.instagram_publisher import InstagramPublisher
    publisher = InstagramPublisher(
        montage_engine.INSTAPOSTER_DIR, shutil.which("ffmpeg") or "ffmpeg",
        target_dir / "publish.log", lambda message: None,
    )
    return publisher.prepare_variant(source, target_dir, stem, "normal", progress)


def _render_prepared_cover(purpose: str, cover_path, output) -> Path:
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if purpose == "youtube":
        from montage.youtube_publisher import YouTubePublisher
        publisher = YouTubePublisher(
            montage_engine.INSTAPOSTER_DIR, Path(sys.executable),
            output.parent / "youtube.log",
        )
        publisher.prepare_thumbnail(Path(cover_path), output)
        return output
    from PIL import Image
    with Image.open(cover_path) as image:
        image.convert("RGB").save(output, "JPEG", quality=94, optimize=True)
    return output


def _validate_prepared_video(path) -> dict:
    path = Path(path)
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("Подготовленный файл не создан или пуст")
    try:
        probe = montage_engine.ffprobe(path)
    except Exception as exc:
        raise RuntimeError(f"Подготовленный файл повреждён: {exc}") from exc
    duration = (probe.get("format") or {}).get("duration")
    return {"size": path.stat().st_size, "duration": duration}


def _preparation_error_message(target: dict) -> str:
    """Human-readable reason a publish target is not locally ready (spec 7)."""
    label = target.get("label") or target.get("id") or ""
    mirrored = bool(target.get("mirrored"))
    suffix = " (отражённое)" if mirrored else ""
    reason = str(target.get("reason") or "")
    if reason == "mirror_not_ready":
        return (
            f"Отражённая версия для «{label}»{suffix} не готова. "
            "Включите «Создать отражённую версию» при монтаже и повторите."
        )
    if reason == "cover_not_ready":
        return f"Обложка для «{label}»{suffix} не подготовлена."
    if reason == "preparation_failed":
        return f"Не удалось подготовить видео для «{label}»{suffix}."
    return f"Видео для «{label}»{suffix} не подготовлено."


def _publish_readiness(job: dict, targets: list) -> tuple[dict, list]:
    """Resolve prepared artefacts for resolved publish targets.

    Returns ``(prepared, missing)`` where *prepared* maps a target id to its
    ready paths (video + optional cover/thumbnail) and *missing* lists targets
    that still need local preparation.  Publishing uses only ready files.
    """
    prepared: dict[str, dict] = {}
    missing: list[dict] = []
    for target in targets:
        purpose = _TARGET_PURPOSE.get(str(target.get("kind") or ""))
        target_id = str(target.get("id") or target.get("kind") or "")
        mirrored = bool(target.get("mirrored"))
        entry = _prepared_video_entry(job, purpose, mirrored) if purpose else None
        if entry is None:
            if mirrored and not mirror_variant_ready(job):
                reason = "mirror_not_ready"
            else:
                reason = "video_not_ready"
            missing.append({
                "target": target_id, "label": target.get("label"),
                "kind": target.get("kind"), "mirrored": mirrored, "reason": reason,
            })
            continue
        item: dict = {"video_path": entry.get("path")}
        cover = target.get("cover_path")
        if cover:
            cover_entry = _prepared_cover_entry(job, purpose, cover)
            if cover_entry is None:
                missing.append({
                    "target": target_id, "label": target.get("label"),
                    "kind": target.get("kind"), "mirrored": mirrored, "reason": "cover_not_ready",
                })
                continue
            if purpose == "youtube":
                item["thumbnail_path"] = cover_entry.get("path")
            else:
                item["cover_path"] = cover_entry.get("path")
        prepared[target_id] = item
    return prepared, missing


async def process_prepare_publish(job_id: str, targets: list, cover_path: Optional[str] = None,
                                  cover_id: str = ""):
    """Prepare (locally) every artefact the given publish targets need.

    Runs the platform-specific video transforms and cover conversions *before*
    publishing, stores the results on the job and records a stage snapshot that
    the three-level progress bar renders.  Reuses already prepared artefacts.
    """
    job = jobs.get(job_id)
    if not job or job.get("kind") != "montage":
        return
    workdir = MONTAGE_WORK / job_id
    prep_dir = workdir / _PUBLISH_PREP_DIR
    prep_dir.mkdir(parents=True, exist_ok=True)

    # In-flight master guard (TOCTOU): from this point the retention sweep
    # must not unlink the master this preparation resolves/reads, until the
    # preparation finishes (success, error or cancellation). The claim waits
    # for a concurrently running sweep (in a thread, off the event loop) so
    # registration is linearized with the sweep's own decision window.
    await asyncio.to_thread(_register_prep_master, job_id)
    try:
        needs: list[dict] = []
        seen: set = set()
        for target in targets:
            purpose = _TARGET_PURPOSE.get(str(target.get("kind") or ""))
            if not purpose:
                continue
            mirrored = bool(target.get("mirrored"))
            key = _prep_key(purpose, mirrored)
            if key in seen:
                continue
            seen.add(key)
            needs.append({"kind": "video", "key": key, "purpose": purpose, "mirrored": mirrored})
        requested_purposes = {item["purpose"] for item in needs}
        if cover_path:
            for purpose in _PREP_COVER_PURPOSES:
                if purpose not in requested_purposes:
                    continue
                key = f"cover:{purpose}"
                if key in seen:
                    continue
                seen.add(key)
                needs.append({"kind": "cover", "key": key, "purpose": purpose, "mirrored": False})

        # A mirrored artefact can only be produced from an already-rendered mirror
        # variant.  Its absence is never a reason to render here (publishing and
        # preparation never mirror) — drop those needs so the remaining targets are
        # still prepared; publish reports the missing mirrored targets per target.
        # Resolution reads the master / mirror files (ffprobe inside
        # usable_video), so keep it off the event loop.
        unresolved: list[dict] = []
        for item in needs:
            if item["kind"] != "video":
                continue
            if await asyncio.to_thread(_prep_source_video, job, item["mirrored"]) is None:
                unresolved.append(item)
        if unresolved:
            needs = [item for item in needs if item not in unresolved]

        if not needs:
            job["prepare_operation"] = None
            job["prepare_status"] = "done"
            _save_montage_job(job_id)
            return

        stages = []
        for item in needs:
            if item["kind"] == "video":
                label = _PREP_VIDEO_LABELS[item["purpose"]]
                if item["mirrored"]:
                    label += " (отражённое)"
                stages.append(Stage(item["key"], label, 3.0))
            else:
                stages.append(Stage(item["key"], f"Подготовка обложки для {_PURPOSE_LABELS[item['purpose']]}", 1.0))

        def on_change(tracker: ProgressTracker) -> None:
            job["prepare_operation"] = tracker.snapshot()

        tracker = ProgressTracker(f"{job_id}:prepare", "publish_prepare", stages,
                                  label="Подготовка к публикации", on_change=on_change)
        job["prepare_status"] = "running"
        job["prepare_error"] = None
        job["prepare_operation"] = tracker.snapshot()
        _save_montage_job(job_id)
        await _montage_announce(job_id, "Подготовка файлов к публикации...")

        try:
            for item in needs:
                if _cancel_requested(job_id):
                    raise montage_engine.MontageCancelled("Отменено пользователем")
                key = item["key"]
                tracker.start(key, "Подготовка")
                if item["kind"] == "video":
                    source = await asyncio.to_thread(_prep_source_video, job, item["mirrored"])
                    if source is None:
                        tracker.skip(key, "Источник ещё не готов")
                        continue
                    existing = await asyncio.to_thread(_prepared_video_entry, job, item["purpose"], item["mirrored"])
                    if existing is not None:
                        tracker.complete(key, "Уже подготовлено")
                        continue
                    stem = f"{job_id}_{key.replace(':', '_')}"

                    def video_progress(percent, detail, _key=key):
                        tracker.update(_key, percent, detail)

                    path = await asyncio.to_thread(
                        _render_prepared_video, item["purpose"], source, prep_dir, stem,
                        item["mirrored"], video_progress,
                        cancel_cb=_montage_cancel_cb(job_id),
                    )
                    info = _validate_prepared_video(path)
                    _publish_prep_entries(job)[key] = {
                        "path": str(path),
                        "purpose": item["purpose"],
                        "kind": "video",
                        "mirrored": item["mirrored"],
                        "ready": True,
                        "signature": _publish_prep_signature(job, key, source),
                        "size": info["size"],
                        "duration": info.get("duration"),
                        "validation": {"ok": True, "detail": f"{info['size']} байт"},
                        "created_at": time.time(),
                    }
                    tracker.complete(key, f"Готово: {Path(path).name}")
                else:
                    existing = _prepared_cover_entry(job, item["purpose"], cover_path)
                    if existing is not None:
                        tracker.complete(key, "Уже подготовлено")
                        continue
                    output = prep_dir / f"{job_id}_cover_{item['purpose']}.jpg"
                    await asyncio.to_thread(_render_prepared_cover, item["purpose"], cover_path, output)
                    if not output.is_file() or output.stat().st_size == 0:
                        raise RuntimeError("Не удалось подготовить обложку")
                    _publish_prep_entries(job)[key] = {
                        "path": str(output),
                        "purpose": item["purpose"],
                        "kind": "cover",
                        "cover_id": cover_id,
                        "ready": True,
                        "signature": _publish_prep_signature(job, key, cover_path),
                        "size": output.stat().st_size,
                        "created_at": time.time(),
                    }
                    tracker.complete(key, f"Готово: {output.name}")
            tracker.finish("Файлы для публикации готовы")
            job["prepare_status"] = "done"
            _save_montage_job(job_id)
            await _montage_announce(job_id, "Файлы для публикации готовы")
        except BaseException as exc:
            if _cancel_requested(job_id):
                job["prepare_operation"] = failed_snapshot(tracker.snapshot(), "Отменено пользователем")
                job["prepare_status"] = "cancelled"
                job["prepare_error"] = "Отменено пользователем"
                _save_montage_job(job_id)
                await _montage_announce(job_id, "Подготовка к публикации отменена пользователем")
                raise montage_engine.MontageCancelled("Отменено пользователем")
            job["prepare_operation"] = failed_snapshot(tracker.snapshot(), str(exc))
            job["prepare_status"] = "error"
            job["prepare_error"] = str(exc)
            _save_montage_job(job_id)
            await _montage_announce(job_id, f"Подготовка к публикации не удалась: {exc}")
    finally:
        # Release on every exit path: success, error and cancellation (the
        # inner handler re-raises MontageCancelled, so the outer finally is
        # the only guarantee against stale registry claims).
        _release_prep_master(job_id)


def _initialize_publish_attempt(job: dict, targets: list[dict], preparing_ids: Optional[set[str]] = None) -> None:
    """Publish a queued/in-progress snapshot before the API response returns.

    The UI begins polling immediately after POST /publish. Marking the job as
    publishing synchronously prevents the first GET from seeing the previous
    completed montage state and stopping the poll timer before the background
    worker has started.
    """
    preparing_ids = preparing_ids or set()
    job["status"] = "publishing"
    job["stage"] = "publishing"
    job["detail"] = "Подготовка файлов к публикации..." if preparing_ids else "Публикация..."
    job["error"] = None
    job["publish"] = dict(job.get("publish") or {})
    job["publish_status"] = dict(job.get("publish_status") or {})
    job["publish_started"] = dict(job.get("publish_started") or {})
    job["publish_unknown"] = dict(job.get("publish_unknown") or {})
    job["publish_errors"] = dict(job.get("publish_errors") or {})
    progress = {}
    for target in targets:
        target_id = str(target.get("id") or target.get("kind") or "")
        if not target_id:
            continue
        job["publish_unknown"].pop(target_id, None)
        job["publish_errors"].pop(target_id, None)
        preparing = target_id in preparing_ids
        progress[target_id] = {
            "label": str(target.get("label") or target_id),
            "progress": 0.0,
            "status": "preparing" if preparing else "queued",
            "detail": "Подготовка файла" if preparing else "Ожидание отправки",
            "steps": [],
        }
    job["publish_progress"] = progress
    job["last_publish_targets"] = targets
    job["publish_tracking"] = True


def _recover_publish_outcomes(job: dict) -> None:
    """Classify per-platform publish outcomes after an interrupted publish.

    Serves two purposes (task 2):

    * preserved confirmed successes: a platform already reported as published
      is kept published with its ids/links; it is never silently republished;
    * distinguishing the three outcome kinds used after a restart:
      published / failed / unknown.  A target whose outcome could not be
      determined (started but no terminal event was persisted, or a legacy
      job that predates per-target tracking) stays in an explicit *unknown*
      state that the UI shows as "requires manual check" and that is NOT
      retried without an explicit user decision.
    """
    progress = dict(job.get("publish_progress") or {})
    statuses = dict(job.get("publish_status") or {})
    errors = dict(job.get("publish_errors") or {})
    unknown = dict(job.get("publish_unknown") or {})
    started = set((job.get("publish_started") or {}).keys())
    legacy = not bool(job.get("publish_tracking"))
    confirmed = dict(job.get("publish") or {})
    for target in job.get("last_publish_targets") or []:
        tid = str(target.get("id") or target.get("kind") or "")
        if not tid:
            continue
        item = progress.get(tid) or {}
        state = statuses.get(tid)
        if state == "published" or item.get("status") == "done" or tid in confirmed:
            statuses[tid] = "published"
        elif state == "failed" or item.get("status") == "error":
            statuses[tid] = "failed"
            errors.setdefault(tid, item.get("detail") or "Публикация не выполнена")
        elif state == "unknown" or item.get("status") == "unknown":
            statuses[tid] = "unknown"
            unknown.setdefault(tid, item.get("detail") or "Результат публикации неизвестен")
        elif legacy or tid in started:
            statuses[tid] = "unknown"
            unknown[tid] = (
                "Результат неизвестен: сервер перезапущен во время публикации; "
                "проверьте публикацию вручную"
            )
        else:
            statuses[tid] = "failed"
            errors[tid] = "Публикация не начата до перезапуска сервера — можно повторить"
    for tid, state in statuses.items():
        item = progress.get(tid)
        if item is not None:
            item["status"] = {
                "published": "done", "failed": "error", "unknown": "unknown",
            }.get(state, str(item.get("status") or "unknown"))
            if state == "published":
                item["progress"] = 100
                item["detail"] = item.get("detail") or "Опубликовано"
            elif state == "unknown":
                item["progress"] = None
                item["detail"] = unknown.get(tid) or item.get("detail") or "Результат неизвестен"
            else:
                try:
                    item["progress"] = min(99.0, max(0.0, float(item.get("progress") or 0)))
                except (TypeError, ValueError):
                    item["progress"] = 0
                item["detail"] = errors.get(tid) or item.get("detail") or "Публикация не выполнена"
    job["publish_status"] = statuses
    job["publish_errors"] = errors
    job["publish_unknown"] = unknown
    job["publish_progress"] = progress


_MONTAGE_RENDER_PROGRESS_CAP = 90.0


# --------------------------------------------------------------------------
# Cancellation of montage jobs ("передумал" support).
#
# Every background coroutine of a montage job is registered here so the cancel
# endpoint can stop it.  Cancelling the asyncio task alone is not enough: the
# render/publish work runs in worker threads blocked in ``Popen.wait()``, so
# the endpoint also sets ``job["cancel_requested"]`` (read by engine
# ``cancel_cb`` hooks) and kills tracked subprocesses via
# ``montage_engine.cancel_job_procs``.  Worker code that observes the flag
# raises ``montage_engine.MontageCancelled`` and the handlers below translate
# it into a ``cancelled`` state instead of ``error``.
# --------------------------------------------------------------------------

MONTAGE_TASKS: dict[str, set] = {}


def _track_montage_task(job_id: str, coro) -> "asyncio.Task":
    task = asyncio.create_task(coro)
    tasks = MONTAGE_TASKS.setdefault(job_id, set())
    tasks.add(task)

    def _forget(done_task: "asyncio.Task") -> None:
        tasks.discard(done_task)
        if not tasks:
            MONTAGE_TASKS.pop(job_id, None)

    task.add_done_callback(_forget)
    return task


def _cancel_requested(job_id: str) -> bool:
    job = jobs.get(job_id)
    return bool(job is not None and job.get("cancel_requested"))


def _montage_cancel_cb(job_id: str):
    """Callback given to engine worker calls; True once the user cancelled."""
    return lambda: _cancel_requested(job_id)


def _mark_montage_cancelled(job_id: str, detail: str = "Отменено пользователем") -> None:
    job = jobs.get(job_id)
    if job is None:
        return
    job["status"] = "cancelled"
    job["phase"] = "cancelled"
    job["stage"] = "cancelled"
    job["detail"] = detail
    job["error"] = detail
    mark_operation_failed(job_id, detail)
    _save_montage_job(job_id)


def _finalize_montage_state(job: dict) -> list:
    """Set the terminal unified-pipeline state on *job* (synchronous).

    A failure of an optional phase (mirrored variant) never hides the
    already-produced main result: it becomes a warning and the job still
    reaches ``done`` so the main video stays usable (spec 4).  Platform
    preparation runs lazily at publish time and never affects render
    readiness.  The overall progress only reaches 100% here (spec 4),
    never at ``main_ready``.  Shared by the async ``_finalize_montage``
    and server-restart recovery.
    """
    params = job.get("params") or {}
    warnings = []
    if params.get("prepare_mirrored") and not mirror_variant_ready(job):
        reason = str((job.get("mirror_variant") or {}).get("error") or "").strip()
        warnings.append("Отражённая версия не подготовлена" + (f": {reason}" if reason else ""))
    job["warnings"] = warnings
    job["phase"] = "ready_with_warnings" if warnings else "ready"
    job["status"] = "done"
    job["progress"] = 100
    job["stage"] = "done"
    job["detail"] = "Готово с предупреждениями" if warnings else "Готово"
    return warnings


def _recover_montage_jobs() -> None:
    root = MONTAGE_WORK.resolve()
    workdirs = sorted(
        (
            path for path in MONTAGE_WORK.iterdir()
            if not path.is_symlink() and path.is_dir() and path.resolve().parent == root
        ),
        key=lambda path: path.stat().st_mtime,
    )[-20:]
    for workdir in workdirs:
        state_path = workdir / "job_state.json"
        if state_path.is_file():
            try:
                job = json.loads(state_path.read_text(encoding="utf-8"))
                if job.get("kind") != "montage":
                    continue
                if str(job.get("id") or "") != workdir.name:
                    continue
                recovered_after_interruption = False
                in_flight = job.get("status") in {"processing", "publishing"}
                if in_flight:
                    recovered_after_interruption = True
                    recovered_id = str(job.get("id") or "")
                    if not job.get("selected_cover_id"):
                        prepared_cover = next((workdir / "covers").glob(".*-instagram.jpg"), None)
                        if prepared_cover:
                            job["selected_cover_id"] = prepared_cover.name[1:].split("-instagram.", 1)[0]
                    message = "Монтаж прерван перезапуском сервера"
                    result_path = (job.get("result") or {}).get("result_video")
                    has_result = bool(result_path) and Path(str(result_path)).is_file()
                    if has_result:
                        # The main render finished.  The unified pipeline may have
                        # been interrupted inside an optional phase (mirrored
                        # variant / platform preparation).  Mark only the
                        # interrupted sub-operation as failed, keep every ready
                        # artefact, and let ``_finalize_montage_state`` decide
                        # readiness with warnings -- never claim full readiness
                        # while a requested phase is incomplete (task: recovery
                        # must be phase-aware, not result-video-only).
                        mirror_state = job.get("mirror_variant")
                        if isinstance(mirror_state, dict) and mirror_state.get("status") == "generating":
                            mirror_msg = "Подготовка отражённой версии прервана перезапуском сервера"
                            mirror_state = dict(mirror_state)
                            mirror_state.update(status="error", error=mirror_msg, detail=mirror_msg)
                            mirror_state["operation"] = failed_snapshot(mirror_state.get("operation"), mirror_msg)
                            job["mirror_variant"] = mirror_state
                        if job.get("prepare_status") == "running":
                            prep_msg = "Подготовка файлов к публикации прервана перезапуском сервера"
                            job["prepare_status"] = "error"
                            job["prepare_error"] = prep_msg
                            prepare_op = job.get("prepare_operation")
                            if isinstance(prepare_op, dict):
                                job["prepare_operation"] = failed_snapshot(prepare_op, prep_msg)
                        operation = job.get("operation")
                        if isinstance(operation, dict) and operation.get("status") in {"running", "pending"}:
                            job["operation"] = failed_snapshot(operation, message)
                        _recover_publish_outcomes(job)
                        _finalize_montage_state(job)
                        if job.get("publish_status") or job.get("publish_errors") or job.get("publish_unknown"):
                            job["detail"] = "Публикация прервана перезапуском сервера"
                        _run_cleanup_safely(_cleanup_render_artifacts, recovered_id, job)
                        if not job.get("publish_errors") and not job.get("publish_unknown"):
                            _run_cleanup_safely(_cleanup_publish_artifacts, recovered_id, job)
                    else:
                        # Render was interrupted before a finished video existed: never
                        # advertise it as completed without a real result (task 1).
                        job["status"] = "failed"
                        job["stage"] = "interrupted"
                        if bool((job.get("params") or {}).get("silence_trim_enabled")):
                            job["detail"] = "Рендер прерван перезапуском. Исходник и разрезы сохранены в задаче."
                            job["error"] = job["detail"]
                            job["error_info"] = {
                                "type": "urn:automontage:silence:render_failed",
                                "title": "Render Interrupted",
                                "detail": job["detail"],
                                "code": "render_failed",
                                "operation": "render",
                                "context": {"stage": "interrupted", "cause": "ffmpeg_exit"},
                                "recovery": "Проверьте задачу и повторите рендер один раз.",
                                "side_effects": "partial",
                                "request_id": uuid.uuid4().hex,
                            }
                        else:
                            job["detail"] = message
                            job["error"] = message
                        try:
                            job["progress"] = min(float(job.get("progress") or 0), 99.0)
                        except (TypeError, ValueError):
                            job["progress"] = 0
                        job["result"] = None
                        operation = job.get("operation")
                        if isinstance(operation, dict) and operation.get("status") != "failed":
                            job["operation"] = failed_snapshot(operation, message)
                if job.get("variants") is None:
                    job["variants"] = {}
                mirror_state = job.get("mirror_variant")
                if isinstance(mirror_state, dict) and mirror_state.get("status") == "generating":
                    mirror_msg = "Подготовка отражённой версии прервана перезапуском сервера"
                    mirror_state = dict(mirror_state)
                    mirror_state.update(status="error", error=mirror_msg, detail=mirror_msg)
                    mirror_state["operation"] = failed_snapshot(mirror_state.get("operation"), mirror_msg)
                    job["mirror_variant"] = mirror_state
                    recovered_after_interruption = True
                recovered_id = str(job["id"])
                jobs[recovered_id] = job
                if recovered_after_interruption:
                    _save_montage_job(recovered_id)
                if not in_flight:
                    _run_cleanup_safely(_cleanup_render_artifacts, recovered_id, job)
                    if job.get("stage") == "published" and not job.get("publish_errors") and not job.get("publish_unknown"):
                        _run_cleanup_safely(_cleanup_publish_artifacts, recovered_id, job)
                continue
            except Exception:
                pass

        # Older jobs predate on-disk state; recover enough metadata to download or republish them.
        result_video = workdir / "final.mp4"
        if not result_video.is_file():
            continue
        sources = list(workdir.glob("project_video.*")) + list(workdir.glob("source.*"))
        transcripts = list(workdir.glob("*_words.srt"))
        if not sources or not transcripts:
            continue
        subtitle_mode = "phrases" if (workdir / "sub_phrases.mp4").is_file() else "words"
        timeline = {"subtitle_mode": subtitle_mode, "subtitle_position": "custom", "zoom": []}
        try:
            zoom = json.loads((workdir / "zoom_timeline_config.json").read_text(encoding="utf-8"))
            timeline["zoom"] = (zoom.get("render") or {}).get("zoom", {}).get("timeline") or []
        except Exception:
            pass
        try:
            duration = float(montage_engine.ffprobe(result_video).get("format", {}).get("duration") or 0)
        except Exception:
            duration = 0
        covers = []
        try:
            covers = json.loads((workdir / "covers" / "manifest.json").read_text(encoding="utf-8")).get("covers") or []
        except Exception:
            pass
        instagram_attempted = any((workdir / "staging").glob("*_normal_*.mp4"))
        prepared_cover = next((workdir / "covers").glob(".*-instagram.jpg"), None)
        selected_cover_id = prepared_cover.name[1:].split("-instagram.", 1)[0] if prepared_cover else ""
        job_id = workdir.name
        jobs[job_id] = {
            "id": job_id,
            "original_name": sources[0].name,
            "video_path": str(sources[0]),
            "srt_text": transcripts[0].read_text(encoding="utf-8", errors="replace"),
            "transcript_job_id": None,
            "project_id": None,
            "kind": "montage",
            "status": "done",
            "progress": 100,
            "stage": "done",
            "detail": "Готовый ролик восстановлен после перезапуска",
            "result": {
                "job_id": job_id,
                "result_video": str(result_video.resolve()),
                "preview_video": str((workdir / "preview.mp4").resolve()),
                "duration": duration,
                "subtitle_mode": subtitle_mode,
                "timeline": timeline,
            },
            "params": {"subtitle_mode": subtitle_mode, "mirror_horizontal": False},
            "publish": {} if instagram_attempted else None,
            "publish_errors": ({"instagram_feed": "Предыдущая попытка Instagram не завершилась; можно повторить"}
                               if instagram_attempted else {}),
            "publish_progress": None,
            "error": None,
            "created_at": datetime.fromtimestamp(workdir.stat().st_mtime).isoformat(),
            "covers": covers,
            "selected_cover_id": selected_cover_id,
            "cover_status": "done" if covers else "idle",
            "cover_progress": 100 if covers else 0,
            "cover_detail": "",
            "cover_error": None,
            "operation": None,
            "variants": {},
            "mirror_variant": None,
            "publish_prep": {},
            "prepare_operation": None,
            "prepare_status": "idle",
            "prepare_error": None,
        }
        _save_montage_job(job_id)
        _run_cleanup_safely(_cleanup_render_artifacts, job_id, jobs[job_id])

    # Master retention after restart: expired masters of fully settled jobs
    # are pruned here, live ones survive and stay available for the next
    # platform preparation.
    _maybe_retire_masters()


_recover_montage_jobs()
_recover_transcription_projects()


def _threaded_progress(loop, job_id):
    def progress(pct, stage, detail=""):
        asyncio.run_coroutine_threadsafe(
            broadcast_progress(job_id, pct, stage, detail), loop
        )
    return progress


async def _montage_announce(job_id: str, text: str):
    q = job_queues.get(job_id)
    if q:
        try:
            await q.put({"type": "log", "message": text})
        except Exception:
            pass


async def _finalize_montage(job_id: str) -> None:
    """Finish the unified montage process (``ready`` / ``ready_with_warnings``).

    The main MP4 and the platform artefacts are already produced by the
    preceding phases.  A failure of an optional phase (mirrored variant or
    platform preparation) never hides the main result: it becomes a warning and
    the job still reaches ``done`` so the main video stays usable (spec 4).
    """
    job = jobs.get(job_id)
    if not job or job.get("kind") != "montage":
        return
    if job.get("status") == "error":
        return
    warnings = _finalize_montage_state(job)
    _save_montage_job(job_id)
    await broadcast_progress(job_id, 100, "done", job["detail"])
    if warnings:
        await _montage_announce(job_id, "Готово с предупреждениями: " + "; ".join(warnings))
    else:
        await _montage_announce(job_id, "Монтаж полностью готов")


async def process_montage(job_id: str, params: dict):
    job = jobs[job_id]
    loop = asyncio.get_running_loop()
    try:
        job["status"] = "processing"
        raw_progress = _threaded_progress(loop, job_id)

        def progress(pct, stage, detail=""):
            # Reserve headroom for the automatic phases that follow the main
            # render (mirroring / platform preparation): the overall job only
            # reaches 100% in ``_finalize_montage`` (spec 4).
            capped = min(max(float(pct or 0), 0.0), _MONTAGE_RENDER_PROGRESS_CAP)
            raw_progress(capped, stage, detail)

        await _montage_announce(job_id, "Проверка установленных компонентов...")
        status = montage_engine.montage_status()

        video = Path(job["video_path"])
        if not video.exists():
            raise RuntimeError("Видеофайл не найден")

        srt_text: str = job["srt_text"] or ""
        transcript_job = job.get("transcript_job_id")
        if not srt_text and transcript_job and transcript_job in jobs:
            source = jobs[transcript_job]
            if source.get("status") == "done" and source.get("result"):
                srt_text = montage_engine.build_words_srt(source["result"])

        if not srt_text.strip():
            raise RuntimeError("Нет текста: загрузите SRT или укажите готовую транскрибацию")

        censor_srt_text = srt_text
        if params.get("blur_banned_words"):
            source_job = jobs.get(transcript_job) or {}
            exact_word_srt = montage_engine.build_word_timing_srt(source_job.get("result") or [])
            if exact_word_srt.strip():
                censor_srt_text = exact_word_srt

        if params.get("blur_banned_words") and montage_engine.needs_word_alignment(censor_srt_text):
            await _montage_announce(job_id, "Уточнение таймкодов слов для цензуры...")
            source_job = jobs.get(transcript_job) or {}
            source_params = source_job.get("params") or {}
            model_name = str(source_params.get("model") or "small")
            language = str(source_params.get("language") or "ru")

            def alignment_progress(ratio: float) -> None:
                progress(2 + ratio * 4, "audio_censor", f"Таймкоды слов: {ratio * 100:.0f}%")

            aligned = await asyncio.to_thread(
                run_whisper_job,
                model_name,
                str(video),
                lambda *_: None,
                lambda: None,
                lambda: None,
                alignment_progress,
                language=language,
                word_timestamps=True,
            )
            aligned_srt = montage_engine.build_word_timing_srt(aligned.get("segments") or [])
            if not aligned_srt.strip() or montage_engine.needs_word_alignment(aligned_srt):
                raise RuntimeError("Не удалось определить точные таймкоды нежелательных слов")
            censor_srt_text = aligned_srt

        render_params = dict(params)
        if params.get("blur_banned_words"):
            render_params["audio_censor_srt"] = censor_srt_text
            job["audio_censor_srt"] = censor_srt_text
            _save_montage_job(job_id)

        needs_face_model = montage_engine.resolve_camera_options(params)["do_track"]
        if needs_face_model and not status["face_model"]:
            await broadcast_progress(job_id, 3, "model", "Загрузка модели face_landmarker.task...")
            download_face_model(job_id, progress)
            status = montage_engine.montage_status()

        workdir = MONTAGE_WORK / job_id
        workdir.mkdir(parents=True, exist_ok=True)

        def report(snapshot: dict) -> None:
            asyncio.run_coroutine_threadsafe(broadcast_operation(job_id, snapshot), loop)

        await _montage_announce(job_id, "Запуск рендера...")
        meta = await asyncio.to_thread(
            montage_engine.render_montage,
            job_id=job_id,
            workdir=workdir,
            source_video=video,
            srt_text=srt_text,
            options=render_params,
            progress_cb=progress,
            report_cb=report,
            cancel_cb=_montage_cancel_cb(job_id),
            log_cb=lambda message: asyncio.run_coroutine_threadsafe(
                _montage_announce(job_id, message), loop
            ),
        )

        # The montage operation is not finished here: the mirrored variant and
        # the platform-ready artefacts are produced by the same unified process
        # (see ``_run_montage_then_variant``).  ``status`` stays ``processing``
        # until every automatic phase is done, so the UI never reports "done"
        # prematurely (spec section 4).  The main MP4 is already available.
        job["status"] = "processing"
        job["phase"] = "main_ready"
        # The overall job must NOT reach 100% while the mirrored variant and the
        # platform preparation still run; only ``_finalize_montage`` may do that
        # (spec 4).  The internal main-render stage may still show 100%.
        job["progress"] = _MONTAGE_RENDER_PROGRESS_CAP
        job["stage"] = "main_ready"
        job["detail"] = "Основной ролик готов"
        job["result"] = meta
        _save_montage_job(job_id)
        _run_cleanup_safely(_cleanup_render_artifacts, job_id, job)
        await broadcast_progress(job_id, _MONTAGE_RENDER_PROGRESS_CAP, "main_ready", "Основной ролик готов")
        await _montage_announce(job_id, f"Основной ролик готов: {Path(meta['result_video']).name} ({(os.path.getsize(meta['result_video']) >> 20)} МБ)")
    except Exception as e:
        if _cancel_requested(job_id):
            _mark_montage_cancelled(job_id)
            print(f"[montage] job {job_id} cancelled by user", file=sys.stderr)
            await broadcast_progress(job_id, job.get("progress", 0), "cancelled", "Отменено пользователем")
            return
        failure_stage = str(job.get("stage") or "render")
        job["status"] = "error"
        job["phase"] = "failed"
        job["stage"] = "error"
        if bool(params.get("silence_trim_enabled")):
            error_detail = "Монтаж не выполнен. Исходник и разрезы сохранены в задаче для проверки и повтора."
            job["error"] = error_detail
            job["detail"] = error_detail
            cause = (
                "media_read_failed" if failure_stage == "prepare"
                else "workspace_capacity" if "disk" in str(e).lower() or "space" in str(e).lower()
                else "ffmpeg_exit" if failure_stage in {"silence_trim", "finalize", "preview"}
                else "render_stage_failed"
            )
            recovery = {
                "media_read_failed": "Проверьте выбранный источник и повторите анализ.",
                "workspace_capacity": "Освободите место на диске и повторите рендер один раз.",
                "ffmpeg_exit": "Проверьте источник и свободное место; затем повторите рендер один раз.",
                "render_stage_failed": "Проверьте указанный этап, настройки и журнал задачи; затем повторите один раз.",
            }[cause]
            job["error_info"] = {
                "type": "urn:automontage:silence:render_failed",
                "title": "Render Failed",
                "detail": error_detail,
                "code": "render_failed",
                "operation": "render",
                "context": {
                    "stage": failure_stage,
                    "cause": cause,
                },
                "recovery": recovery,
                "side_effects": "partial",
                "request_id": uuid.uuid4().hex,
            }
        else:
            job["error"] = str(e)
            job["detail"] = str(e)
            job.pop("error_info", None)
        operation = mark_operation_failed(job_id, str(job["error"]))
        _save_montage_job(job_id)
        print(f"[montage] job {job_id} failed: {traceback.format_exc()}", file=sys.stderr)
        if operation is not None:
            await broadcast_progress(job_id, operation.get("progress", 0), "error", str(job["error"]))
            await broadcast_operation(job_id, operation)
        else:
            await broadcast_progress(job_id, 0, "error", str(job["error"]))


async def _store_project_covers(job_id: str) -> None:
    job = jobs[job_id]
    project_ids = [job.get("project_id"), f"p_{job_id}"]
    async with PROJECTS_LOCK:
        for project_id in project_ids:
            if project_id and project_id in PROJECTS:
                PROJECTS[project_id]["covers"] = list(job.get("covers") or [])
                PROJECTS[project_id]["covers_dir"] = str(MONTAGE_WORK / job_id / "covers")
        _save_projects()


async def process_cover_suggestions(job_id: str) -> None:
    job = jobs[job_id]
    loop = asyncio.get_running_loop()
    job["cover_status"] = "generating"
    job["cover_progress"] = 0
    job["cover_detail"] = "Подготовка стоп-кадров"
    job["cover_error"] = None

    def progress(percent: float, detail: str) -> None:
        def update() -> None:
            job["cover_progress"] = round(max(0, min(100, percent)), 1)
            job["cover_detail"] = detail
        loop.call_soon_threadsafe(update)

    try:
        items = await asyncio.to_thread(
            cover_generator.generate_suggested_covers,
            job_id,
            Path(job["video_path"]),
            str(job.get("srt_text") or ""),
            MONTAGE_WORK / job_id / "covers",
            progress,
            str(job.get("cover_title") or ""),
            str(((job.get("params") or {}).get("ai_model")) or ""),
        )
        job["covers"] = items
        job["cover_status"] = "done"
        job["cover_progress"] = 100
        job["cover_detail"] = f"Обложки готовы: {len(items)}"
        await _store_project_covers(job_id)
        _save_montage_job(job_id)
    except Exception as exc:
        if _cancel_requested(job_id):
            job["cover_status"] = "cancelled"
            job["cover_error"] = "Отменено пользователем"
            job["cover_detail"] = "Отменено пользователем"
            _save_montage_job(job_id)
            return
        job["cover_status"] = "error"
        job["cover_error"] = str(exc)
        job["cover_detail"] = str(exc)
        _save_montage_job(job_id)
        print(f"[covers] job {job_id} failed: {traceback.format_exc()}", file=sys.stderr)


async def process_custom_cover(job_id: str, timestamp: float, title: str) -> None:
    job = jobs[job_id]
    loop = asyncio.get_running_loop()
    job["cover_status"] = "generating"
    job["cover_progress"] = 0
    job["cover_detail"] = "Создание обложки из стоп-кадра"
    job["cover_error"] = None

    def progress(percent: float, detail: str) -> None:
        def update() -> None:
            job.update(cover_progress=round(percent, 1), cover_detail=detail)
        loop.call_soon_threadsafe(update)

    try:
        video = Path((job.get("result") or {}).get("result_video") or job["video_path"])
        items = await asyncio.to_thread(
            cover_generator.generate_custom_cover,
            video, timestamp, title, MONTAGE_WORK / job_id / "covers", progress,
        )
        job["covers"] = items
        job["cover_status"] = "done"
        job["cover_progress"] = 100
        job["cover_detail"] = "Новая обложка готова"
        await _store_project_covers(job_id)
        _save_montage_job(job_id)
    except Exception as exc:
        if _cancel_requested(job_id):
            job["cover_status"] = "cancelled"
            job["cover_error"] = "Отменено пользователем"
            job["cover_detail"] = "Отменено пользователем"
            _save_montage_job(job_id)
            return
        job["cover_status"] = "error"
        job["cover_error"] = str(exc)
        job["cover_detail"] = str(exc)
        _save_montage_job(job_id)


def _mark_publish_cancelled(job_id: str, targets: list) -> None:
    """Mark an interrupted publication as cancelled, keeping finished work."""
    job = jobs.get(job_id)
    if job is None:
        return
    for target in targets or []:
        target_id = str(target.get("id") or target.get("kind") or "")
        if not target_id:
            continue
        item = (job.get("publish_progress") or {}).get(target_id)
        if item is not None and item.get("status") in {"queued", "preparing", "publishing"}:
            item.update(status="cancelled", progress=item.get("progress"),
                        detail="Отменено пользователем")
        if (job.get("publish_status") or {}).get(target_id) in {None, "queued", "preparing", "publishing"}:
            job["publish_status"][target_id] = "failed"
            job["publish_errors"][target_id] = "Отменено пользователем"
    job["status"] = "done"
    job["stage"] = "cancelled"
    job["detail"] = "Публикация отменена пользователем"
    job["error"] = None
    _save_montage_job(job_id)


async def process_publish(job_id: str, targets: list, publish_lock: Optional[asyncio.Lock] = None):
    job = jobs[job_id]
    loop = asyncio.get_running_loop()
    try:
        previous_publish = dict(job.get("publish") or {})
        _initialize_publish_attempt(job, targets)
        _save_montage_job(job_id)
        progress = _threaded_progress(loop, job_id)
        workdir = MONTAGE_WORK / job_id
        source_video = Path(job["result"]["result_video"])
        mirrored_source_video = None

        # Publishing never renders/encodes (spec 9.1).  The mirrored variant must
        # already exist on the job (prepared by the montage pipeline).  If it is
        # missing/stale we fail only the mirrored targets with a clear reason
        # instead of re-rendering here.
        mirrored_targets = [target for target in targets if target.get("mirrored")]
        if mirrored_targets:
            if mirror_variant_ready(job):
                mirrored_source_video = Path(_mirror_variant_entry(job)["path"])
                for target in mirrored_targets:
                    job["publish_progress"][target["id"]].update(
                        status="preparing", detail="Отражённая версия готова"
                    )
            else:
                message = (
                    "Отражённая версия не готова. Включите «Создать отражённую версию» "
                    "при монтаже и повторите."
                )
                for target in mirrored_targets:
                    target["preparation_error"] = message
                await _montage_announce(job_id, message)

        # Publishing only sends ready files: resolve the artefacts produced by
        # the preparation stage.  Anything missing is reported per target
        # instead of transcoding here (spec: publish must not run FFmpeg).
        prepared_targets, missing_targets = await asyncio.to_thread(_publish_readiness, job, targets)
        for item in missing_targets:
            target_id = item.get("target")
            if target_id and target_id in job["publish_progress"]:
                job["publish_progress"][target_id].update(
                    status="error", progress=0,
                    detail=_preparation_error_message(item),
                )
        for target in targets:
            target_id = str(target.get("id") or target.get("kind") or "")
            if target_id and target_id not in prepared_targets:
                target["preparation_error"] = _preparation_error_message(target)
        if missing_targets:
            labels = ", ".join(str(item.get("label") or item.get("target")) for item in missing_targets)
            await _montage_announce(job_id, f"Не подготовлены к публикации: {labels}")

        await _montage_announce(job_id, "Публикация...")

        def publish_progress(percent: float, stage: str, detail: str) -> None:
            progress(percent, stage, detail)

        def target_progress(target_id: str, percent: float, detail: str, status: str) -> None:
            def update() -> None:
                item = job["publish_progress"].get(target_id)
                if item is None:
                    return
                previous = item.get("status")
                if status == "unknown":
                    safe_percent = None
                elif status == "done":
                    safe_percent = 100.0
                else:
                    try:
                        safe_percent = min(99.0, max(0.0, float(percent or 0)))
                    except (TypeError, ValueError):
                        safe_percent = 0.0
                item.update(
                    progress=None if safe_percent is None else round(safe_percent, 1),
                    detail=detail, status=status,
                )
                if status == "done":
                    job["publish_status"][target_id] = "published"
                    job["publish_errors"].pop(target_id, None)
                    job["publish_unknown"].pop(target_id, None)
                elif status == "error":
                    job["publish_status"][target_id] = "failed"
                    job["publish_errors"][target_id] = detail
                    job["publish_unknown"].pop(target_id, None)
                elif status == "unknown":
                    job["publish_status"][target_id] = "unknown"
                    job["publish_unknown"][target_id] = detail
                    job["publish_errors"].pop(target_id, None)
                started = (status == "publishing" and previous == "queued")
                if started:
                    job["publish_started"][target_id] = True
                if status in {"done", "error", "unknown"} or started:
                    _save_montage_job(job_id)
            loop.call_soon_threadsafe(update)

        def confirmed_publish(target_id: str, result_data: dict) -> None:
            def update() -> None:
                job["publish_status"][target_id] = "published"
                job["publish"][target_id] = dict(result_data)
                job["publish_errors"].pop(target_id, None)
                job["publish_unknown"].pop(target_id, None)
                _save_montage_job(job_id)
            loop.call_soon_threadsafe(update)

        def target_step(target_id: str, step_key: str, step_label: str, status: str, detail: str) -> None:
            def update() -> None:
                item = job["publish_progress"].get(target_id)
                if item is None:
                    return
                steps = list(item.get("steps") or [])
                entry = next((s for s in steps if s.get("key") == step_key), None)
                if entry is None:
                    entry = {"key": step_key, "label": step_label, "status": status, "detail": detail or ""}
                    steps.append(entry)
                else:
                    entry["status"] = status
                    entry["label"] = step_label or entry.get("label")
                    if detail:
                        entry["detail"] = detail
                item["steps"] = steps
                _save_montage_job(job_id)
            loop.call_soon_threadsafe(update)

        result = await asyncio.to_thread(
            montage_engine.publish_job,
            job_id=job_id,
            workdir=workdir,
            source_video=source_video,
            mirrored_source_video=mirrored_source_video,
            original_video=Path(job["video_path"]),
            transcript_text=str(job.get("srt_text") or ""),
            targets=targets,
            prepared_targets=prepared_targets,
            cancel_cb=_montage_cancel_cb(job_id),
            progress_cb=publish_progress,
            target_progress_cb=target_progress,
            confirmed_cb=confirmed_publish,
            target_step_cb=target_step,
            log_cb=lambda message: asyncio.run_coroutine_threadsafe(
                _montage_announce(job_id, message), loop
            ),
        )
        publish_results = dict(result.get("results") or {})
        engine_errors = dict(result.get("errors") or {})
        expected_targets = {str(target.get("id") or target.get("kind") or "") for target in targets}
        for missing in expected_targets - set(publish_results) - set(engine_errors):
            if missing:
                engine_errors[missing] = "Публикация не вернула результат"
        for target_id, error_text in engine_errors.items():
            if str(error_text).startswith("AMBIGUOUS_MEDIA_PUBLISH"):
                job["publish_status"][target_id] = "unknown"
                job["publish_unknown"][target_id] = error_text
                job["publish_errors"].pop(target_id, None)
            else:
                job["publish_status"][target_id] = "failed"
                job["publish_errors"][target_id] = error_text
        for target_id in publish_results:
            job["publish_status"][target_id] = "published"
            job["publish_errors"].pop(target_id, None)
            job["publish_unknown"].pop(target_id, None)
        for target_id in expected_targets:
            if job["publish_status"].get(target_id) in {None, "queued", "preparing", "publishing"}:
                job["publish_status"][target_id] = "failed"
                job["publish_errors"].setdefault(target_id, "Публикация не вернула результат")
        terminal_status = {"published": "done", "failed": "error", "unknown": "unknown"}
        for target_id in expected_targets:
            item = job["publish_progress"].get(target_id)
            if item is None:
                continue
            state = job["publish_status"].get(target_id) or "failed"
            item["status"] = terminal_status.get(state, "error")
            if state == "published":
                item.update(progress=100, detail="Опубликовано")
            elif state == "unknown":
                item.update(progress=None, detail=job["publish_unknown"].get(target_id) or "Результат публикации неизвестен")
            else:
                try:
                    failed_progress = min(99.0, max(0.0, float(item.get("progress") or 0)))
                except (TypeError, ValueError):
                    failed_progress = 0.0
                item.update(progress=failed_progress, detail=item.get("detail")
                            or job["publish_errors"].get(target_id) or "Публикация не выполнена")
        current_errors = {k: v for k, v in job["publish_errors"].items() if k in expected_targets}
        current_unknown = {k: v for k, v in job["publish_unknown"].items() if k in expected_targets}
        job["status"] = "done"
        job["progress"] = 100
        job["stage"] = "published"
        job["publish"] = {**previous_publish, **publish_results}
        if current_errors and current_unknown:
            detail = "Публикация завершена с ошибками; часть результатов требует ручной проверки"
        elif current_unknown:
            detail = "Публикация завершена; часть результатов требует ручной проверки"
        elif current_errors:
            detail = "Публикация завершена с ошибками"
        else:
            detail = "Публикация завершена"
        job["detail"] = detail
        _save_montage_job(job_id)
        if not current_errors and not current_unknown:
            _run_cleanup_safely(_cleanup_publish_artifacts, job_id, job)
        await broadcast_progress(job_id, 100, "published", detail)
    except (asyncio.CancelledError, montage_engine.MontageCancelled):
        if _cancel_requested(job_id):
            _mark_publish_cancelled(job_id, targets)
            await broadcast_progress(job_id, job.get("progress", 0), "cancelled", "Публикация отменена пользователем")
            return
        raise
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        if job.get("last_publish_targets"):
            _recover_publish_outcomes(job)
        _save_montage_job(job_id)
        await broadcast_progress(job_id, 0, "error", str(e))
    finally:
        # Release the per-job lock on success, on error and on cancellation so
        # a retry is never blocked by a stale lock.
        if publish_lock is not None and publish_lock.locked():
            publish_lock.release()


async def _prepare_then_publish(job_id: str, targets: list, publish_lock,
                                cover_path: Optional[str] = None, cover_id: str = "",
                                prepare_ids: Optional[set] = None,
                                attempt_started_cb=None) -> None:
    """Auto-prepare missing local artefacts, then publish (spec 6).

    Runs in the background so the HTTP request returns immediately.  Preparation
    failures never stop publishing: ``process_publish`` reports the still-missing
    targets per target with a clear reason.  The lock is released here if the
    preparation step is cancelled before ``process_publish`` takes over.
    """
    try:
        if prepare_ids:
            prep_targets = [t for t in targets if t.get("id") in prepare_ids]
            await _montage_announce(job_id, "Подготовка файлов к публикации перед отправкой...")
            try:
                await process_prepare_publish(
                    job_id, prep_targets, cover_path=cover_path, cover_id=cover_id
                )
            except Exception as exc:
                job = jobs.get(job_id)
                if job is not None:
                    job["prepare_status"] = "error"
                    job["prepare_error"] = str(exc)
                    _save_montage_job(job_id)
                await _montage_announce(job_id, f"Не удалось подготовить файлы: {exc}")
    except BaseException:
        if publish_lock is not None and publish_lock.locked():
            publish_lock.release()
        raise
    if attempt_started_cb is not None:
        callback_result = attempt_started_cb()
        if asyncio.iscoroutine(callback_result):
            await callback_result
    await process_publish(job_id, targets, publish_lock=publish_lock)


def _publication_targets_for_queue_item(queue_item: dict, job: dict) -> tuple[list, Optional[str]]:
    targets = []
    cover_id = str(queue_item.get("cover_id") or "")
    cover_path = None
    if cover_id:
        cover = next((item for item in job.get("covers") or [] if item.get("id") == cover_id), None)
        if cover is None:
            raise RuntimeError("Выбранная обложка больше не найдена")
        candidate = MONTAGE_WORK / str(job["id"]) / "covers" / Path(str(cover.get("file") or "")).name
        if not candidate.is_file():
            raise RuntimeError("Файл выбранной обложки недоступен")
        cover_path = str(candidate)

    for target_id in queue_item.get("target_ids") or []:
        spec = _PUBLISH_TARGET_SPECS.get(str(target_id))
        if spec is None:
            raise RuntimeError(f"Неизвестная цель публикации: {target_id}")
        kind, mirrored, label = spec
        platform = "youtube" if kind == "youtube" else "instagram"
        if platform != queue_item.get("platform"):
            raise RuntimeError("Цель публикации не совпадает с сетью очереди")
        target = {"id": str(target_id), "kind": kind, "mirrored": mirrored, "label": label}
        if cover_path:
            target["cover_path"] = cover_path
        if kind == "youtube":
            target["title"] = str(queue_item.get("youtube_title") or queue_item.get("display_title") or "")
        else:
            caption = str(queue_item.get("instagram_caption") or "")
            target["text"] = caption
            target["caption"] = caption
        targets.append(target)
    if not targets:
        raise RuntimeError("В записи очереди нет целей публикации")
    return targets, cover_path


def _publication_queue_terminal_status(job: dict, targets: list) -> tuple[str, str]:
    target_ids = [str(target.get("id") or "") for target in targets]
    statuses = job.get("publish_status") or {}
    values = [statuses.get(target_id) for target_id in target_ids]
    if values and all(value == "published" for value in values):
        return "published", ""
    if any(value == "unknown" for value in values):
        errors = job.get("publish_unknown") or {}
        return "unknown", "; ".join(str(errors.get(tid) or "") for tid in target_ids if statuses.get(tid) == "unknown")
    if job.get("stage") == "cancelled":
        return "cancelled", "Публикация отменена пользователем"
    errors = job.get("publish_errors") or {}
    detail = "; ".join(str(errors.get(tid) or "") for tid in target_ids if statuses.get(tid) != "published")
    return "failed", detail or "Не все цели публикации завершились успешно"


async def _run_publication_queue_item(queue_item: dict) -> None:
    queue_id = str(queue_item["id"])
    job_id = str(queue_item["job_id"])
    job = jobs.get(job_id)
    if job is None or job.get("kind") != "montage" or not job.get("result"):
        await asyncio.to_thread(
            publication_queue.set_status, queue_id, "failed",
            "Исходный монтаж больше недоступен",
        )
        return

    publish_lock = _publish_lock(job_id)
    lock_acquired = False
    try:
        targets, cover_path = _publication_targets_for_queue_item(queue_item, job)
        await publish_lock.acquire()
        lock_acquired = True

        _, missing_targets = await asyncio.to_thread(_publish_readiness, job, targets)
        prepare_ids = {
            item.get("target") for item in missing_targets
            if item.get("reason") in {"video_not_ready", "cover_not_ready"}
        }
        if prepare_ids:
            job["prepare_operation"] = None
            job["prepare_status"] = "running"
            job["prepare_error"] = None
        mirror_blocked = [item for item in missing_targets if item.get("reason") == "mirror_not_ready"]
        for item in mirror_blocked:
            target_id = item.get("target")
            target = next((target for target in targets if target.get("id") == target_id), None)
            if target is not None:
                target["preparation_error"] = _preparation_error_message(item)

        job["selected_cover_id"] = str(queue_item.get("cover_id") or "")
        _initialize_publish_attempt(job, targets, preparing_ids=prepare_ids)
        _save_montage_job(job_id)
        await asyncio.to_thread(
            publication_queue.set_status, queue_id,
            "preparing" if prepare_ids else "publishing", "",
        )

        publish_task = _track_montage_task(
            job_id,
            _prepare_then_publish(
                job_id, targets, publish_lock,
                cover_path=cover_path,
                cover_id=str(queue_item.get("cover_id") or ""),
                prepare_ids=prepare_ids,
                attempt_started_cb=lambda: asyncio.to_thread(
                    publication_queue.record_attempt, queue_id
                ),
            ),
        )
        await publish_task
        lock_acquired = False  # _prepare_then_publish/process_publish releases it.
        status, error = _publication_queue_terminal_status(job, targets)
        await asyncio.to_thread(
            publication_queue.set_progress, queue_id, job.get("publish_progress") or {}
        )
        await asyncio.to_thread(publication_queue.set_status, queue_id, status, error)
    except asyncio.CancelledError:
        await asyncio.to_thread(
            publication_queue.set_status, queue_id, "unknown",
            "Сервер остановил задачу; проверьте результат публикации вручную",
        )
        raise
    except Exception as exc:
        cancelled = job.get("stage") == "cancelled" or bool(job.get("cancel_requested"))
        await asyncio.to_thread(
            publication_queue.set_status, queue_id,
            "cancelled" if cancelled else "failed", str(exc),
        )
    finally:
        if lock_acquired and publish_lock.locked():
            publish_lock.release()


async def _publication_queue_worker() -> None:
    while True:
        try:
            paused = await asyncio.to_thread(publication_queue.is_paused)
        except (sqlite3.Error, OSError):
            paused = True
        try:
            item = await asyncio.to_thread(publication_queue.claim_next_due, force_only=paused)
        except (sqlite3.Error, OSError):
            item = None
        if item is not None:
            try:
                await _run_publication_queue_item(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await asyncio.to_thread(
                    publication_queue.set_status, item["id"], "unknown",
                    f"Ошибка обработчика очереди: {exc}",
                )
            continue
        wake = PUBLICATION_QUEUE_WAKE
        if wake is None:
            await asyncio.sleep(2)
            continue
        try:
            await asyncio.wait_for(wake.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
        wake.clear()


def _wake_publication_queue() -> None:
    loop = PUBLICATION_QUEUE_LOOP
    wake = PUBLICATION_QUEUE_WAKE
    if loop is not None and wake is not None and not loop.is_closed():
        loop.call_soon_threadsafe(wake.set)


def _publication_queue_worker_enabled(modules=None) -> bool:
    if os.environ.get("AUTOMONTAGE_DISABLE_PUBLICATION_QUEUE_WORKER") == "1":
        return False
    if os.environ.get("AUTOMONTAGE_ENABLE_PUBLICATION_QUEUE_WORKER") == "1":
        return True
    loaded_modules = sys.modules if modules is None else modules
    return "uvicorn" in loaded_modules


@app.on_event("startup")
async def _start_publication_queue_worker() -> None:
    global PUBLICATION_QUEUE_WAKE, PUBLICATION_QUEUE_TASK, PUBLICATION_QUEUE_LOOP
    # TestClient starts ASGI lifespan too; only start the dispatcher in the
    # production Uvicorn process, not in the test runner that imported app.py.
    if not _publication_queue_worker_enabled():
        return
    try:
        await asyncio.to_thread(publication_queue.recover_inflight)
    except (sqlite3.Error, OSError) as exc:
        print(f"[publication-queue] Storage recovery failed: {exc}", file=sys.stderr)
    if PUBLICATION_QUEUE_TASK is not None and not PUBLICATION_QUEUE_TASK.done():
        return
    PUBLICATION_QUEUE_LOOP = asyncio.get_running_loop()
    PUBLICATION_QUEUE_WAKE = asyncio.Event()
    PUBLICATION_QUEUE_TASK = asyncio.create_task(_publication_queue_worker())


@app.on_event("shutdown")
async def _stop_publication_queue_worker() -> None:
    global PUBLICATION_QUEUE_WAKE, PUBLICATION_QUEUE_TASK, PUBLICATION_QUEUE_LOOP
    task = PUBLICATION_QUEUE_TASK
    PUBLICATION_QUEUE_TASK = None
    PUBLICATION_QUEUE_WAKE = None
    PUBLICATION_QUEUE_LOOP = None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
def download_face_model(job_id: str, progress):
    model = montage_engine.MODELS_DIR / "face_landmarker.task"
    if model.exists():
        return
    montage_engine.MODELS_DIR.mkdir(parents=True, exist_ok=True)
    url = "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task"
    part = str(model) + ".part"
    start = os.path.getsize(part) if os.path.exists(part) else 0
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    if start:
        req.add_header("Range", f"bytes={start}-")
    with urllib.request.urlopen(req, timeout=60) as resp, open(part, "ab") as out:
        total = start + int(resp.headers.get("Content-Length") or 0)
        downloaded = start
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            downloaded += len(chunk)
            if total > 0:
                pct = int(downloaded * 100 / total)
                progress(pct * 0.03, "model", f"Скачивание модели: {pct}%")
    os.replace(part, str(model))


_AI_MODELS_CACHE = {"models": [], "ts": 0.0, "lock": threading.Lock()}


def _parse_opencode_models(text: str) -> list:
    models = []
    for line in (text or "").splitlines():
        line = line.strip()
        if line.count("/") == 1 and not any(ch.isspace() for ch in line) and len(line) <= 100:
            models.append(line)
    return models


def montage_ai_models() -> list:
    """List models currently offered by the opencode CLI (cached 60 s)."""
    now = time.time()
    with _AI_MODELS_CACHE["lock"]:
        if _AI_MODELS_CACHE["models"] and now - _AI_MODELS_CACHE["ts"] < 60:
            return list(_AI_MODELS_CACHE["models"])
        models = []
        try:
            out = subprocess.run(
                [montage_engine.opencode_bin(), "models"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15,
            )
            if out.returncode == 0:
                models = _parse_opencode_models(out.stdout)
        except Exception:
            models = []
        _AI_MODELS_CACHE["models"] = models
        _AI_MODELS_CACHE["ts"] = now
        return list(models)


def _normalize_ai_model(raw: Any) -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if len(value) > 100 or value.count("/") != 1 or any(ch.isspace() for ch in value):
        raise HTTPException(400, "Некорректное имя модели ИИ")
    return value


@app.get("/api/ai/models")
async def ai_models_list():
    load_secrets_env()
    models = montage_ai_models()
    default_model = os.getenv("BIGPICKLE_MODEL", "opencode/big-pickle").strip()
    return {"available": bool(models), "models": models, "default": default_model}


@app.get("/api/montage/status")
async def montage_status():
    load_secrets_env()
    status = montage_engine.montage_status()
    entries = [
        {"id": j["id"], "name": j["original_name"], "status": j["status"],
         "phase": j.get("phase"), "warnings": list(j.get("warnings") or []),
         "created_at": j.get("created_at", ""),
         "project_id": j.get("project_id"), "has_result": bool(j.get("result"))}
        for j in jobs.values() if j.get("kind") == "montage"
    ]
    entries.sort(key=lambda item: item["created_at"])
    return {**status, "jobs": entries}


@app.post("/api/montage/fonts/download")
async def download_comfortaa_font():
    target = montage_engine.FONTS_DIR / "Comfortaa.ttf"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.stat().st_size > 0:
        return {"font": {"id": target.name, "name": "Comfortaa", "path": str(target)}}
    try:
        await asyncio.to_thread(
            urllib.request.urlretrieve,
            "https://github.com/google/fonts/raw/main/ofl/comfortaa/Comfortaa%5Bwght%5D.ttf",
            target,
        )
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(400, f"Не удалось скачать Comfortaa: {exc}")
    return {"font": {"id": target.name, "name": "Comfortaa", "path": str(target)}}


@app.get("/api/montage/silence/progress/{request_id}")
async def montage_silence_progress(request_id: str):
    try:
        request_id = str(uuid.UUID(request_id))
    except (ValueError, AttributeError):
        raise HTTPException(404, "Analysis progress not found")
    progress = _SILENCE_PROGRESS.get(request_id)
    if not progress or time.time() - float(progress.get("updated_at", 0)) > SILENCE_ANALYSIS_TTL_SECONDS:
        _SILENCE_PROGRESS.pop(request_id, None)
        raise HTTPException(404, "Analysis progress not found")
    return {
        "request_id": request_id,
        "status": progress.get("status"),
        "phase": progress.get("phase"),
        "percent": progress.get("percent"),
        "detail": progress.get("detail"),
        "wait_seconds": progress.get("wait_seconds"),
    }


@app.delete("/api/montage/silence/analyze/{analysis_id}")
async def release_montage_silence_analysis(analysis_id: str, request: Request):
    async with _SILENCE_ANALYSIS_LOCK:
        session = _SILENCE_SESSIONS.get(str(analysis_id or ""))
        if not session:
            return {"released": False}
        if session.get("status") != "ready":
            return {"released": False, "status": str(session.get("status") or "unknown")}
        _SILENCE_SESSIONS.pop(str(analysis_id), None)
    shutil.rmtree(str(session.get("directory") or ""), ignore_errors=True)
    _record_silence_progress(
        str(session.get("request_id") or ""), status="released", phase="released",
        percent=None, detail="Сессия анализа освобождена",
    ) if session.get("request_id") else None
    return {"released": True}


@app.post("/api/montage/silence/analyze")
async def analyze_montage_silence(request: Request):
    """Create an immutable analysis session from a streamed upload or project id."""
    await _expire_silence_sessions()
    request_id = _silence_request_id(request)
    _record_silence_progress(
        request_id, status="running", phase="resolving", percent=None,
        detail="Проверка выбранного источника",
    )
    content_type = str(request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    project_id: Optional[str] = None
    if content_type == "application/json":
        length_header = request.headers.get("content-length")
        if length_header:
            try:
                if int(length_header) > 64 * 1024:
                    return _silence_problem(
                        request, 413, "source_too_large", "Project analysis request is too large.",
                        "Send only a project_id and retry.", operation="analysis_request",
                        context={"max_request_bytes": 64 * 1024},
                    )
            except ValueError:
                return _silence_problem(
                    request, 400, "invalid_request", "Invalid content length.",
                    "Retry with a valid project request.", operation="analysis_request",
                )
        raw_body = bytearray()
        try:
            async for chunk in request.stream():
                if len(raw_body) + len(chunk) > 64 * 1024:
                    return _silence_problem(
                        request, 413, "source_too_large", "Project analysis request is too large.",
                        "Send only a project_id and retry.", operation="analysis_request",
                        context={"max_request_bytes": 64 * 1024},
                    )
                raw_body.extend(chunk)
            body = json.loads(raw_body.decode("utf-8"))
        except Exception:
            body = None
        project_id = str(body.get("project_id") or "") if isinstance(body, dict) else ""
        if not project_id or len(project_id) > 160:
            return _silence_problem(
                request, 400, "invalid_request", "A valid project_id is required.",
                "Select a saved project and retry.", operation="analysis_request",
            )
        async with PROJECTS_LOCK:
            project = PROJECTS.get(project_id)
        if not project:
            return _silence_problem(
                request, 404, "invalid_request", "The selected project was not found.",
                "Select the project again.", operation="source_resolve", context={"project_id": project_id},
            )
        project_video = Path(str(project.get("video_path") or ""))
        if not project_video.is_file():
            return _silence_problem(
                request, 409, "source_changed", "The project's video is unavailable.",
                "Reselect the project and analyze it again.", operation="source_resolve",
                context={"project_id": project_id},
            )
        try:
            size = project_video.stat().st_size
        except OSError:
            size = 0
        if size <= 0 or size > SILENCE_SOURCE_MAX_BYTES:
            return _silence_problem(
                request, 413, "source_too_large", "The project video is empty or exceeds the 2 GiB limit.",
                "Choose a smaller source video.", operation="source_resolve",
                context={"source_bytes": size, "max_source_bytes": SILENCE_SOURCE_MAX_BYTES},
            )
        filename, extension = _silence_safe_filename(project_video.name)
    elif content_type == "application/octet-stream":
        filename, extension = _silence_safe_filename(request.headers.get("x-filename") or "video.mp4")
        project_video = None
    else:
        return _silence_problem(
            request, 415, "invalid_request", "Use application/octet-stream for a video upload or application/json for a project id.",
            "Retry with one supported source selector.", operation="analysis_request",
            context={"supported_content_types": ["application/octet-stream", "application/json"]},
        )

    session = await _reserve_silence_session(filename, extension, project_id, request_id)
    if session is None:
        wait_seconds = await _silence_capacity_wait_seconds()
        _record_silence_progress(
            request_id, status="capacity", phase="capacity", percent=None,
            detail="Ожидание свободной сессии анализа", wait_seconds=wait_seconds,
        )
        return _silence_problem(
            request, 429, "analysis_capacity", "Two silence-analysis sessions are already active.",
            "Wait for a session to expire or finish, then retry.", operation="session_reserve",
            context={"active_limit": SILENCE_ANALYSIS_MAX_SESSIONS, "wait_seconds": wait_seconds,
                     "next_expiry_ms": int((time.time() + wait_seconds) * 1000)},
            headers={"Retry-After": str(wait_seconds)},
        )
    analysis_id = str(session["analysis_id"])
    source_path = Path(str(session["source_path"]))
    try:
        try:
            if project_id:
                _record_silence_progress(
                    request_id, status="running", phase="copying", percent=0.0,
                    detail="Копирование снимка видео проекта",
                )
                source_bytes, source_hash = await asyncio.to_thread(
                    _copy_project_video_snapshot,
                    project_video,
                    source_path,
                    lambda copied, total: _record_silence_progress(
                        request_id, status="running", phase="copying",
                        percent=min(99.0, copied * 100.0 / max(1, total)),
                        detail="Копирование снимка видео проекта",
                    ),
                )
            else:
                source_bytes, source_hash = await _copy_raw_upload(request, source_path, request_id)
        except OverflowError:
            await _discard_silence_session(analysis_id)
            return _silence_problem(
                request, 413, "source_too_large", "The source exceeds the 2 GiB upload limit.",
                "Choose a smaller source video.", operation="source_copy",
                context={"max_source_bytes": SILENCE_SOURCE_MAX_BYTES}, side_effects="partial",
            )
        except FileNotFoundError:
            await _discard_silence_session(analysis_id)
            return _silence_problem(
                request, 409, "source_changed", "The project source disappeared during snapshot creation.",
                "Reselect the project and analyze it again.", operation="source_copy", side_effects="partial",
            )
        except OSError as exc:
            await _discard_silence_session(analysis_id)
            code = "workspace_unavailable" if str(exc) == "workspace capacity" else "analysis_failed"
            status = 507 if code == "workspace_unavailable" else 500
            return _silence_problem(
                request, status, code, "The source could not be copied into the analysis workspace.",
                "Free disk space or choose another source, then retry.", operation="source_copy",
                context={"cause": "workspace_capacity"}, side_effects="partial",
            )
        except ValueError as exc:
            await _discard_silence_session(analysis_id)
            detail = "The upload was empty or incomplete." if str(exc) in {"empty upload", "incomplete upload"} else "The request was invalid."
            return _silence_problem(
                request, 400, "invalid_request", detail, "Select the source again and retry.",
                operation="source_copy", side_effects="partial",
            )
        except Exception as exc:
            print(f"[silence-analysis] upload failed for request {analysis_id[:8]}: {type(exc).__name__}", file=sys.stderr)
            await _discard_silence_session(analysis_id)
            return _silence_problem(
                request, 400, "invalid_request", "The upload did not complete.",
                "Reselect the source and retry.", operation="source_copy",
                context={"stage": "upload", "cause": "media_read_failed"}, side_effects="partial",
            )
        if not _free_space_for_silence_copy(0):
            await _discard_silence_session(analysis_id)
            return _silence_problem(
                request, 507, "workspace_unavailable", "The workspace needs 128 MiB of free headroom.",
                "Free disk space and retry.", operation="source_copy", context={"required_headroom_bytes": SILENCE_WORKSPACE_HEADROOM_BYTES},
                side_effects="partial",
            )

        try:
            _record_silence_progress(
                request_id, status="running", phase="probing", percent=None,
                detail="Проверка видео и аудиодорожки",
            )
            source_info = await asyncio.to_thread(_probe_silence_source, source_path)
        except OverflowError:
            await _discard_silence_session(analysis_id)
            return _silence_problem(
                request, 413, "source_too_large", "The media duration exceeds the two-hour limit.",
                "Choose a shorter source video.", operation="source_probe",
                context={"max_duration_seconds": SILENCE_MAX_DURATION_SECONDS}, side_effects="partial",
            )
        except ValueError as exc:
            await _discard_silence_session(analysis_id)
            detail = "The source has no video stream." if str(exc) == "no video stream" else "The source duration is invalid."
            return _silence_problem(
                request, 422, "unsupported_media", detail,
                "Choose a readable video file and retry.", operation="source_probe", side_effects="partial",
            )
        except Exception as exc:
            print(f"[silence-analysis] probe failed for request {analysis_id[:8]}: {type(exc).__name__}", file=sys.stderr)
            await _discard_silence_session(analysis_id)
            return _silence_problem(
                request, 422, "unsupported_media", "The video could not be inspected.",
                "Reselect or convert the source and retry.", operation="source_probe",
                context={"stage": "probe", "cause": "media_read_failed"}, side_effects="partial",
            )

        audio = source_info.get("audio")
        if not audio:
            analysis = {
                "audio_available": False,
                "mean_rms_dbfs": None,
                "max_peak_dbfs": None,
                "envelope_dbfs_x100": [],
                "sample_rate_hz": SILENCE_SAMPLE_RATE,
                "channels": 0,
                "audio_duration_ms": 0,
            }
        else:
            try:
                channels = int(audio.get("channels") or 0)
            except (TypeError, ValueError):
                channels = 0
            if not 1 <= channels <= SILENCE_MAX_CHANNELS:
                await _discard_silence_session(analysis_id)
                return _silence_problem(
                    request, 422, "unsupported_media", "Audio channel count is unsupported.",
                    "Use a source with one to eight audio channels.", operation="audio_probe",
                    context={"channels": channels, "max_channels": SILENCE_MAX_CHANNELS}, side_effects="partial",
                )
            try:
                _record_silence_progress(
                    request_id, status="running", phase="decoding", percent=0.0,
                    detail="Декодирование аудио и расчёт уровней",
                )
                analysis = await _decode_silence_audio(
                    source_path, channels, analysis_id,
                    request_id=request_id, source_duration_ms=source_info["duration_ms"],
                )
            except asyncio.TimeoutError:
                await _discard_silence_session(analysis_id)
                return _silence_problem(
                    request, 500, "analysis_failed", "Audio analysis exceeded its time limit.",
                    "Check the source and retry once.", operation="audio_decode",
                    context={"stage": "decode", "cause": "ffmpeg_timeout"}, side_effects="partial",
                )
            except OverflowError:
                await _discard_silence_session(analysis_id)
                return _silence_problem(
                    request, 413, "source_too_large", "The source produces too many analysis bins.",
                    "Choose a shorter source video.", operation="audio_decode",
                    context={"max_bins": 720_000}, side_effects="partial",
                )
            except MemoryError:
                await _discard_silence_session(analysis_id)
                return _silence_problem(
                    request, 413, "analysis_resource_limit", "The audio decoder exceeded its memory limit.",
                    "Choose a smaller source or a source with fewer channels, then retry.", operation="audio_decode",
                    context={"stage": "decode", "cause": "workspace_capacity",
                             "max_worker_rss_bytes": SILENCE_WORKER_MEMORY_LIMIT_BYTES},
                    side_effects="partial",
                )
            except Exception as exc:
                print(f"[silence-analysis] decode failed for request {analysis_id[:8]}: {type(exc).__name__}", file=sys.stderr)
                await _discard_silence_session(analysis_id)
                return _silence_problem(
                    request, 422, "unsupported_media", "The audio stream could not be decoded.",
                    "Reselect or convert the source and retry.", operation="audio_decode",
                    context={"stage": "decode", "cause": "unsupported_codec"}, side_effects="partial",
                )
            analysis["audio_available"] = True

        if len(analysis.get("envelope_dbfs_x100") or []) > 720_000:
            await _discard_silence_session(analysis_id)
            return _silence_problem(
                request, 413, "source_too_large", "The analysis envelope exceeds the supported limit.",
                "Choose a shorter source video.", operation="response_build", context={"max_bins": 720_000},
                side_effects="partial",
            )
        session_update = {
            "status": "ready",
            "source_bytes": source_bytes,
            "source_sha256": source_hash,
            "source_duration_ms": source_info["duration_ms"],
            "fps": source_info["fps"],
            "analysis": analysis,
            "project_transcript_job_id": str((project or {}).get("transcript_job_id") or "") if project_id else "",
            "source_name": str((project or {}).get("name") or filename) if project_id else filename,
        }
        async with _SILENCE_ANALYSIS_LOCK:
            current = _SILENCE_SESSIONS.get(analysis_id)
            if not current:
                raise RuntimeError("analysis session expired")
            current.update(session_update)
        _record_silence_progress(
            request_id, status="ready", phase="complete", percent=100.0,
            detail="Анализ готов",
        )

        response_body = {
            "analysis_id": analysis_id,
            "request_id": request_id,
            "source_filename": session_update["source_name"],
            "source_duration_ms": source_info["duration_ms"],
            "fps": source_info["fps"],
            "audio_available": bool(analysis["audio_available"]),
            "sample_rate_hz": SILENCE_SAMPLE_RATE,
            "channels": analysis["channels"],
            "bin_ms": SILENCE_BIN_MS,
            "mean_rms_dbfs": analysis["mean_rms_dbfs"],
            "max_peak_dbfs": analysis["max_peak_dbfs"],
            "audio_duration_ms": analysis["audio_duration_ms"],
            "envelope_dbfs_x100": analysis["envelope_dbfs_x100"],
            "expires_at_ms": int(float(session["expires_at"]) * 1000),
        }
        response = JSONResponse(content=response_body)
        if len(response.body) > SILENCE_RESPONSE_MAX_BYTES:
            await _discard_silence_session(analysis_id)
            return _silence_problem(
                request, 413, "source_too_large", "The analysis response exceeds the 8 MiB limit.",
                "Choose a shorter source video.", operation="response_build",
                context={"max_response_bytes": SILENCE_RESPONSE_MAX_BYTES}, side_effects="partial",
            )
        return response
    except asyncio.CancelledError:
        _record_silence_progress(
            request_id, status="cancelled", phase="cancelled", percent=None,
            detail="Анализ отменён",
        )
        await _discard_silence_session(analysis_id)
        raise
    except Exception as exc:
        _record_silence_progress(
            request_id, status="error", phase="error", percent=None,
            detail="Не удалось завершить анализ",
        )
        print(f"[silence-analysis] unexpected failure for request {request_id[:8]}: {type(exc).__name__}", file=sys.stderr)
        await _discard_silence_session(analysis_id)
        raise


@app.post("/api/montage/render")
async def start_montage(
    request: Request,
    video: Optional[UploadFile] = File(None),
    srt: Optional[UploadFile] = File(None),
    overlay_files: list[UploadFile] = File(default=[]),
    transcript_job_id: Optional[str] = Form(None),
    project_id: Optional[str] = Form(None),
    subtitle_mode: str = Form("words"),
    subtitle_position: str = Form("custom"),
    subtitle_x: Optional[float] = Form(None),
    subtitle_y: Optional[float] = Form(None),
    subtitle_scale: Optional[float] = Form(None),
    subtitle_box: Optional[bool] = Form(False),
    subtitle_font: Optional[str] = Form(None),
    face_tracking: bool = Form(False),
    autozoom: bool = Form(False),
    camera_mode: str = Form(""),
    follow_strength: float = Form(30.0),
    lag_seconds: float = Form(0.4),
    min_zoom: float = Form(1.12),
    camera_wobble: bool = Form(False),
    blur_banned_words: bool = Form(False),
    subtitles: bool = Form(True),
    bigpickle: bool = Form(True),
    ai_model: str = Form(""),
    edge_mode: str = Form("scale"),
    animation_id: str = Form(""),
    animation_start: float = Form(0.0),
    animation_offset_x: int = Form(0),
    animation_item_size: int = Form(72),
    animation_bar: bool = Form(False),
    animations: str = Form("[]"),
    overlays_json: Optional[str] = Form(None),
    propose_cover: bool = Form(False),
    cover_title: str = Form(""),
    zoom_timeline_json: Optional[str] = Form(None),
    prepare_mirrored: bool = Form(False),
    silence_analysis_id: Optional[str] = Form(None),
    silence_trim_enabled: bool = Form(False),
    silence_cuts_json: str = Form("[]"),
):
    silence_request = None
    validated_silence_cuts: list[dict[str, Any]] = []
    if silence_trim_enabled:
        if not silence_analysis_id or video is not None or project_id:
            return _silence_problem(
                request, 400, "invalid_request",
                "Trimmed renders require one analysis session and cannot include another video or project source.",
                "Analyze the selected source, then submit only its analysis id with the cut list.",
                operation="render_request",
            )
        try:
            raw_cuts = json.loads(silence_cuts_json or "[]")
        except (TypeError, json.JSONDecodeError):
            raw_cuts = None
        if not isinstance(raw_cuts, list):
            return _silence_problem(
                request, 422, "invalid_cut_list", "The cut list must be a JSON array.",
                "Correct the cut list and retry.", operation="render_validation",
            )
        if len(raw_cuts) > SILENCE_MAX_CUTS:
            return _silence_problem(
                request, 422, "invalid_cut_list", "The cut list exceeds the 10,000 cut limit.",
                "Reduce the number of cut ranges and retry.", operation="render_validation",
                context={"cut_count": len(raw_cuts), "max_cuts": SILENCE_MAX_CUTS},
            )
        silence_request = await _analysis_session(silence_analysis_id)
        if not silence_request:
            return _silence_problem(
                request, 410, "analysis_expired", "The analysis session expired or is no longer available.",
                "Reanalyze the source and retry.", operation="session_lookup",
            )
        try:
            validated_silence_cuts = raw_cuts
            normalized = normalize_cut_list(raw_cuts, int(silence_request["source_duration_ms"]))
            source_fps = parse_frame_rate(silence_request.get("fps") or "30/1")
            total_frames = max(1, ceil_fraction(Fraction(
                int(silence_request["source_duration_ms"]), 1000
            ) * source_fps))
            effective = effective_frame_intervals(
                normalized, source_fps, int(silence_request["source_duration_ms"])
            )
            if effective:
                retained_frame_segments(effective, total_frames)
        except ValueError as exc:
            return _silence_problem(
                request, 422, "invalid_cut_list", "The cut list is invalid or removes the entire source.",
                "Adjust the cut boundaries and retry.", operation="render_validation",
                context={"cause": "invalid_cut_list"},
            )
    elif silence_analysis_id:
        return _silence_problem(
            request, 400, "invalid_request", "An analysis id requires trimming to be enabled.",
            "Enable silence trimming or omit the analysis id.", operation="render_request",
        )

    job_id = str(uuid.uuid4())[:8]
    ai_model = _normalize_ai_model(ai_model)
    work_dir = MONTAGE_WORK / job_id
    work_dir.mkdir(parents=True, exist_ok=True)

    video_path: Optional[Path] = None
    srt_text = ""
    src_job_id: Optional[str] = None
    existing_covers: list[dict] = []
    original_name = "video.mp4"
    analysis_handoff: Optional[dict[str, Any]] = None

    if silence_trim_enabled:
        analysis_handoff = silence_request
        video_path = Path(str(silence_request["source_path"]))
        original_name = str(silence_request.get("source_name") or silence_request.get("filename") or "video.mp4")
        project_id = silence_request.get("project_id")
        if project_id:
            async with PROJECTS_LOCK:
                project = PROJECTS.get(project_id)
            if not project:
                shutil.rmtree(work_dir, ignore_errors=True)
                return _silence_problem(
                    request, 409, "source_changed", "The analyzed project is no longer available.",
                    "Reselect the project and analyze it again.", operation="session_handoff",
                    context={"project_id": project_id}, side_effects="partial",
                )
            project_covers_dir = Path(str(project.get("covers_dir") or ""))
            if project_covers_dir.is_dir() and project.get("covers"):
                shutil.copytree(project_covers_dir, work_dir / "covers", dirs_exist_ok=True)
                existing_covers = list(project.get("covers") or [])
            src_job_id = project.get("transcript_job_id")
            if src_job_id and jobs.get(src_job_id) and jobs[src_job_id].get("status") == "done" \
                    and isinstance(jobs[src_job_id].get("result"), list):
                srt_text = montage_engine.build_words_srt(jobs[src_job_id]["result"])
    elif project_id:
        async with PROJECTS_LOCK:
            project = PROJECTS.get(project_id)
        if not project:
            raise HTTPException(404, "Проект не найден")
        if not project.get("video_path") or not Path(project["video_path"]).exists():
            raise HTTPException(400, "Видео проекта недоступно")
        video_path = work_dir / f"project_video{Path(project['video_path']).suffix or '.mp4'}"
        shutil.copy2(project["video_path"], video_path)
        project_covers_dir = Path(str(project.get("covers_dir") or ""))
        if project_covers_dir.is_dir() and project.get("covers"):
            shutil.copytree(project_covers_dir, work_dir / "covers", dirs_exist_ok=True)
            existing_covers = list(project.get("covers") or [])
        src_job_id = project.get("transcript_job_id")
        if src_job_id and jobs.get(src_job_id) and jobs[src_job_id].get("status") == "done" \
                and isinstance(jobs[src_job_id].get("result"), list):
            srt_text = montage_engine.build_words_srt(jobs[src_job_id]["result"])
    else:
        if video is None:
            raise HTTPException(400, "Загрузите видео или выберите проект")
        ext = Path(video.filename or "video.mp4").suffix or ".mp4"
        video_path = work_dir / f"source{ext}"
        content = await video.read()
        video_path.write_bytes(content)
        original_name = Path(video.filename or "video.mp4").name

    if video is not None and video.filename:
        original_name = Path(video.filename).name
    elif project_id:
        original_name = PROJECTS.get(project_id, {}).get("name", original_name or "Проект")

    if srt is not None and srt.filename:
        srt_text = (await srt.read()).decode("utf-8", errors="replace")

    if not srt_text and transcript_job_id:
        source_job = jobs.get(transcript_job_id)
        if not source_job or source_job.get("status") != "done" \
                or not isinstance(source_job.get("result"), list):
            raise HTTPException(400, "Сохранённая транскрибация недоступна")
        src_job_id = transcript_job_id
        srt_text = montage_engine.build_words_srt(source_job["result"])

    if not srt_text:
        raise HTTPException(400, "Нет текста для субтитров (загрузите SRT или выберите проект с транскрибацией)")
    cover_title = cover_title.strip()
    if len(cover_title) > 100:
        raise HTTPException(400, "Название обложки должно быть не длиннее 100 символов")

    zoom_override = None
    if zoom_timeline_json and zoom_timeline_json.strip():
        try:
            zoom_override = json.loads(zoom_timeline_json)
        except json.JSONDecodeError:
            raise HTTPException(400, "zoom_timeline_json должен быть валидным JSON")

    overlays: list[dict] = []
    if overlays_json and overlays_json.strip():
        try:
            raw_overlays = json.loads(overlays_json)
        except json.JSONDecodeError:
            raise HTTPException(400, "overlays_json должен быть валидным JSON")
        if not isinstance(raw_overlays, list):
            raise HTTPException(400, "overlays_json должен быть списком")
        if len(raw_overlays) > 20:
            raise HTTPException(400, "Можно добавить не более 20 наложений")
        saved_overlay_paths: dict[int, Path] = {}
        overlay_uploads = overlay_files or []
        total_overlay_bytes = 0
        for index, raw in enumerate(raw_overlays):
            if not isinstance(raw, dict):
                raise HTTPException(400, f"Наложение {index + 1} имеет неверный формат")
            try:
                start = float(raw.get("start") or 0.0)
                end = float(raw.get("end"))
                x = float(raw.get("x", 0.5))
                y = float(raw.get("y", 0.5))
                size = max(20, min(1200, int(raw.get("size") or 180)))
            except (TypeError, ValueError):
                raise HTTPException(400, f"Параметры наложения {index + 1} некорректны")
            if not all(math.isfinite(value) for value in (start, end, x, y)):
                raise HTTPException(400, f"Параметры наложения {index + 1} должны быть конечными числами")
            start = max(0.0, min(86399.95, start))
            end = max(0.0, min(86400.0, end))
            x = max(0.0, min(1.0, x))
            y = max(0.0, min(1.0, y))
            if end - start < 0.05:
                raise HTTPException(400, f"Длительность наложения {index + 1} должна быть не меньше 0.05 сек")
            overlay = {
                "uid": str(raw.get("uid") or f"overlay-{index + 1}")[:80],
                "start": round(start, 3),
                "end": round(end, 3),
                "x": round(x, 4),
                "y": round(y, 4),
                "size": size,
                "z_index": index,
            }
            overlay_kind = str(raw.get("kind") or "")
            if overlay_kind == "image":
                try:
                    file_index = int(raw.get("file_index"))
                    upload = overlay_uploads[file_index]
                except (TypeError, ValueError, IndexError):
                    raise HTTPException(400, f"Файл наложения {index + 1} не найден")
                if file_index not in saved_overlay_paths:
                    content = await upload.read()
                    total_overlay_bytes += len(content)
                    if not content or len(content) > 20 * 1024 * 1024 or total_overlay_bytes > 100 * 1024 * 1024:
                        raise HTTPException(400, "Изображения наложений слишком большие")
                    try:
                        from PIL import Image
                        with Image.open(io.BytesIO(content)) as image:
                            image.verify()
                        with Image.open(io.BytesIO(content)) as image:
                            width, height = image.size
                            image_format = str(image.format or "").upper()
                    except Exception:
                        raise HTTPException(400, f"Наложение {index + 1} не является корректным изображением")
                    if image_format not in {"PNG", "JPEG", "WEBP"} or width < 1 or height < 1 or width * height > 50_000_000:
                        raise HTTPException(400, f"Формат или размер наложения {index + 1} не поддерживается")
                    extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[image_format]
                    overlay_dir = work_dir / "overlays"
                    overlay_dir.mkdir(exist_ok=True)
                    overlay_path = overlay_dir / f"overlay_{file_index + 1:02d}{extension}"
                    overlay_path.write_bytes(content)
                    saved_overlay_paths[file_index] = overlay_path
                overlay.update({
                    "kind": "image",
                    "image_path": str(saved_overlay_paths[file_index].resolve()),
                    "name": Path(upload.filename or f"Наложение {index + 1}").name[:120],
                })
            else:
                overlay_animation_id = str(raw.get("animation_id") or "")
                if Path(overlay_animation_id).name != overlay_animation_id or not (
                    montage_engine.ANIMATIONS_DIR / overlay_animation_id / "manifest.json"
                ).is_file():
                    raise HTTPException(400, f"Источник наложения {index + 1} не найден")
                overlay.update({"kind": "animation", "animation_id": overlay_animation_id})
            overlays.append(overlay)
    if animation_id:
        if Path(animation_id).name != animation_id or not (
            montage_engine.ANIMATIONS_DIR / animation_id / "manifest.json"
        ).is_file():
            raise HTTPException(400, "Выбранная анимация не найдена")

    if silence_trim_enabled:
        analysis_handoff, handoff_error = await _handoff_silence_session(silence_analysis_id or "", work_dir)
        if handoff_error:
            shutil.rmtree(work_dir, ignore_errors=True)
            if handoff_error in {"snapshot_missing", "snapshot_hash_mismatch"}:
                return _silence_problem(
                    request, 409, "source_changed",
                    "The immutable analysis snapshot is missing or its contents no longer match the analysis.",
                    "Reanalyze the selected source; manual cuts are retained in the editor.",
                    operation="session_handoff", context={"cause": handoff_error}, side_effects="none",
                )
            return _silence_problem(
                request, 410, "analysis_expired", "The analysis session expired before render handoff.",
                "Reanalyze the source and retry.", operation="session_handoff",
            )
        video_path = Path(str(analysis_handoff["source_path"]))

    jobs[job_id] = {
        "id": job_id,
        "original_name": original_name,
        "video_path": str(video_path),
        "srt_text": srt_text,
        "transcript_job_id": src_job_id,
        "project_id": project_id,
        "kind": "montage",
        "status": "uploaded",
        "phase": "queued",
        "warnings": [],
        "progress": 0,
        "stage": "",
        "detail": "",
        "result": None,
        "publish": None,
        "error": None,
        "error_info": None,
        "created_at": datetime.now().isoformat(),
        "covers": existing_covers,
        "cover_status": "done" if existing_covers else "idle",
        "cover_progress": 0,
        "cover_detail": "",
        "cover_error": None,
        "cover_title": cover_title,
        "operation": None,
        "variants": {},
        "mirror_variant": None,
        "publish_prep": {},
        "prepare_operation": None,
        "prepare_status": "idle",
        "prepare_error": None,
    }
    params = {
        "subtitle_mode": subtitle_mode,
        "subtitle_position": subtitle_position,
        "subtitle_x": subtitle_x,
        "subtitle_y": subtitle_y,
        "subtitle_scale": subtitle_scale,
        "subtitle_box": subtitle_box,
        "subtitle_font": subtitle_font,
        "face_tracking": face_tracking,
        "autozoom": autozoom,
        "camera_mode": _validate_camera_mode(camera_mode, face_tracking),
        "follow_strength": _validate_float_range(
            follow_strength, 0.0, 100.0, 30.0, "follow_strength") / 100.0,
        "lag_seconds": _validate_float_range(
            lag_seconds, 0.0, 3.0, 0.4, "lag_seconds"),
        "min_zoom": _validate_float_range(
            min_zoom, 1.05, 1.25, 1.12, "min_zoom"),
        "camera_wobble": bool(camera_wobble),
        "mirror_horizontal": False,
        "blur_banned_words": blur_banned_words,
        "subtitles": subtitles,
        "bigpickle": bigpickle,
        "ai_model": ai_model,
        "edge_mode": edge_mode,
        "overlays": overlays,
        "animation": {
            "enabled": bool(animation_id),
            "id": animation_id,
            "start": animation_start,
            "offset_x": animation_offset_x,
            "item_size": max(20, animation_item_size),
            "bar": animation_bar,
        },
        "animations": _validate_timeline_animations(
            animations, animation_id, animation_start,
            animation_offset_x, animation_item_size),
        "zoom_timeline_override": zoom_override,
        "prepare_mirrored": bool(prepare_mirrored),
        "silence_trim_enabled": bool(silence_trim_enabled),
        "silence_cuts": validated_silence_cuts if silence_trim_enabled else [],
        "silence_source_duration_ms": int(analysis_handoff.get("source_duration_ms") or 0) if analysis_handoff else 0,
        "silence_source_sha256": str(analysis_handoff.get("source_sha256") or "") if analysis_handoff else "",
        "silence_target_fps": str(analysis_handoff.get("fps") or "") if analysis_handoff else "",
    }
    jobs[job_id]["params"] = params
    _save_montage_job(job_id)
    if not project_id and ((video and video.filename) or analysis_handoff):
        name = original_name
        ext = Path(name).suffix.lower()
        if ext in VIDEO_EXTS and srt_text:
            async with PROJECTS_LOCK:
                PROJECTS[f"p_{job_id}"] = {
                    "id": f"p_{job_id}",
                    "name": name,
                    "video_path": str(video_path),
                    "transcript_job_id": src_job_id,
                    "status": "ready",
                    "created_at": datetime.now().isoformat(),
                    "covers": list(jobs[job_id].get("covers") or []),
                    "covers_dir": str(MONTAGE_WORK / job_id / "covers"),
                }
                _save_projects()
    async def _run_montage_then_variant() -> None:
        await process_montage(job_id, params)
        job = jobs.get(job_id) or {}
        if job.get("status") in {"error", "cancelled"} or _cancel_requested(job_id):
            return
        # ---- Phase B: mirrored variant (optional, one toggle) ----
        if params.get("prepare_mirrored"):
            job["phase"] = "rendering_mirror"
            _save_montage_job(job_id)
            if not mirror_variant_ready(job):
                await broadcast_progress(job_id, 93, "rendering_mirror", "Отражённая версия (мастер): рендер")
                await process_mirror_variant(job_id)
        # ---- Phase C: verify and finalize (no premature done) ----
        # Platform artefacts are prepared lazily at publish time
        # (see _prepare_then_publish), never right after the render.
        job["phase"] = "verifying_outputs"
        _save_montage_job(job_id)
        await broadcast_progress(job_id, 98, "verifying_outputs", "Проверка результатов")
        await _finalize_montage(job_id)

    _track_montage_task(job_id, _run_montage_then_variant())
    if propose_cover:
        _track_montage_task(job_id, process_cover_suggestions(job_id))
    return {"job_id": job_id, "filename": original_name}


class PublicationQueueIntervalInput(BaseModel):
    interval_minutes: int = Field(
        ..., ge=publication_queue.MIN_INTERVAL_MINUTES,
        le=publication_queue.MAX_INTERVAL_MINUTES,
    )


class PublicationQueueSettingsInput(BaseModel):
    intervals: dict


class PublicationQueueNowInput(BaseModel):
    confirm_unknown: bool = False


def _publication_queue_item_view(item: dict) -> dict:
    result = dict(item)
    job = jobs.get(str(item.get("job_id") or ""))
    platform_labels = {"instagram": "Instagram", "youtube": "YouTube"}
    result["platform_label"] = platform_labels.get(item.get("platform"), item.get("platform"))
    result["target_labels"] = [
        _PUBLISH_TARGET_SPECS.get(target_id, (None, False, target_id))[2]
        for target_id in item.get("target_ids") or []
    ]
    result["thumbnail_url"] = f"/api/thumb/{item['job_id']}" if job else ""
    result["preview_url"] = f"/api/montage/preview/{item['job_id']}" if job else ""
    progress = (job.get("publish_progress") or {}) if job else {}
    saved_progress = item.get("progress") or {}
    result["target_progress"] = [
        dict(progress.get(target_id) or saved_progress[target_id], id=target_id)
        if progress.get(target_id) or saved_progress.get(target_id) else {
            "id": target_id,
            "label": _PUBLISH_TARGET_SPECS.get(target_id, (None, False, target_id))[2],
            "progress": 100 if item.get("status") == "published" else None,
            "status": "done" if item.get("status") == "published" else item.get("status"),
            "detail": item.get("error") or (
                "Публикация завершена" if item.get("status") == "published" else "Ожидание публикации"
            ),
            "steps": [],
        }
        for target_id in item.get("target_ids") or []
    ]
    result["preparation_progress"] = (job.get("prepare_operation") or {}) if job else {}
    return result


@app.get("/api/publication-queue")
async def get_publication_queue(page: int = 1, page_size: int = 10):
    try:
        result = await asyncio.to_thread(publication_queue.list_page, page, page_size)
        result["items"] = [_publication_queue_item_view(item) for item in result["items"]]
        result["settings"] = await asyncio.to_thread(publication_queue.get_settings)
        result["control"] = await asyncio.to_thread(publication_queue.get_control)
        return result
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(503, "Очередь публикаций временно недоступна") from exc


@app.put("/api/publication-queue/settings")
async def update_publication_queue_settings(payload: PublicationQueueSettingsInput):
    try:
        settings = await asyncio.to_thread(publication_queue.update_settings, payload.intervals)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(503, "Не удалось сохранить частоту публикаций") from exc
    _wake_publication_queue()
    return {"settings": settings}


@app.post("/api/publication-queue/resume")
async def resume_publication_queue():
    try:
        control = await asyncio.to_thread(publication_queue.resume_queue)
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(503, "Не удалось возобновить очередь") from exc
    _wake_publication_queue()
    return {"control": control}


@app.put("/api/publication-queue/settings/{platform}")
async def update_publication_queue_setting(
    platform: str, payload: PublicationQueueIntervalInput
):
    try:
        setting = await asyncio.to_thread(
            publication_queue.update_setting, platform, payload.interval_minutes
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(503, "Не удалось сохранить частоту публикаций") from exc
    _wake_publication_queue()
    return {"setting": setting, "settings": await asyncio.to_thread(publication_queue.get_settings)}


@app.post("/api/publication-queue/{queue_id}/publish-now")
async def publish_queue_item_now(queue_id: str, payload: PublicationQueueNowInput):
    try:
        item = await asyncio.to_thread(publication_queue.get_item, queue_id)
        if item is None:
            raise HTTPException(404, "Запись очереди не найдена")
        job = jobs.get(str(item.get("job_id") or "")) or {}
        target_status = job.get("publish_status") or {}
        target_unknown = job.get("publish_unknown") or {}
        unknown_targets = [
            target_id for target_id in item.get("target_ids") or []
            if item.get("status") == "unknown"
            or target_status.get(target_id) == "unknown"
            or target_id in target_unknown
        ]
        if unknown_targets and not payload.confirm_unknown:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "Перед повторной отправкой проверьте неизвестный результат публикации",
                    "confirm_targets": unknown_targets,
                },
            )
        updated = await asyncio.to_thread(
            publication_queue.publish_now, queue_id, payload.confirm_unknown
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(503, "Не удалось запустить публикацию сейчас") from exc
    if updated is None:
        raise HTTPException(404, "Запись очереди не найдена")
    _wake_publication_queue()
    return {"item": _publication_queue_item_view(updated)}


@app.delete("/api/publication-queue/{queue_id}")
async def cancel_publication_queue_item(queue_id: str):
    try:
        item = await asyncio.to_thread(publication_queue.cancel_queued_item, queue_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(503, "Не удалось убрать запись из очереди") from exc
    if item is None:
        raise HTTPException(404, "Запись очереди не найдена")
    _wake_publication_queue()
    return {"deleted": True}


@app.post("/api/montage/{job_id}/publish-queue", status_code=201)
async def enqueue_montage_publication(
    job_id: str,
    targets_json: str = Form(...),
    title: str = Form(""),
    text: str = Form(""),
    cover_id: str = Form(""),
    request_id: str = Form(""),
    new_publication: bool = Form(False),
    confirm_unknown: bool = Form(False),
):
    job = jobs.get(job_id)
    if job is None or job.get("kind") != "montage":
        raise HTTPException(404, "Montage job not found")
    if not job.get("result") or job.get("status") not in {"done", "completed", "processing", "publishing"}:
        raise HTTPException(400, "Сначала завершите рендер")
    try:
        requested_targets = json.loads(targets_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(400, "targets_json must be valid JSON") from exc
    if not isinstance(requested_targets, list) or not requested_targets:
        raise HTTPException(400, "Список целей пуст")
    if len(title) > 119:
        raise HTTPException(400, "Заголовок должен быть не длиннее 119 символов")
    if len(text) > 2000:
        raise HTTPException(400, "Текст должен быть не длиннее 2000 символов")

    target_ids = list(dict.fromkeys(str(item) for item in requested_targets))
    unknown = [target_id for target_id in target_ids if target_id not in _PUBLISH_TARGET_SPECS]
    if unknown:
        raise HTTPException(400, f"Неизвестные цели публикации: {', '.join(unknown)}")
    if not new_publication:
        published = set((job.get("publish") or {}).keys())
        published.update(
            target_id for target_id, status in (job.get("publish_status") or {}).items()
            if status == "published"
        )
        target_ids = [target_id for target_id in target_ids if target_id not in published]
    if not target_ids:
        raise HTTPException(400, "Все выбранные цели уже успешно опубликованы")

    unknown_needing_confirm = [
        target_id for target_id in target_ids
        if (job.get("publish_status") or {}).get(target_id) == "unknown"
        or target_id in (job.get("publish_unknown") or {})
    ]
    if unknown_needing_confirm and not confirm_unknown:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Требуется подтверждение повторной публикации: "
                          + ", ".join(unknown_needing_confirm),
                "confirm_targets": unknown_needing_confirm,
            },
        )

    if any(_PUBLISH_TARGET_SPECS[target_id][1] for target_id in target_ids) and not mirror_variant_ready(job):
        raise HTTPException(400, "Отражённая версия не готова. Включите её при монтаже и повторите.")
    selected_cover = None
    if cover_id:
        selected_cover = next(
            (item for item in job.get("covers") or [] if item.get("id") == cover_id), None
        )
        if selected_cover is None:
            raise HTTPException(400, "Выбранная обложка не найдена")
        cover_path = MONTAGE_WORK / job_id / "covers" / Path(str(selected_cover.get("file") or "")).name
        if not cover_path.is_file():
            raise HTTPException(400, "Файл выбранной обложки недоступен")

    cleaned_title = title.strip()
    cleaned_caption = text.strip()
    original_title = Path(str(job.get("original_name") or "")).stem or job_id
    fallback_title = cleaned_title or cleaned_caption or original_title
    youtube_title = (cleaned_title or cleaned_caption or original_title)[:119]
    request_key = request_id.strip() or uuid.uuid4().hex
    if len(request_key) > 120:
        raise HTTPException(400, "Идентификатор запроса слишком длинный")
    items = []
    try:
        for target_id in target_ids:
            kind = _PUBLISH_TARGET_SPECS[target_id][0]
            platform = "youtube" if kind == "youtube" else "instagram"
            item = await asyncio.to_thread(
                publication_queue.enqueue,
                request_id=f"{request_key}:target:{target_id}",
                job_id=job_id,
                platform=platform,
                target_ids=[target_id],
                display_title=fallback_title[:2000],
                youtube_title=youtube_title,
                instagram_caption=cleaned_caption,
                cover_id=cover_id,
                confirm_unknown=confirm_unknown,
            )
            items.append(item)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (sqlite3.Error, OSError) as exc:
        raise HTTPException(503, "Не удалось добавить публикацию в очередь") from exc

    _wake_publication_queue()
    return {
        "status": "queued",
        "items": [_publication_queue_item_view(item) for item in items],
        "settings": await asyncio.to_thread(publication_queue.get_settings),
        "control": await asyncio.to_thread(publication_queue.get_control),
    }


@app.post("/api/montage/{job_id}/publish")
async def publish_montage(
    job_id: str,
    targets_json: str = Form(...),
    title: str = Form(""),
    text: str = Form(""),
    cover_id: str = Form(""),
    new_publication: bool = Form(False),
    confirm_unknown: bool = Form(False),
):
    if job_id not in jobs or jobs[job_id].get("kind") != "montage":
        raise HTTPException(404, "Montage job not found")
    job = jobs[job_id]
    if job["status"] == "publishing":
        raise HTTPException(409, "Публикация уже выполняется для этого ролика")
    # The unified pipeline may still be finishing the optional mirrored
    # variant after the main render.  The main video is already
    # available, so publishing is allowed as soon as a result exists.
    if not job.get("result") or job["status"] not in {"done", "completed", "processing"}:
        raise HTTPException(400, "Сначала завершите рендер")
    try:
        requested_targets = json.loads(targets_json)
    except json.JSONDecodeError:
        raise HTTPException(400, "targets_json must be valid JSON")
    if not isinstance(requested_targets, list) or not requested_targets:
        raise HTTPException(400, "Список целей пуст")
    if len(title) > 119:
        raise HTTPException(400, "Заголовок должен быть не длиннее 119 символов")
    if len(text) > 2000:
        raise HTTPException(400, "Текст должен быть не длиннее 2000 символов")
    target_specs = _PUBLISH_TARGET_SPECS
    target_ids = list(dict.fromkeys(str(item) for item in requested_targets))
    unknown = [target_id for target_id in target_ids if target_id not in target_specs]
    if unknown:
        raise HTTPException(400, f"Неизвестные цели публикации: {', '.join(unknown)}")
    if not new_publication:
        already_published = set((job.get("publish") or {}).keys())
        already_published |= {
            target_id for target_id, state in (job.get("publish_status") or {}).items()
            if state == "published"
        }
        target_ids = [target_id for target_id in target_ids if target_id not in already_published]
        if not target_ids:
            raise HTTPException(400, "Все выбранные цели уже успешно опубликованы")
    # A platform whose outcome is unknown must not be re-published without an
    # explicit per-request confirmation.  new_publication does NOT bypass this.
    publish_status = job.get("publish_status") or {}
    publish_unknown = job.get("publish_unknown") or {}
    unknown_needing_confirm = [
        target_id for target_id in target_ids
        if publish_status.get(target_id) == "unknown" or target_id in publish_unknown
    ]
    if unknown_needing_confirm and not confirm_unknown:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Требуется подтверждение повторной публикации: "
                          + ", ".join(unknown_needing_confirm),
                "confirm_targets": unknown_needing_confirm,
            },
        )
    if any(target_id.startswith("youtube") for target_id in target_ids) and not title.strip():
        raise HTTPException(400, "Для YouTube укажите заголовок")
    targets = []
    selected_cover = None
    if cover_id:
        selected_cover = next((item for item in job.get("covers") or [] if item.get("id") == cover_id), None)
        if selected_cover is None:
            raise HTTPException(400, "Выбранная обложка не найдена")
        cover_path = MONTAGE_WORK / job_id / "covers" / Path(str(selected_cover.get("file") or "")).name
        if not cover_path.exists():
            raise HTTPException(400, "Файл выбранной обложки недоступен")
    for target_id in target_ids:
        kind, mirrored, label = target_specs[target_id]
        target = {"id": target_id, "kind": kind, "mirrored": mirrored, "label": label}
        if selected_cover:
            target["cover_path"] = str(cover_path)
        if kind == "youtube":
            target["title"] = title.strip()
        else:
            target["text"] = text.strip()
            target["caption"] = text.strip()
        targets.append(target)
    # Publishing automatically prepares every missing local artefact first (spec
    # 6) — the user never presses a separate "prepare" button.  Mirrored targets
    # whose mirror variant was never rendered cannot be produced here (publish
    # and preparation never mirror); they fail per target with a clear reason.
    _, missing_targets = await asyncio.to_thread(_publish_readiness, job, targets)
    preparable_ids = {
        item.get("target") for item in missing_targets
        if item.get("reason") in {"video_not_ready", "cover_not_ready"}
    }
    auto_prepare = [t.get("id") for t in targets if t.get("id") in preparable_ids]
    mirror_blocked = [item for item in missing_targets if item.get("reason") == "mirror_not_ready"]
    if mirror_blocked:
        blocked_labels = ", ".join(
            str(item.get("label") or item.get("target")) for item in mirror_blocked
        )
        await _montage_announce(
            job_id,
            f"Отражённая версия не готова — цели ({blocked_labels}) будут пропущены. "
            "Включите «Создать отражённую версию» и повторите монтаж.",
        )
    # Atomic per-job reservation: acquire the lock before creating the
    # background task so two near-simultaneous requests can never start two
    # publications for the same job_id.
    lock = _publish_lock(job_id)
    if lock.locked():
        raise HTTPException(409, "Публикация уже выполняется для этого ролика")
    await lock.acquire()
    try:
        job["selected_cover_id"] = cover_id
        _initialize_publish_attempt(job, targets, preparing_ids=set(preparable_ids))
        _save_montage_job(job_id)
        _track_montage_task(
            job_id,
            _prepare_then_publish(
                job_id, targets, lock,
                cover_path=str(cover_path) if cover_id else None,
                cover_id=cover_id,
                prepare_ids=set(preparable_ids),
            ),
        )
    except BaseException:
        if lock.locked():
            lock.release()
        raise
    return {
        "job_id": job_id,
        "status": "publishing",
        "targets": [t.get("kind") for t in targets],
        "auto_prepare": auto_prepare,
    }


@app.post("/api/montage/{job_id}/cancel")
async def cancel_montage(job_id: str):
    """Stop every running process of a montage job ("передумал").

    Stops the render / mirrored variant / covers / preparation / publication,
    kills their FFmpeg subprocesses and then:

    * if the main video was never produced — deletes the whole workdir and
      the job itself (plus the auto-created ``p_<job_id>`` project), so the
      user can start from scratch;
    * otherwise — keeps the finished files and only marks the interrupted
      processes as cancelled, so completed work is never lost.
    """
    job = jobs.get(job_id)
    if job is None or job.get("kind") != "montage":
        raise HTTPException(404, "Montage job not found")
    tasks = [t for t in MONTAGE_TASKS.get(job_id, set()) if not t.done()]
    busy = bool(tasks) or job.get("status") in {"uploaded", "processing", "publishing"} \
        or job.get("cover_status") == "generating" or job.get("prepare_status") == "running"
    if not busy:
        raise HTTPException(409, "Нет активных процессов для отмены")
    job["cancel_requested"] = True
    _save_montage_job(job_id)
    await _montage_announce(job_id, "Отмена по запросу пользователя...")
    montage_engine.cancel_job_procs(job_id)
    if tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=30)
        except asyncio.TimeoutError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    job = jobs.get(job_id)
    if job is None:
        return {"job_id": job_id, "cancelled": True, "wiped": True}
    result_video = str((job.get("result") or {}).get("result_video") or "")
    has_result = bool(result_video) and Path(result_video).is_file() \
        and Path(result_video).stat().st_size > 0
    if has_result or job.get("status") in {"done", "completed", "published"}:
        # Finished work survives: only mark what was interrupted.
        if job.get("status") in {"uploaded", "processing", "publishing"}:
            job["status"] = "done"
            job["stage"] = "cancelled"
            job["detail"] = "Отменено пользователем. Готовые файлы сохранены."
            job["error"] = None
            mark_operation_failed(job_id, "Отменено пользователем")
        if isinstance(job.get("mirror_variant"), dict) \
                and job["mirror_variant"].get("status") == "generating":
            job["mirror_variant"].update(status="cancelled", error="Отменено пользователем",
                                         detail="Отменено пользователем")
        if job.get("cover_status") == "generating":
            job.update(cover_status="cancelled", cover_error="Отменено пользователем",
                       cover_detail="Отменено пользователем")
        if job.get("prepare_status") == "running":
            job.update(prepare_status="cancelled", prepare_error="Отменено пользователем")
        shutil.rmtree(MONTAGE_WORK / job_id / _MIRROR_VARIANT_DIR, ignore_errors=True)
        _save_montage_job(job_id)
        await broadcast_progress(job_id, job.get("progress", 0), "cancelled", "Отменено пользователем")
        return {"job_id": job_id, "cancelled": True, "wiped": False}
    # Nothing usable was produced: remove everything to start from scratch.
    shutil.rmtree(MONTAGE_WORK / job_id, ignore_errors=True)
    jobs.pop(job_id, None)
    MONTAGE_TASKS.pop(job_id, None)
    auto_project_id = f"p_{job_id}"
    workdir_resolved = str((MONTAGE_WORK / job_id).resolve())
    async with PROJECTS_LOCK:
        project = PROJECTS.get(auto_project_id)
        if project is not None:
            try:
                video_resolved = str(Path(str(project.get("video_path") or "")).resolve())
            except Exception:
                video_resolved = ""
            if video_resolved.startswith(workdir_resolved):
                PROJECTS.pop(auto_project_id, None)
                _save_projects()
    return {"job_id": job_id, "cancelled": True, "wiped": True}


_youtube_reauth_state: dict[str, str] = {"status": "idle", "detail": ""}


@app.post("/api/montage/youtube-reauth")
async def youtube_reauth():
    if _youtube_reauth_state["status"] in {"running"}:
        raise HTTPException(409, "Повторная авторизация уже выполняется")
    _youtube_reauth_state.update(status="running", detail="Открытие браузера...")

    def run() -> None:
        try:
            from montage.youtube_publisher import YouTubePublisher
            from montage.secrets_env import load_secrets_env
            load_secrets_env()
            workdir = MONTAGE_WORK / "youtube_reauth"
            workdir.mkdir(parents=True, exist_ok=True)
            youtube = YouTubePublisher(
                montage_engine.INSTAPOSTER_DIR,
                Path(sys.executable),
                workdir / "youtube.log",
            )
            youtube.reauth()
            _youtube_reauth_state.update(status="ok", detail="Авторизация YouTube обновлена")
        except Exception as exc:
            _youtube_reauth_state.update(status="error", detail=str(exc))

    threading.Thread(target=run, daemon=True).start()
    return {"status": "running"}


@app.get("/api/montage/youtube-reauth")
async def youtube_reauth_status():
    return dict(_youtube_reauth_state)


@app.get("/api/montage/result/{job_id}")
async def montage_result(job_id: str):
    if job_id not in jobs or jobs[job_id].get("kind") != "montage":
        raise HTTPException(404, "Montage job not found")
    job = jobs[job_id]
    # ffprobe inside the readiness probes must not stall the event loop.
    publish_ready = await asyncio.to_thread(_public_publish_ready, job)
    mirrored_ready = await asyncio.to_thread(mirror_variant_ready, job)
    return {
        "id": job_id,
        "status": job["status"],
        "phase": job.get("phase"),
        "warnings": list(job.get("warnings") or []),
        "progress": job["progress"],
        "stage": job["stage"],
        "detail": job["detail"],
        "error": job.get("error"),
        "error_info": job.get("error_info"),
        "result": job.get("result"),
        "publish": job.get("publish"),
        "publish_errors": job.get("publish_errors"),
        "publish_unknown": job.get("publish_unknown"),
        "publish_status": job.get("publish_status"),
        "publish_progress": job.get("publish_progress"),
        "covers": job.get("covers") or [],
        "cover_status": job.get("cover_status") or "idle",
        "cover_progress": job.get("cover_progress") or 0,
        "cover_detail": job.get("cover_detail") or "",
        "cover_error": job.get("cover_error"),
        "selected_cover_id": job.get("selected_cover_id") or "",
        "last_publish_targets": job.get("last_publish_targets") or [],
        "operation": job.get("operation"),
        "prepare_operation": job.get("prepare_operation"),
        "prepare_status": job.get("prepare_status") or "idle",
        "prepare_error": job.get("prepare_error"),
        "publish_ready": publish_ready,
        "mirror_variant": _public_mirror_variant(job),
        "variants": {"mirrored_ready": mirrored_ready},
    }


@app.get("/api/montage/{job_id}/covers/{filename}")
async def montage_cover_asset(job_id: str, filename: str, thumbnail: bool = False):
    if job_id not in jobs or jobs[job_id].get("kind") != "montage":
        raise HTTPException(404, "Montage job not found")
    safe_name = Path(filename).name
    allowed = {Path(str(item.get("file") or "")).name for item in jobs[job_id].get("covers") or []}
    if safe_name not in allowed:
        raise HTTPException(404, "Обложка не найдена")
    path = MONTAGE_WORK / job_id / "covers" / safe_name
    media_type = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    }.get(path.suffix.lower(), "image/png")
    if thumbnail:
        from PIL import Image
        thumbnail_dir = path.parent / ".thumbnails"
        thumbnail_dir.mkdir(exist_ok=True)
        thumbnail_path = thumbnail_dir / f"{path.stem}.jpg"
        if not thumbnail_path.is_file() or thumbnail_path.stat().st_mtime < path.stat().st_mtime:
            temporary = thumbnail_dir / f".{path.stem}.{uuid.uuid4().hex[:8]}.tmp.jpg"
            with Image.open(path) as image:
                image.convert("RGB").resize((360, 640), Image.Resampling.LANCZOS).save(
                    temporary, "JPEG", quality=82, optimize=True
                )
            os.replace(temporary, thumbnail_path)
        path = thumbnail_path
        media_type = "image/jpeg"
    return FileResponse(
        str(path), media_type=media_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.post("/api/montage/{job_id}/covers/upload")
async def upload_montage_cover(job_id: str, cover: UploadFile = File(...)):
    if job_id not in jobs or jobs[job_id].get("kind") != "montage":
        raise HTTPException(404, "Montage job not found")
    suffix = Path(cover.filename or "cover.png").suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise HTTPException(400, "Поддерживаются PNG, JPG и WebP")
    covers_dir = MONTAGE_WORK / job_id / "covers"
    covers_dir.mkdir(parents=True, exist_ok=True)
    temporary = covers_dir / f"upload-source-{uuid.uuid4().hex[:8]}{suffix}"
    temporary.write_bytes(await cover.read())
    try:
        items = await asyncio.to_thread(cover_generator.save_uploaded_cover, temporary, covers_dir)
    except Exception as exc:
        raise HTTPException(400, f"Не удалось обработать обложку: {exc}")
    finally:
        temporary.unlink(missing_ok=True)
    jobs[job_id]["covers"] = items
    jobs[job_id]["cover_status"] = "done"
    await _store_project_covers(job_id)
    _save_montage_job(job_id)
    return {"covers": items}


@app.post("/api/montage/{job_id}/covers/generate")
async def generate_montage_cover(job_id: str, timestamp: float = Form(...), title: str = Form(...)):
    if job_id not in jobs or jobs[job_id].get("kind") != "montage":
        raise HTTPException(404, "Montage job not found")
    title = title.strip()
    if not title or len(title) > 100:
        raise HTTPException(400, "Название обложки должно содержать 1–100 символов")
    if jobs[job_id].get("cover_status") == "generating":
        raise HTTPException(409, "Генерация обложки уже выполняется")
    jobs[job_id].update(
        cover_status="generating",
        cover_progress=0,
        cover_detail="Создание обложки из стоп-кадра",
        cover_error=None,
    )
    _save_montage_job(job_id)
    _track_montage_task(job_id, process_custom_cover(job_id, max(0, timestamp), title))
    return {"status": "generating"}


@app.get("/api/montage/download/{job_id}")
async def montage_download(job_id: str):
    if job_id not in jobs or jobs[job_id].get("kind") != "montage":
        raise HTTPException(404, "Montage job not found")
    result = jobs[job_id].get("result")
    if not result:
        raise HTTPException(400, "Рендер не завершён")
    video = Path(result["result_video"])
    if not video.exists():
        raise HTTPException(404, "Файл не найден")
    return FileResponse(path=str(video), media_type="video/mp4", filename=video.name, content_disposition_type="inline")


@app.get("/api/montage/preview/{job_id}")
async def montage_preview(job_id: str, request: Request):
    if job_id not in jobs or jobs[job_id].get("kind") != "montage":
        raise HTTPException(404, "Montage job not found")
    result = jobs[job_id].get("result")
    if not result:
        raise HTTPException(400, "Рендер не завершён")
    video = Path(result.get("preview_video") or result["result_video"])
    if not video.exists():
        raise HTTPException(404, "Файл не найден")
    return _video_response(video, request, "video/mp4")


@app.get("/api/montage/animations")
async def montage_animations():
    return {"animations": montage_engine.animation_ids()}


def _animation_dir_or_404(animation_id: str) -> Path:
    clean = Path(animation_id).name
    if not clean or clean != animation_id:
        raise HTTPException(400, "Некорректный id анимации")
    target = montage_engine.ANIMATIONS_DIR / clean
    manifest = target / "manifest.json"
    if not target.is_dir() or not manifest.is_file():
        raise HTTPException(404, "Анимация не найдена")
    return target


@app.get("/api/montage/animations/{animation_id}")
async def montage_animation_detail(animation_id: str):
    target = _animation_dir_or_404(animation_id)
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    frames = manifest.get("frames") or []
    urls = []
    for entry in frames:
        name = entry.get("file") if isinstance(entry, dict) else entry
        if name and (target / Path(name).name).is_file():
            urls.append(f"/animation-assets/{animation_id}/{Path(name).name}")
    return {"id": animation_id, "name": manifest.get("name", animation_id),
            "fps": manifest.get("fps", 10), "frames": urls,
            "names": [url.rsplit("/", 1)[1] for url in urls]}


@app.post("/api/montage/animations")
async def upload_animation(zipfile: UploadFile = File(...)):
    import zipfile as _zipfile
    if not zipfile.filename or not zipfile.filename.lower().endswith(".zip"):
        raise HTTPException(400, "Ожидается ZIP-архив")
    tmp = MONTAGE_WORK / f"anim_{uuid.uuid4().hex[:8]}.zip"
    tmp.write_bytes(await zipfile.read())
    target = montage_engine.ANIMATIONS_DIR / (zipfile.filename.rsplit(".", 1)[0])
    target.mkdir(parents=True, exist_ok=True)
    with _zipfile.ZipFile(tmp) as z:
        for name in z.namelist():
            safe = Path(name).name
            if safe:
                z.extract(name, str(target))
    tmp.unlink(missing_ok=True)
    return {"animations": montage_engine.animation_ids()}


@app.post("/api/montage/animations/create")
async def create_animation(
    name: str = Form(...),
    fps: float = Form(10.0),
    frames: list[UploadFile] = File(...),
):
    from PIL import Image

    clean_name = re.sub(r"[^\w\- ]+", "", name, flags=re.UNICODE).strip() or "Animation"
    slug = re.sub(r"\s+", "_", clean_name)
    animation_id = f"{slug}_{uuid.uuid4().hex[:8]}"
    target = montage_engine.ANIMATIONS_DIR / animation_id
    target.mkdir(parents=True, exist_ok=False)
    manifest_frames = []
    try:
        for index, upload in enumerate(frames):
            raw = await upload.read()
            if not raw:
                continue
            source = target / f"upload_{index:04d}"
            source.write_bytes(raw)
            output = target / f"frame_{len(manifest_frames):04d}.png"
            try:
                with Image.open(source) as image:
                    image.convert("RGBA").save(output, "PNG")
            finally:
                source.unlink(missing_ok=True)
            manifest_frames.append({"file": output.name})
        if not manifest_frames:
            raise HTTPException(400, "Добавьте хотя бы один корректный кадр")
        manifest = {
            "id": animation_id,
            "name": clean_name,
            "fps": max(1.0, min(60.0, float(fps))),
            "frames": manifest_frames,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return {"animation": {"id": animation_id, "name": clean_name}}


@app.post("/api/montage/animations/{animation_id}/update")
async def update_animation(
    animation_id: str,
    name: str = Form(""),
    fps: float = Form(10.0),
    keep: str = Form("[]"),
    frames: list[UploadFile] = File([]),
):
    """Update name/fps/order/frames. ``keep`` = JSON list of existing
    filenames in the desired order (deleted files are dropped)."""
    from PIL import Image

    target = _animation_dir_or_404(animation_id)
    manifest_path = target / "manifest.json"
    try:
        keep_names = json.loads(keep or "[]")
    except (TypeError, ValueError):
        raise HTTPException(400, "keep должен быть JSON-списком")
    if not isinstance(keep_names, list):
        raise HTTPException(400, "keep должен быть JSON-списком")
    existing = {}
    try:
        old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in old_manifest.get("frames") or []:
            filename = entry.get("file") if isinstance(entry, dict) else entry
            if filename and (target / Path(filename).name).is_file():
                existing[Path(filename).name] = True
    except (OSError, ValueError):
        pass
    ordered_kept = []
    for filename in keep_names:
        safe = Path(str(filename)).name
        if safe in existing and safe not in ordered_kept:
            ordered_kept.append(safe)
    manifest_frames = []
    try:
        # Two passes: kept files first move to temp names, otherwise a
        # reorder like [b, a] would overwrite a before it is moved.
        staged = []
        for filename in ordered_kept:
            current = target / filename
            temp = target / f"kept_{uuid.uuid4().hex[:8]}.png"
            current.rename(temp)
            staged.append(temp)
        for temp in staged:
            output = target / f"frame_{len(manifest_frames):04d}.png"
            if output.is_file():
                output.unlink()
            temp.rename(output)
            manifest_frames.append({"file": output.name})
        for upload in frames or []:
            raw = await upload.read()
            if not raw:
                continue
            source = target / f"upload_{uuid.uuid4().hex[:8]}"
            source.write_bytes(raw)
            output = target / f"frame_{len(manifest_frames):04d}.png"
            try:
                with Image.open(source) as image:
                    image.convert("RGBA").save(output, "PNG")
            finally:
                source.unlink(missing_ok=True)
            manifest_frames.append({"file": output.name})
        if not manifest_frames:
            raise HTTPException(400, "Анимация не может остаться без кадров")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["name"] = (name or "").strip() or manifest.get("name", animation_id)
        manifest["fps"] = max(1.0, min(60.0, float(fps)))
        manifest["frames"] = manifest_frames
        manifest["updated_at"] = time.time()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        for leftover in target.iterdir():
            if leftover.is_file() and leftover.name.startswith(("frame_", "upload_")) \
                    and leftover.name not in {entry["file"] for entry in manifest_frames}:
                leftover.unlink(missing_ok=True)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Не удалось обновить анимацию: {exc}")
    return {"animation": {"id": animation_id, "name": manifest["name"]}}


@app.delete("/api/montage/animations/{animation_id}")
async def delete_animation(animation_id: str):
    target = _animation_dir_or_404(animation_id)
    shutil.rmtree(target, ignore_errors=True)
    return {"deleted": animation_id}


@app.get("/api/jobs")
async def list_jobs():
    return [{"id": j["id"], "name": j["original_name"], "status": j["status"]} for j in jobs.values()]


@app.websocket("/ws/{job_id}")
async def websocket_progress(websocket: WebSocket, job_id: str):
    await websocket.accept()
    q = asyncio.Queue()
    job_queues[job_id] = q
    try:
        job = jobs.get(job_id)
        if job:
            await websocket.send_json({
                "type": "progress",
                "progress": job["progress"],
                "stage": job["stage"],
                "detail": job["detail"],
            })
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=10)
                await websocket.send_json(msg)
            except asyncio.TimeoutError:
                continue
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        job_queues.pop(job_id, None)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
