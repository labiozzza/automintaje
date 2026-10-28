"""Structural and color validation for Motion-composited output files."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any, Dict

from .source_snapshot import probe_video


class MotionQualityError(RuntimeError):
    pass


def validate_composite(source_path: Path, output_path: Path, duration_ms: int) -> Dict[str, Any]:
    source = probe_video(source_path)
    output = probe_video(output_path)
    if output["width"] != source["width"] or output["height"] != source["height"]:
        raise MotionQualityError("Разрешение Motion-ролика изменилось относительно мастера")
    if bool(output["audio_present"]) != bool(source["audio_present"]):
        raise MotionQualityError("Наличие аудиодорожки Motion-ролика не совпадает с мастером")
    source_fps = Fraction(int(source["fps_num"]), int(source["fps_den"]))
    output_fps = Fraction(int(output["fps_num"]), int(output["fps_den"]))
    if source_fps <= 0 or output_fps <= 0 or abs(float(source_fps - output_fps)) > 0.02:
        raise MotionQualityError("Частота кадров Motion-ролика отличается от мастера")
    source_duration = int(source.get("duration_ms") or duration_ms)
    output_duration = int(output.get("duration_ms") or 0)
    one_frame_ms = int(round(1000.0 * float(source_fps.denominator) / float(source_fps.numerator)))
    if abs(output_duration - source_duration) > max(120, one_frame_ms + 80):
        raise MotionQualityError("Длительность Motion-ролика отличается более чем на один кадр")
    source_color = source.get("color") or {}
    output_color = output.get("color") or {}
    if source_color.get("hdr") and not output_color.get("hdr"):
        raise MotionQualityError("HDR-мастер неожиданно преобразован в SDR")
    for key in ("primaries", "transfer", "matrix", "range"):
        left, right = source_color.get(key), output_color.get(key)
        if left and right and left != right:
            raise MotionQualityError("Изменилась цветовая характеристика %s (%s → %s)" % (key, left, right))
    return {
        "source": {"duration_ms": source_duration, "fps": "%d/%d" % (source_fps.numerator, source_fps.denominator), "color": source_color},
        "output": {"duration_ms": output_duration, "fps": "%d/%d" % (output_fps.numerator, output_fps.denominator), "color": output_color},
        "verified": True,
    }
