"""Immutable source snapshot and ffprobe metadata for Motion projects."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, Tuple

from montage.color_pipeline import probe_color_profile


MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
COPY_HEADROOM_BYTES = 256 * 1024 * 1024


class SnapshotError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_video(path: Path) -> Dict[str, Any]:
    ffprobe = shutil.which("ffprobe") or "ffprobe"
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    if result.returncode != 0:
        raise SnapshotError("ffprobe не смог прочитать готовый монтаж")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise SnapshotError("ffprobe вернул некорректные метаданные") from exc
    streams = payload.get("streams") or []
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video:
        raise SnapshotError("В готовом монтаже не найдена видеодорожка")
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    from fractions import Fraction
    rates = {}
    for key in ("r_frame_rate", "avg_frame_rate"):
        try:
            candidate = Fraction(str(video.get(key) or "0/1"))
            if candidate > 0:
                rates[key] = candidate
        except (ValueError, ZeroDivisionError):
            continue
    # r_frame_rate is the encoder's nominal cadence. avg_frame_rate is often
    # computed from the last packet timestamp and can become a huge, slightly
    # inaccurate rational (e.g. 338250000/11267329 for a 30 fps CFR master).
    fps = rates.get("r_frame_rate") or rates.get("avg_frame_rate", Fraction(0, 1))
    if fps.numerator > 240_000 or fps.denominator > 1001 or fps.numerator > 240 * fps.denominator:
        average = rates.get("avg_frame_rate")
        if average:
            fps = average.limit_denominator(1001)
        if fps.numerator > 240_000 or fps.denominator > 1001 or fps.numerator > 240 * fps.denominator:
            raise SnapshotError("FPS исходника вне поддерживаемого рационального диапазона")
    try:
        duration = float((payload.get("format") or {}).get("duration") or video.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0 or fps <= 0:
        raise SnapshotError("Не удалось определить длительность или FPS готового монтажа")
    color = probe_color_profile(path)
    return {
        "duration_ms": int(round(duration * 1000)),
        "duration_seconds": duration,
        "fps_num": int(fps.numerator),
        "fps_den": int(fps.denominator),
        "fps": "%d/%d" % (fps.numerator, fps.denominator),
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "coded_width": int(video.get("coded_width") or video.get("width") or 0),
        "coded_height": int(video.get("coded_height") or video.get("height") or 0),
        "rotation": _rotation(video),
        "sample_aspect_ratio": str(video.get("sample_aspect_ratio") or "1:1"),
        "display_aspect_ratio": str(video.get("display_aspect_ratio") or ""),
        "video_codec": str(video.get("codec_name") or ""),
        "audio_codec": str(audio.get("codec_name") or "") if audio else None,
        "audio_present": bool(audio),
        "color": color,
    }


def _rotation(video_stream: dict) -> int:
    for data in video_stream.get("side_data_list") or []:
        if data.get("side_data_type") in {"Display Matrix", "displaymatrix"}:
            try:
                return int(round(float(data.get("rotation") or 0))) % 360
            except (TypeError, ValueError):
                pass
    try:
        return int(float((video_stream.get("tags") or {}).get("rotate") or 0)) % 360
    except (TypeError, ValueError):
        return 0


def create_source_snapshot(source_path: Path, project_dir: Path, source_job_id: str,
                           source_kind: str) -> Tuple[Path, Dict[str, Any]]:
    """Pin an immutable master/delivery source without changing its inode or mode."""
    source = Path(source_path)
    if source.is_symlink() or not source.is_file():
        raise SnapshotError("Выбранный готовый монтаж недоступен как обычный файл")
    source = source.resolve()
    before = source.stat()
    if not stat.S_ISREG(before.st_mode):
        raise SnapshotError("Источник Motion должен быть обычным файлом")
    if before.st_size <= 0 or before.st_size > MAX_SOURCE_BYTES:
        raise SnapshotError("Размер готового монтажа вне поддерживаемого диапазона")

    directory = Path(project_dir) / "source"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "input.mp4"
    staging = directory / (".snapshot-%s.part" % uuid.uuid4().hex)
    if target.exists() or target.is_symlink():
        raise SnapshotError("Motion-проект уже содержит исходный snapshot")

    mode = "hardlink"
    linked_stat = None
    try:
        if source.stat().st_dev != directory.stat().st_dev:
            raise OSError("snapshot requires copy across filesystems")
        os.link(str(source), str(staging), follow_symlinks=False)
        linked_stat = staging.stat()
        if (linked_stat.st_dev, linked_stat.st_ino, linked_stat.st_size, linked_stat.st_mtime_ns) != (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ):
            raise SnapshotError("Hardlink snapshot не совпадает с выбранным мастером")
    except SnapshotError:
        staging.unlink(missing_ok=True)
        raise
    except OSError:
        staging.unlink(missing_ok=True)
        mode = "copy"
        usage = shutil.disk_usage(directory)
        if usage.free < before.st_size + COPY_HEADROOM_BYTES:
            raise SnapshotError("Недостаточно места для независимой копии Motion-источника")
        try:
            with source.open("rb") as input_stream, staging.open("xb") as output_stream:
                opened_stat = os.fstat(input_stream.fileno())
                if (opened_stat.st_dev, opened_stat.st_ino, opened_stat.st_size, opened_stat.st_mtime_ns) != (
                    before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
                ):
                    raise SnapshotError("Готовый монтаж изменился до начала копирования")
                shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
                output_stream.flush()
                os.fsync(output_stream.fileno())
                copied_stat = os.fstat(input_stream.fileno())
                if (copied_stat.st_dev, copied_stat.st_ino, copied_stat.st_size, copied_stat.st_mtime_ns) != (
                    before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
                ):
                    raise SnapshotError("Готовый монтаж изменился во время копирования")
            if staging.stat().st_size != before.st_size:
                raise SnapshotError("Размер source snapshot изменился во время копирования")
            os.chmod(staging, 0o444)
        except Exception:
            staging.unlink(missing_ok=True)
            raise

    try:
        if mode == "hardlink" and linked_stat is None:
            raise SnapshotError("Hardlink snapshot не был создан")
        checksum = _sha256(staging)
        os.replace(staging, target)
        metadata = probe_video(target)
        provenance = {
            "source_job_id": str(source_job_id),
            "source_kind": str(source_kind),
            "source_name": source.name,
            "snapshot_file": "source/input.mp4",
            "snapshot_mode": mode,
            "size_bytes": int(before.st_size),
            "source_mtime_ns": int(before.st_mtime_ns),
            "source_device": int(before.st_dev),
            "source_inode": int(before.st_ino),
            "sha256": checksum,
            "fingerprint": "sha256:%s" % checksum,
            "metadata": metadata,
        }
        return target, provenance
    except Exception:
        staging.unlink(missing_ok=True)
        if target.exists() and not target.is_symlink():
            target.unlink(missing_ok=True)
        raise


def verify_snapshot(path: Path, expected_sha256: str, expected_size: int) -> Dict[str, Any]:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise SnapshotError("Закреплённый Motion-источник отсутствует")
    info = source.stat()
    if info.st_size != int(expected_size):
        raise SnapshotError("Размер закреплённого Motion-источника изменился")
    digest = _sha256(source)
    if digest != str(expected_sha256):
        raise SnapshotError("Checksum закреплённого Motion-источника не совпадает")
    return probe_video(source)
