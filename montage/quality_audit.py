#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnostic tooling for video quality (Stage A).

Collects ffprobe metadata for arbitrary files and computes PSNR/SSIM/VMAF
between a reference and one or more distorted videos using ffmpeg.  Does not
modify any source video; all work happens in a temporary directory or in an
explicitly provided output directory.

Usage:
  python -m montage.quality_audit --inputs a.mp4 b.mp4 --output report.json
  python -m montage.quality_audit --reference ref.mp4 --distorted out.mp4 --metrics psnr,ssim,vmaf
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

FFPROBE = shutil.which("ffprobe") or "ffprobe"
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"


def _run(cmd: List[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout,
    )


def probe_video(path: Path) -> Dict[str, Any]:
    """Collect structured ffprobe metadata for *path*."""
    cmd = [
        FFPROBE, "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ]
    result = _run(cmd)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr.strip()[-500:]}")
    data = json.loads(result.stdout or "{}")
    video_stream = None
    audio_stream = None
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video" and video_stream is None:
            video_stream = stream
        elif stream.get("codec_type") == "audio" and audio_stream is None:
            audio_stream = stream
    fmt = data.get("format") or {}
    out: Dict[str, Any] = {"path": str(path), "format": {}}
    if video_stream:
        out["video"] = {
            "codec": video_stream.get("codec_name"),
            "profile": video_stream.get("profile"),
            "level": video_stream.get("level"),
            "width": int(video_stream.get("width") or 0),
            "height": int(video_stream.get("height") or 0),
            "pix_fmt": video_stream.get("pix_fmt"),
            "bit_depth": video_stream.get("bits_per_raw_sample"),
            "r_frame_rate": video_stream.get("r_frame_rate"),
            "avg_frame_rate": video_stream.get("avg_frame_rate"),
            "time_base": video_stream.get("time_base"),
            "color_range": video_stream.get("color_range"),
            "color_space": video_stream.get("color_space"),
            "color_transfer": video_stream.get("color_transfer"),
            "color_primaries": video_stream.get("color_primaries"),
            "duration": float(video_stream.get("duration") or 0) or None,
            "nb_frames": video_stream.get("nb_frames"),
        }
    if audio_stream:
        out["audio"] = {
            "codec": audio_stream.get("codec_name"),
            "channels": audio_stream.get("channels"),
            "sample_rate": audio_stream.get("sample_rate"),
            "bit_rate": audio_stream.get("bit_rate"),
        }
    out["format"] = {
        "container": fmt.get("format_name"),
        "duration": float(fmt.get("duration") or 0) or None,
        "size_bytes": int(fmt.get("size") or 0) or None,
        "bit_rate": int(fmt.get("bit_rate") or 0) or None,
        "tags": fmt.get("tags") or {},
    }
    return out


def _ffmpeg_metric_filter(metric: str) -> str:
    m = metric.lower()
    if m == "psnr":
        return "psnr"
    if m == "ssim":
        return "ssim"
    if m == "vmaf":
        return "libvmaf"
    if m == "xpsnr":
        return "xpsnr"
    raise ValueError(f"unknown metric: {metric}")


def compute_metric(reference: Path, distorted: Path, metric: str,
                   workdir: Path | None = None) -> Dict[str, Any]:
    """Compute a single quality metric between *reference* and *distorted*.

    Returns a dict with the metric name and parsed aggregate values.  ffmpeg
    writes the per-frame log to stderr; we parse the final summary line.
    """
    workdir = workdir or Path(tempfile.mkdtemp(prefix="qa_"))
    workdir.mkdir(parents=True, exist_ok=True)
    filt = _ffmpeg_metric_filter(metric)
    log_path = workdir / f"{filt}.log"
    cmd = [
        FFMPEG, "-y", "-hide_banner", "-nostats",
        "-i", str(reference),
        "-i", str(distorted),
        "-lavfi", f"[0:v][1:v]{filt}",
        "-f", "null", "-",
    ]
    result = _run(cmd, timeout=600)
    log_path.write_text(result.stderr or "", encoding="utf-8", errors="replace")
    parsed = _parse_metric_output(metric, result.stderr or "")
    parsed["metric"] = metric
    parsed["reference"] = str(reference)
    parsed["distorted"] = str(distorted)
    parsed["log_path"] = str(log_path)
    parsed["ffmpeg_returncode"] = result.returncode
    return parsed


def _parse_metric_output(metric: str, text: str) -> Dict[str, Any]:
    m = metric.lower()
    out: Dict[str, Any] = {}
    # PSNR: "PSNR y:.. u:.. v:.. average:.. min:.. max:.."
    if m == "psnr":
        for line in reversed(text.splitlines()):
            if "average:" in line.lower():
                # find average value
                import re
                match = re.search(r"average:\s*([0-9.inf]+)", line, re.IGNORECASE)
                if match:
                    val = match.group(1)
                    out["average"] = float("inf") if val.lower() == "inf" else float(val)
                ymin = re.search(r"y:\s*([0-9.]+)", line, re.IGNORECASE)
                if ymin:
                    out["y"] = float(ymin.group(1))
                break
        return out
    if m == "ssim":
        for line in reversed(text.splitlines()):
            if "All:" in line:
                import re
                match = re.search(r"All:\s*([0-9.]+)", line)
                if match:
                    out["all"] = float(match.group(1))
                ym = re.search(r"Y:\s*([0-9.]+)", line)
                if ym:
                    out["y"] = float(ym.group(1))
                break
        return out
    if m == "vmaf":
        for line in reversed(text.splitlines()):
            if "VMAF score:" in line or "vmaf" in line.lower():
                import re
                match = re.search(r"VMAF score:\s*([0-9.]+)", line, re.IGNORECASE)
                if match:
                    out["score"] = float(match.group(1))
                    break
                match = re.search(r"vmaf[_ ]?score[:=]\s*([0-9.]+)", line, re.IGNORECASE)
                if match:
                    out["score"] = float(match.group(1))
                    break
        # fallback: parse last number
        if "score" not in out:
            import re
            nums = re.findall(r"([0-9]+\.[0-9]+)", text)
            if nums:
                out["score"] = float(nums[-1])
        return out
    if m == "xpsnr":
        for line in reversed(text.splitlines()):
            if "average:" in line.lower() or "xpsnr" in line.lower():
                import re
                match = re.search(r"average:\s*([0-9.inf]+)", line, re.IGNORECASE)
                if match:
                    val = match.group(1)
                    out["average"] = float("inf") if val.lower() == "inf" else float(val)
                    break
        return out
    return out


def audit_files(paths: List[Path]) -> Dict[str, Any]:
    reports = []
    for p in paths:
        try:
            reports.append(probe_video(p))
        except Exception as exc:
            reports.append({"path": str(p), "error": str(exc)})
    return {"files": reports}


def build_report(reference: Path | None, distorted: List[Path],
                 metrics: List[str], workdir: Path | None = None) -> Dict[str, Any]:
    report: Dict[str, Any] = {"reference": str(reference) if reference else None,
                              "distorted": []}
    wd = workdir or Path(tempfile.mkdtemp(prefix="quality_audit_"))
    wd.mkdir(parents=True, exist_ok=True)
    for d in distorted:
        entry: Dict[str, Any] = {"path": str(d)}
        try:
            entry["probe"] = probe_video(d)
        except Exception as exc:
            entry["probe_error"] = str(exc)
        if reference is not None:
            entry["metrics"] = {}
            for metric in metrics:
                try:
                    entry["metrics"][metric] = compute_metric(reference, d, metric, wd)
                except Exception as exc:
                    entry["metrics"][metric] = {"error": str(exc)}
        report["distorted"].append(entry)
    if reference is not None:
        try:
            report["reference_probe"] = probe_video(reference)
        except Exception as exc:
            report["reference_probe_error"] = str(exc)
    report["workdir"] = str(wd)
    return report


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Video quality diagnostic tool")
    parser.add_argument("--inputs", nargs="*", default=[], help="Files to probe")
    parser.add_argument("--reference", type=Path, default=None, help="Reference video for metrics")
    parser.add_argument("--distorted", nargs="*", default=[], type=Path, help="Distorted videos for metrics")
    parser.add_argument("--metrics", default="psnr,ssim", help="Comma-separated metrics")
    parser.add_argument("--output", type=Path, default=None, help="Output JSON path")
    parser.add_argument("--workdir", type=Path, default=None, help="Working directory for metric logs")
    args = parser.parse_args(argv)

    metrics = [m.strip() for m in args.metrics.split(",") if m.strip()]
    distorted = list(args.distorted)
    # If --inputs given without --distorted, probe only
    if args.inputs and not distorted and args.reference is None:
        report = audit_files([Path(p) for p in args.inputs])
    else:
        report = build_report(args.reference, distorted, metrics, args.workdir)

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
