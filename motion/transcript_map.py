"""Map transcript intervals to the ready montage's output timeline."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Tuple


_SRT_ENTRY_RE = re.compile(
    r"(?:^|\n)\s*(?:\d+\s*\n)?"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})[^\n]*\n"
    r"(.*?)(?=\n\s*\n|\Z)",
    flags=re.S,
)


def _time_ms(hours: str, minutes: str, seconds: str, milliseconds: str) -> int:
    return (((int(hours) * 60 + int(minutes)) * 60 + int(seconds)) * 1000) + int(milliseconds)


def parse_srt(srt_text: str, limit: int = 6000) -> List[dict]:
    items = []
    for match in _SRT_ENTRY_RE.finditer(str(srt_text or "").replace("\r", "")):
        start = _time_ms(*match.groups()[:4])
        end = _time_ms(*match.groups()[4:8])
        text = " ".join(line.strip() for line in match.group(9).splitlines() if line.strip())
        if text and end > start:
            items.append({"start_ms": start, "end_ms": end, "text": text, "timing_source": "srt"})
        if len(items) >= limit:
            break
    return items


def _segments_from_transcript(segments: Optional[Iterable[dict]], srt_text: str) -> Tuple[List[dict], bool, str]:
    normalized = []
    word_level = True
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        words = segment.get("word_timestamps") or segment.get("words") or []
        if isinstance(words, list) and words:
            for word in words:
                if not isinstance(word, dict):
                    continue
                try:
                    start = int(round(float(word.get("start")) * 1000))
                    end = int(round(float(word.get("end")) * 1000))
                except (TypeError, ValueError):
                    continue
                text = str(word.get("word") or word.get("text") or "").strip()
                if text and end > start:
                    normalized.append({"start_ms": start, "end_ms": end, "text": text, "timing_source": "word_alignment"})
        else:
            word_level = False
            try:
                start = int(round(float(segment.get("start")) * 1000))
                end = int(round(float(segment.get("end")) * 1000))
            except (TypeError, ValueError):
                continue
            text = str(segment.get("text") or "").strip()
            if text and end > start:
                normalized.append({"start_ms": start, "end_ms": end, "text": text, "timing_source": "segment_alignment"})
    if normalized:
        normalized.sort(key=lambda item: (item["start_ms"], item["end_ms"]))
        return normalized, word_level and all(item.get("timing_source") == "word_alignment" for item in normalized), "transcript_segments"
    parsed_srt = parse_srt(srt_text)
    return parsed_srt, False, "srt" if parsed_srt else "unavailable"


def _map_interval(start_ms: int, end_ms: int, timeline_segments: Optional[list]) -> List[Tuple[int, int]]:
    if not timeline_segments:
        return [(start_ms, end_ms)]
    mapped = []
    for item in timeline_segments:
        if not isinstance(item, dict):
            continue
        try:
            source_start = int(item["source_start_ms"])
            source_end = int(item["source_end_ms"])
            output_start = int(item["output_start_ms"])
            output_end = int(item["output_end_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        overlap_start = max(start_ms, source_start)
        overlap_end = min(end_ms, source_end)
        if overlap_end <= overlap_start or source_end <= source_start:
            continue
        # EDL segments are frame-aligned and have a one-to-one offset mapping.
        mapped_start = output_start + overlap_start - source_start
        mapped_end = output_start + overlap_end - source_start
        if output_end > output_start:
            mapped_end = min(mapped_end, output_end)
        if mapped_end > mapped_start:
            mapped.append((mapped_start, mapped_end))
    return mapped


def build_output_transcript(segments: Optional[Iterable[dict]], srt_text: str,
                            timeline_segments: Optional[list] = None) -> dict:
    source, word_aligned, source_kind = _segments_from_transcript(segments, srt_text)
    output = []
    all_words_exact = bool(source) and word_aligned
    for item in source:
        pieces = _map_interval(item["start_ms"], item["end_ms"], timeline_segments)
        covered_ms = sum(end - start for start, end in pieces)
        piece_is_exact = word_aligned and abs(covered_ms - (item["end_ms"] - item["start_ms"])) <= 2
        if not piece_is_exact:
            all_words_exact = False
        for piece_index, (start, end) in enumerate(pieces):
            output.append({
                "start_ms": int(start),
                "end_ms": int(end),
                "text": item["text"],
                "timing_source": item.get("timing_source") or source_kind,
                "source_start_ms": item["start_ms"],
                "source_end_ms": item["end_ms"],
                "split_by_edit": len(pieces) > 1,
                "piece_index": piece_index,
                "word_aligned": bool(piece_is_exact),
            })
    output.sort(key=lambda item: (item["start_ms"], item["end_ms"]))
    edl_applied = bool(timeline_segments)
    exact = bool(output) and all_words_exact
    return {
        "segments": output[:6000],
        "word_aligned": exact,
        "timing_source": source_kind,
        "edl_applied": edl_applied,
        "exact_word_sync_available": exact,
        "note": "" if exact else "Точные таймкоды слов недоступны; синхронизация графики к отдельным словам ограничена.",
    }
