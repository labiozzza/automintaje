"""Silence-analysis math and deterministic source-time EDL helpers."""

from __future__ import annotations

import math
import re
from fractions import Fraction
from typing import Any, Iterable

import numpy as np

SAMPLE_RATE = 48_000
BIN_MS = 10
SAMPLES_PER_BIN = SAMPLE_RATE * BIN_MS // 1000
DB_FLOOR = -120.0
MAX_CHANNELS = 8
MAX_DURATION_SECONDS = 2 * 60 * 60
MAX_CUTS = 10_000


def dbfs(value: float) -> float:
    """Return a finite dBFS value with the UI's explicit -120 dB floor."""
    if not math.isfinite(value) or value <= 0.0:
        return DB_FLOOR
    return max(DB_FLOOR, 20.0 * math.log10(value))


def _dbfs_x100(rms: float) -> int:
    return int(round(dbfs(rms) * 100.0))


class PcmAccumulator:
    """Incremental f32le analyzer; memory use is bounded to a partial 10 ms bin."""

    def __init__(self, channels: int) -> None:
        if isinstance(channels, bool) or not isinstance(channels, int) or not 1 <= channels <= MAX_CHANNELS:
            raise ValueError("unsupported channel count")
        self.channels = channels
        self.samples_per_window = SAMPLES_PER_BIN * channels
        self.window_bytes = self.samples_per_window * 4
        self.pending = bytearray()
        self.envelope: list[int] = []
        self.total_samples = 0
        self.total_square = 0.0
        self.peak = 0.0
        self.window_square = 0.0

    def _consume(self, raw: bytes, *, final: bool = False) -> None:
        values = np.frombuffer(raw, dtype="<f4")
        if not np.isfinite(values).all():
            values = np.nan_to_num(values, copy=True, nan=0.0, posinf=0.0, neginf=0.0)
        if values.size:
            self.peak = max(self.peak, float(np.max(np.abs(values))))
            values64 = values.astype(np.float64, copy=False)
            square_sum = float(np.dot(values64, values64))
        else:
            square_sum = 0.0
        self.total_samples += int(values.size)
        self.total_square += square_sum
        self.window_square += square_sum
        if values.size == self.samples_per_window or final:
            divisor = int(values.size) if final else self.samples_per_window
            if divisor:
                self.envelope.append(_dbfs_x100(math.sqrt(self.window_square / divisor)))
            self.window_square = 0.0

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.pending.extend(chunk)
        full_bytes = (len(self.pending) // self.window_bytes) * self.window_bytes
        for offset in range(0, full_bytes, self.window_bytes):
            self._consume(bytes(self.pending[offset:offset + self.window_bytes]))
        if full_bytes:
            del self.pending[:full_bytes]

    def finish(self) -> dict[str, Any]:
        if len(self.pending) % 4:
            raise ValueError("truncated PCM sample")
        if self.pending:
            if len(self.pending) // 4 % self.channels:
                raise ValueError("truncated PCM channel frame")
            self._consume(bytes(self.pending), final=True)
            self.pending.clear()
        mean_rms = math.sqrt(self.total_square / self.total_samples) if self.total_samples else 0.0
        duration = self.total_samples / (SAMPLE_RATE * self.channels)
        return {
            "sample_rate_hz": SAMPLE_RATE,
            "channels": self.channels,
            "audio_duration_ms": int(round(duration * 1000.0)),
            "mean_rms_dbfs": round(dbfs(mean_rms), 2),
            "max_peak_dbfs": round(dbfs(self.peak), 2),
            "envelope_dbfs_x100": self.envelope,
        }


def analyze_pcm_chunks(chunks: Iterable[bytes], channels: int) -> dict[str, Any]:
    """Analyze little-endian f32le PCM, retaining only one 10 ms bin at a time."""
    accumulator = PcmAccumulator(channels)
    for chunk in chunks:
        accumulator.feed(chunk)
    return accumulator.finish()


def parse_frame_rate(value: Any) -> Fraction:
    """Parse ffprobe's rational frame rate without floating-point drift."""
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d+)\s*/\s*(\d+)", text)
    if match:
        numerator, denominator = int(match.group(1)), int(match.group(2))
        if numerator > 0 and denominator > 0:
            return Fraction(numerator, denominator)
    try:
        rate = Fraction(text)
    except (ValueError, ZeroDivisionError):
        rate = Fraction(0, 1)
    if rate <= 0:
        raise ValueError("invalid frame rate")
    return rate


def ceil_fraction(value: Fraction) -> int:
    return -((-value.numerator) // value.denominator)


def round_fraction(value: Fraction) -> int:
    """Round nonnegative rational values half-up (never bankers' rounding)."""
    if value < 0:
        raise ValueError("value must be nonnegative")
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def normalize_cut_list(cuts: Any, duration_ms: int) -> list[dict[str, Any]]:
    """Validate source-time cuts and union overlaps/adjacency with provenance."""
    if not isinstance(duration_ms, int) or isinstance(duration_ms, bool) or duration_ms <= 0:
        raise ValueError("invalid source duration")
    if not isinstance(cuts, list) or len(cuts) > MAX_CUTS:
        raise ValueError("invalid cut list")

    validated: list[dict[str, Any]] = []
    ids: set[str] = set()
    for item in cuts:
        if not isinstance(item, dict):
            raise ValueError("cut must be an object")
        cut_id = item.get("cut_id")
        start = item.get("start_ms")
        end = item.get("end_ms")
        origin = item.get("origin")
        if not isinstance(cut_id, str) or not cut_id or len(cut_id) > 128 or cut_id in ids:
            raise ValueError("invalid or duplicate cut id")
        if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
            raise ValueError("cut bounds must be integer milliseconds")
        if start < 0 or end <= start or end > duration_ms:
            raise ValueError("cut bounds are outside the source")
        if origin not in ("auto", "edited", "manual"):
            raise ValueError("invalid cut origin")
        ids.add(cut_id)
        validated.append({"cut_id": cut_id, "start_ms": start, "end_ms": end, "origin": origin})

    validated.sort(key=lambda cut: (cut["start_ms"], cut["end_ms"], cut["cut_id"]))
    merged: list[dict[str, Any]] = []
    for cut in validated:
        if not merged or cut["start_ms"] > merged[-1]["end_ms"]:
            merged.append({
                "start_ms": cut["start_ms"],
                "end_ms": cut["end_ms"],
                "cut_ids": [cut["cut_id"]],
                "origins": [cut["origin"]],
            })
            continue
        current = merged[-1]
        current["end_ms"] = max(current["end_ms"], cut["end_ms"])
        current["cut_ids"].append(cut["cut_id"])
        current["origins"].append(cut["origin"])
    return merged


def effective_frame_intervals(
    intervals: list[dict[str, Any]], fps: Fraction, duration_ms: int,
    total_frames: int | None = None,
) -> list[dict[str, Any]]:
    """Map half-open source-time cuts to CFR frame ranges using ceil at both edges."""
    if fps <= 0:
        raise ValueError("invalid frame rate")
    if total_frames is None:
        total_frames = ceil_fraction(Fraction(duration_ms, 1000) * fps)
    elif isinstance(total_frames, bool) or not isinstance(total_frames, int) or total_frames <= 0:
        raise ValueError("invalid total frame count")
    result: list[dict[str, Any]] = []
    for interval in intervals:
        start_frame = max(0, min(total_frames, ceil_fraction(Fraction(interval["start_ms"], 1000) * fps)))
        end_frame = max(0, min(total_frames, ceil_fraction(Fraction(interval["end_ms"], 1000) * fps)))
        if end_frame <= start_frame:
            continue
        result.append({
            "start_frame": start_frame,
            "end_frame": end_frame,
            "start_ms": round_fraction(Fraction(start_frame * 1000, 1) / fps),
            "end_ms": round_fraction(Fraction(end_frame * 1000, 1) / fps),
            "cut_ids": list(interval["cut_ids"]),
            "origins": list(interval["origins"]),
        })
    return result


def retained_frame_segments(
    effective: list[dict[str, Any]], total_frames: int
) -> list[tuple[int, int]]:
    """Return half-open retained frame spans and reject a full-duration cut."""
    if total_frames <= 0:
        raise ValueError("invalid master frame count")
    segments: list[tuple[int, int]] = []
    cursor = 0
    for interval in effective:
        start = interval["start_frame"]
        end = interval["end_frame"]
        if start < cursor or end <= start or end > total_frames:
            raise ValueError("invalid effective frame intervals")
        if start > cursor:
            segments.append((cursor, start))
        cursor = end
    if cursor < total_frames:
        segments.append((cursor, total_frames))
    if not segments:
        raise ValueError("cut list removes the entire video")
    return segments


def build_concat_filter(
    effective: list[dict[str, Any]],
    *,
    fps: Fraction,
    total_frames: int,
    has_audio: bool,
    audio_start_offset_seconds: float = 0.0,
    normalize_cfr: bool = False,
) -> tuple[str, list[tuple[int, int]]]:
    """Build a one-pass CFR normalize, cut and concat graph."""
    segments = retained_frame_segments(effective, total_frames)
    fps_text = f"{fps.numerator}/{fps.denominator}"
    if normalize_cfr:
        video_timing = f"fps=fps={fps_text},settb=expr={fps.denominator}/{fps.numerator},setpts=N"
    else:
        video_timing = "setpts=PTS-STARTPTS"
    filters = [f"[0:v:0]setpts=PTS-STARTPTS,{video_timing}[vnorm]"]
    if has_audio:
        offset = max(-10.0, min(10.0, float(audio_start_offset_seconds)))
        filters.append(
            f"[0:a:0]asetpts=PTS-STARTPTS+({offset:.9f})/TB,"
            "aresample=48000:async=1:first_pts=0[anorm]"
        )
    video_labels: list[str] = []
    audio_labels: list[str] = []
    for index, (start_frame, end_frame) in enumerate(segments):
        video_label = f"v{index}"
        segment_timing = (
            f"settb=expr={fps.denominator}/{fps.numerator},setpts=N"
            if normalize_cfr else "setpts=PTS-STARTPTS"
        )
        filters.append(
            f"[vnorm]trim=start_frame={start_frame}:end_frame={end_frame},"
            f"{segment_timing}[{video_label}]"
        )
        video_labels.append(f"[{video_label}]")
        if has_audio:
            start_sample = round_fraction(Fraction(start_frame * SAMPLE_RATE, 1) / fps)
            end_sample = round_fraction(Fraction(end_frame * SAMPLE_RATE, 1) / fps)
            audio_label = f"a{index}"
            filters.append(
                f"[anorm]atrim=start_sample={start_sample}:end_sample={end_sample},"
                f"asetpts=PTS-STARTPTS[{audio_label}raw]"
            )
            duration = max(0.0, (end_sample - start_sample) / float(SAMPLE_RATE))
            fade = min(0.005, duration / 2.0)
            fade_filters = []
            if start_frame > 0 and fade > 0:
                fade_filters.append(f"afade=t=in:st=0:d={fade:.6f}")
            if end_frame < total_frames and fade > 0:
                fade_filters.append(f"afade=t=out:st={max(0.0, duration-fade):.6f}:d={fade:.6f}")
            if fade_filters:
                filters.append(f"[{audio_label}raw]{','.join(fade_filters)}[{audio_label}]")
            else:
                filters.append(f"[{audio_label}raw]anull[{audio_label}]")
            audio_labels.append(f"[{audio_label}]")
    if len(segments) == 1:
        filters.append(f"{video_labels[0]}null[vout]")
        if has_audio:
            filters.append(f"{audio_labels[0]}anull[aout]")
    else:
        inputs = "".join(
            video_labels[index] + (audio_labels[index] if has_audio else "")
            for index in range(len(segments))
        )
        filters.append(
            f"{inputs}concat=n={len(segments)}:v=1:a={1 if has_audio else 0}"
            + ("[vout][aout]" if has_audio else "[vout]")
        )
    return ";".join(filters), segments
