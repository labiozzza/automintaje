"""Color-profile probing and pixel/metadata handling for the video pipeline.

OpenCV exposes decoded frames as full-range BGR.  Every BGR-to-YUV rawvideo
pipe must therefore choose the output matrix and range explicitly; metadata
alone cannot undo a conversion performed with the wrong matrix.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np


_PROBE_ENTRIES = (
    "stream=index,codec_type,codec_name,pix_fmt,bits_per_raw_sample,bits_per_coded_sample,"
    "color_primaries,color_transfer,color_space,color_range,profile,level,width,height:"
    "stream_side_data:format=format_name,duration"
)

_X264_PRIMARIES = {
    "bt709": "bt709", "bt470m": "bt470m", "bt470bg": "bt470bg",
    "smpte170m": "smpte170m", "smpte240m": "smpte240m", "bt2020": "bt2020",
    "smpte431": "smpte431", "smpte432": "smpte432",
}
_X264_TRANSFERS = {
    "bt709": "bt709", "smpte170m": "smpte170m", "smpte240m": "smpte240m",
    "linear": "linear", "log100": "log100", "log316": "log316",
    "iec61966-2-1": "iec61966-2-1", "bt2020-10": "bt2020-10", "bt2020-12": "bt2020-12",
    "smpte2084": "smpte2084", "arib-std-b67": "arib-std-b67",
}
_X264_MATRICES = {
    "bt709": "bt709", "smpte170m": "smpte170m", "bt470bg": "bt470bg",
    "smpte240m": "smpte240m", "bt2020nc": "bt2020nc", "bt2020c": "bt2020c",
    "fcc": "fcc", "gbr": "gbr",
}


def _norm(value: Any) -> str:
    value = str(value or "").strip().lower()
    return "" if value in {"unknown", "unspecified", "reserved", "n/a"} else value


def _pixel_depth(pixel_format: str, raw_bits: Any, coded_bits: Any) -> int | None:
    for value in (raw_bits, coded_bits):
        try:
            parsed = int(value)
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            pass
    match = re.search(r"(?:p|le|be)(9|10|12|14|16)(?:$|le|be)", pixel_format.lower())
    if match:
        return int(match.group(1))
    if pixel_format:
        return 8
    return None


def probe_color_profile(video_path: Path | str, ffprobe: str | None = None) -> dict[str, Any]:
    """Return the source stream's coded color properties without guessing tags."""
    path = Path(video_path)
    result = subprocess.run(
        [ffprobe or shutil.which("ffprobe") or "ffprobe", "-v", "error",
         "-select_streams", "v:0", "-show_streams", "-show_format",
         "-show_entries", _PROBE_ENTRIES, "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError("ffprobe could not read video color profile: " + result.stderr.strip()[-400:])
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError("ffprobe found no video stream")
    stream = streams[0]
    pixel_format = str(stream.get("pix_fmt") or "")
    side_data = stream.get("side_data_list") or []
    hdr_side_data = [
        item for item in side_data
        if any(term in str(item.get("side_data_type") or "").lower()
               for term in ("mastering display", "content light", "dynamic hdr", "dolby vision", "dovi"))
    ]
    transfer = _norm(stream.get("color_transfer"))
    return {
        "codec_name": _norm(stream.get("codec_name")),
        "pixel_format": pixel_format,
        "bit_depth": _pixel_depth(pixel_format, stream.get("bits_per_raw_sample"),
                                  stream.get("bits_per_coded_sample")),
        "primaries": _norm(stream.get("color_primaries")),
        "transfer": transfer,
        "matrix": _norm(stream.get("color_space")),
        "range": _norm(stream.get("color_range")),
        "profile": _norm(stream.get("profile")),
        "level": stream.get("level"),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "side_data": side_data,
        "hdr_metadata": hdr_side_data,
        "format_name": str((payload.get("format") or {}).get("format_name") or ""),
        "duration": (payload.get("format") or {}).get("duration"),
        "hdr": transfer in {"smpte2084", "arib-std-b67"} or bool(hdr_side_data),
    }


def is_bt709_sdr(profile: dict[str, Any]) -> bool:
    return (
        not profile.get("hdr")
        and profile.get("primaries") == "bt709"
        and profile.get("transfer") == "bt709"
        and profile.get("matrix") == "bt709"
        and profile.get("range") in {"tv", "mpeg", "pc", "jpeg"}
    )


def _scale_matrix(profile: dict[str, Any]) -> tuple[str, bool]:
    matrix = _norm(profile.get("matrix"))
    aliases = {
        "bt709": "bt709", "smpte170m": "smpte170m", "bt470bg": "bt470bg",
        "bt2020nc": "bt2020", "bt2020c": "bt2020", "fcc": "fcc",
        "smpte240m": "smpte240m",
    }
    if matrix in aliases:
        return aliases[matrix], False
    # FFmpeg still must choose a conversion matrix for RGB->YUV.  Use the
    # conventional SD/HD matrix by raster size, but leave the source metadata
    # unknown rather than claiming that this inferred choice was authored.
    if int(profile.get("width") or 0) >= 1280 or int(profile.get("height") or 0) >= 720:
        return "bt709", True
    return "smpte170m", True


def _scale_range(profile: dict[str, Any]) -> tuple[str, bool]:
    value = _norm(profile.get("range"))
    if value in {"pc", "jpeg", "full"}:
        return "full", False
    if value in {"tv", "mpeg", "limited"}:
        return "limited", False
    pixel_format = str(profile.get("pixel_format") or "").lower()
    if pixel_format.startswith("yuvj"):
        return "full", True
    # Plain YUV pixel formats use MPEG/limited range by convention.  This is
    # a decoder fallback only; no source range metadata is invented.
    return "limited", True


def raw_bgr_to_yuv_filter(profile: dict[str, Any]) -> str:
    """Explicit RGB(full)->YUV matrix/range conversion for OpenCV BGR pipes."""
    if profile.get("hdr"):
        raise ValueError("HDR must be tone-mapped before OpenCV raw BGR encoding")
    matrix, _inferred_matrix = _scale_matrix(profile)
    # Normalize OpenCV's full-range BGR to standard limited-range YUV.  This
    # changes the encoded range but not the represented RGB appearance.
    color_range = "limited"
    return (
        "scale=in_range=pc:"
        f"out_range={color_range}:out_color_matrix={matrix}:flags=accurate_rnd+full_chroma_int"
    )


def raw_bgr_output_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Accurate labels for the normalized YUV representation of raw BGR."""
    out = dict(profile)
    # Matrix inference is used only to perform RGB->YUV math.  Keep the source
    # label unknown unless ffprobe actually supplied it; raster conventions
    # are not evidence that the original was authored in that matrix.
    out["range"] = "tv"
    out["hdr"] = False
    return out


def raw_bgr_decoder_filter(profile: dict[str, Any]) -> str:
    """Decode YUV to full-range BGR using the source matrix/range explicitly."""
    if profile.get("hdr"):
        raise ValueError("HDR must be tone-mapped before 8-bit BGR decoding")
    matrix, _inferred_matrix = _scale_matrix(profile)
    input_range, _inferred_range = _scale_range(profile)
    return (
        f"scale=in_color_matrix={matrix}:in_range={input_range}:out_range=full:"
        "flags=accurate_rnd+full_chroma_int,format=bgr24"
    )


class RawBGRReader:
    """Decode video frames through FFmpeg so OpenCV does not silently assume
    BT.601 for a BT.709 stream. Frame pixels remain BGR for existing CV code.
    """

    def __init__(self, path: Path | str, width: int, height: int,
                 profile: dict[str, Any], ffmpeg: str | None = None) -> None:
        if profile.get("hdr"):
            raise ValueError("HDR must be tone-mapped before OpenCV BGR processing")
        self.width = int(width)
        self.height = int(height)
        self.frame_bytes = self.width * self.height * 3
        self.process = subprocess.Popen(
            [ffmpeg or shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-i", str(path),
             "-map", "0:v:0", "-vf", raw_bgr_decoder_filter(profile),
             "-fps_mode", "passthrough", "-an", "-sn", "-f", "rawvideo",
             "-pix_fmt", "bgr24", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=self.frame_bytes,
        )
        if self.process.stdout is None:
            raise RuntimeError("FFmpeg raw BGR stdout is unavailable")
        self._eof = False

    def read(self) -> tuple[bool, np.ndarray | None]:
        assert self.process.stdout is not None
        data = bytearray(self.frame_bytes)
        view = memoryview(data)
        offset = 0
        while offset < self.frame_bytes:
            chunk = self.process.stdout.read(self.frame_bytes - offset)
            if not chunk:
                if offset == 0:
                    self._eof = True
                    return False, None
                raise RuntimeError("truncated frame from FFmpeg raw BGR decoder")
            view[offset:offset + len(chunk)] = chunk
            offset += len(chunk)
        return True, np.frombuffer(data, dtype=np.uint8).reshape((self.height, self.width, 3))

    def close(self) -> None:
        if self.process.stdout is not None:
            self.process.stdout.close()
        if not self._eof and self.process.poll() is None:
            self.process.terminate()
        code = self.process.wait()
        if self._eof and code != 0:
            raise RuntimeError("FFmpeg raw BGR decoder failed with code {}".format(code))


def yuv_preserving_filter(profile: dict[str, Any]) -> str | None:
    """Make a YUV-to-YUV resize/flip explicitly retain the probed encoding."""
    matrix, _inferred_matrix = _scale_matrix(profile)
    range_value, _inferred_range = _scale_range(profile)
    return (
        f"scale=in_color_matrix={matrix}:out_color_matrix={matrix}:"
        f"in_range={range_value}:out_range={range_value}:flags=accurate_rnd+full_chroma_int"
    )


def encoder_color_args(profile: dict[str, Any], codec: str = "h264") -> list[str]:
    """Attach only probed color labels; never invent missing BT.709 metadata."""
    args: list[str] = []
    for flag, key in (("-color_primaries", "primaries"), ("-color_trc", "transfer"),
                      ("-colorspace", "matrix")):
        value = _norm(profile.get(key))
        if value:
            args += [flag, value]
    color_range = _norm(profile.get("range"))
    if color_range in {"tv", "mpeg", "limited"}:
        args += ["-color_range", "tv"]
    elif color_range in {"pc", "jpeg", "full"}:
        args += ["-color_range", "pc"]

    codec_name = str(codec or "h264").lower()
    if codec_name in {"h264", "libx264"}:
        params: list[str] = []
        primaries = _norm(profile.get("primaries"))
        transfer = _norm(profile.get("transfer"))
        matrix = _norm(profile.get("matrix"))
        if primaries in _X264_PRIMARIES:
            params.append("colorprim=" + _X264_PRIMARIES[primaries])
        if transfer in _X264_TRANSFERS:
            params.append("transfer=" + _X264_TRANSFERS[transfer])
        if matrix in _X264_MATRICES:
            params.append("colormatrix=" + _X264_MATRICES[matrix])
        if color_range in {"pc", "jpeg", "full"}:
            params.append("fullrange=on")
        elif color_range in {"tv", "mpeg", "limited"}:
            params.append("fullrange=off")
        if params:
            args += ["-x264-params", ":".join(params)]
    elif codec_name in {"hevc", "libx265"}:
        params = []
        primaries = _norm(profile.get("primaries"))
        transfer = _norm(profile.get("transfer"))
        matrix = _norm(profile.get("matrix"))
        if primaries in _X264_PRIMARIES:
            params.append("colorprim=" + _X264_PRIMARIES[primaries])
        if transfer in _X264_TRANSFERS:
            params.append("transfer=" + _X264_TRANSFERS[transfer])
        if matrix in _X264_MATRICES:
            params.append("colormatrix=" + _X264_MATRICES[matrix])
        if color_range in {"pc", "jpeg", "full"}:
            params.append("range=full")
        elif color_range in {"tv", "mpeg", "limited"}:
            params.append("range=limited")
        if params:
            args += ["-x265-params", ":".join(params)]
    return args


def sdr_to_bt709_filter(profile: dict[str, Any], output_range: str | None = None) -> str | None:
    """Convert a fully described non-HDR SDR YUV source to actual BT.709 pixels."""
    if profile.get("hdr") or is_bt709_sdr(profile):
        return None
    if not all(_norm(profile.get(key)) for key in ("primaries", "transfer", "matrix", "range")):
        return None
    matrix = _norm(profile.get("matrix"))
    if matrix in {"bt2020nc", "bt2020c"}:
        matrix = "2020_ncl" if matrix == "bt2020nc" else "2020_cl"
    output_range = output_range or ("full" if profile.get("range") in {"pc", "jpeg"} else "limited")
    input_range = "full" if profile.get("range") in {"pc", "jpeg"} else "limited"
    return (
        f"zscale=primariesin={profile['primaries']}:transferin={profile['transfer']}:"
        f"matrixin={matrix}:rangein={input_range}:primaries=bt709:transfer=bt709:"
        f"matrix=bt709:range={output_range},format=yuv420p"
    )


def hdr_to_bt709_filter(profile: dict[str, Any]) -> str:
    """Tone-map PQ/HLG BT.2020 to limited-range SDR BT.709 pixels."""
    transfer = _norm(profile.get("transfer"))
    if transfer not in {"smpte2084", "arib-std-b67"}:
        raise ValueError("HDR transfer must be explicitly tagged PQ or HLG")
    if _norm(profile.get("primaries")) not in {"bt2020", ""}:
        raise ValueError("HDR tone mapping requires BT.2020 or explicitly absent primaries")
    matrix = _norm(profile.get("matrix"))
    if matrix in {"bt2020nc", ""}:
        matrix = "2020_ncl"
    elif matrix == "bt2020c":
        matrix = "2020_cl"
    else:
        raise ValueError("HDR tone mapping requires a BT.2020 matrix")
    input_range = "full" if _norm(profile.get("range")) in {"pc", "jpeg"} else "limited"
    return (
        f"zscale=primariesin=bt2020:transferin={transfer}:matrixin={matrix}:rangein={input_range}:"
        "primaries=bt2020:transfer=linear:matrix=gbr:npl=100,format=gbrpf32le,"
        "tonemap=mobius:desat=0,zscale=primaries=bt709:transfer=bt709:matrix=bt709:"
        "range=limited,format=yuv420p"
    )


def bt709_sdr_profile(color_range: str = "tv") -> dict[str, Any]:
    return {
        "primaries": "bt709", "transfer": "bt709", "matrix": "bt709",
        "range": color_range if color_range in {"tv", "pc"} else "tv", "hdr": False,
    }
