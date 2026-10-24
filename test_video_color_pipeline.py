from __future__ import annotations

import shutil
import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import cv2
import numpy as np

from montage.color_pipeline import (
    RawBGRReader,
    bt709_sdr_profile,
    encoder_color_args,
    hdr_to_bt709_filter,
    probe_color_profile,
    raw_bgr_output_profile,
    raw_bgr_to_yuv_filter,
    sdr_to_bt709_filter,
)
from montage.youtube_publisher import YouTubePublisher


HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
WIDTH = 720
HEIGHT = 1280
FPS = 25


def _chart_rgb() -> np.ndarray:
    colors = [
        (255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 255, 255),
        (255, 0, 255), (255, 255, 0), (128, 128, 128), (32, 32, 32),
    ]
    frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    for index, color in enumerate(colors):
        frame[:, index * WIDTH // len(colors):(index + 1) * WIDTH // len(colors)] = color
    return frame


def _run(command: list[str], stdin: bytes | None = None) -> None:
    result = subprocess.run(command, input=stdin, capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-1000:])


def _write_sdr_chart(path: Path, color_range: str) -> Path:
    frame = _chart_rgb()
    out_range = "full" if color_range == "pc" else "limited"
    full_flag = "on" if color_range == "pc" else "off"
    _run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "{}x{}".format(WIDTH, HEIGHT),
        "-r", str(FPS), "-i", "-",
        "-vf", "scale=in_range=pc:out_range={}:out_color_matrix=bt709,format=yuv420p".format(out_range),
        "-frames:v", "3", "-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-color_range", "pc" if color_range == "pc" else "tv",
        "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709:fullrange=" + full_flag,
        "-an", str(path),
    ], frame.tobytes() * 3)
    return path


def _read_bgr(path: Path, profile: dict) -> list[np.ndarray]:
    reader = RawBGRReader(path, WIDTH, HEIGHT, profile)
    frames = []
    try:
        while True:
            ok, frame = reader.read()
            if not ok:
                break
            frames.append(frame.copy())
    finally:
        reader.close()
    return frames


def _encode_open_cv_output(source: Path, frames: list[np.ndarray], output: Path,
                           profile: dict, explicit_color: bool) -> None:
    command = [
        "ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", "{}x{}".format(WIDTH, HEIGHT), "-r", str(FPS), "-i", "-",
    ]
    if explicit_color:
        command += ["-vf", raw_bgr_to_yuv_filter(profile)]
    command += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-pix_fmt", "yuv420p"]
    if explicit_color:
        command += encoder_color_args(raw_bgr_output_profile(profile), "h264")
    elif profile.get("range") == "tv":
        # Reproduce the old tagged-but-uncontrolled raw BGR conversion.
        command += encoder_color_args(bt709_sdr_profile(), "h264")
    command += ["-an", str(output)]
    _run(command, b"".join(frame.tobytes() for frame in frames))


def _mae(reference: list[np.ndarray], candidate: list[np.ndarray]) -> float:
    values = [
        np.abs(a.astype(np.int16) - b.astype(np.int16)).mean()
        for a, b in zip(reference, candidate)
    ]
    if len(values) != len(reference) or not values:
        raise AssertionError("frame counts differ or no frames were decoded")
    return float(np.mean(values))


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe required for color-pipeline integration tests")
class VideoColorPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_sdr_bt709_and_full_range_roundtrip_matches_rgb_reference(self):
        for color_range in ("tv", "pc"):
            with self.subTest(color_range=color_range):
                source = _write_sdr_chart(self.root / (color_range + ".mp4"), color_range)
                profile = probe_color_profile(source)
                self.assertEqual((profile["primaries"], profile["transfer"], profile["matrix"]),
                                 ("bt709", "bt709", "bt709"))
                self.assertEqual(profile["range"], color_range)
                reference = _read_bgr(source, profile)

                corrected = self.root / (color_range + "-corrected.mp4")
                _encode_open_cv_output(source, reference, corrected, profile, explicit_color=True)
                corrected_profile = probe_color_profile(corrected)
                self.assertEqual(corrected_profile["range"], "tv")
                self.assertEqual(corrected_profile["matrix"], "bt709")
                corrected_mae = _mae(reference, _read_bgr(corrected, corrected_profile))

                # Baseline approximates the former cv2.VideoCapture + implicit
                # raw BGR conversion, with the previous fixed BT.709 label for TV.
                cv = cv2.VideoCapture(str(source))
                legacy_frames = []
                while True:
                    ok, frame = cv.read()
                    if not ok:
                        break
                    legacy_frames.append(frame)
                cv.release()
                legacy = self.root / (color_range + "-legacy.mp4")
                _encode_open_cv_output(source, legacy_frames, legacy, profile, explicit_color=False)
                legacy_profile = probe_color_profile(legacy)
                legacy_mae = _mae(reference, _read_bgr(legacy, legacy_profile))

                self.assertLess(corrected_mae, 1.0)
                self.assertGreater(legacy_mae, corrected_mae * 4)

    def test_stream_copy_preserves_full_range_vui(self):
        source = _write_sdr_chart(self.root / "copy-source.mp4", "pc")
        before = probe_color_profile(source)
        publisher = YouTubePublisher.__new__(YouTubePublisher)
        publisher.log_path = self.root / "youtube.log"
        output = self.root / "copy-remux.mp4"
        publisher._remux_copy(source, output, lambda _p, _detail: None)
        after = probe_color_profile(output)
        for key in ("primaries", "transfer", "matrix", "range", "pixel_format", "bit_depth"):
            self.assertEqual(after[key], before[key], key)

    def test_unknown_source_tags_are_not_filled_with_bt709(self):
        from montage.color_pipeline import raw_bgr_decoder_filter

        unknown = {"width": WIDTH, "height": HEIGHT, "hdr": False}
        out_args = encoder_color_args(raw_bgr_output_profile(unknown), "h264")
        self.assertNotIn("-color_primaries", out_args)
        self.assertNotIn("-color_trc", out_args)
        self.assertNotIn("-colorspace", out_args)
        # The conversion still selects a resolution-conventional transform;
        # it does not claim that this was the source's original metadata.
        self.assertIn("out_color_matrix=bt709", raw_bgr_to_yuv_filter(unknown))
        self.assertIn("in_color_matrix=bt709", raw_bgr_decoder_filter(unknown))

    def test_probe_preserves_hdr_side_data(self):
        payload = {
            "streams": [{
                "codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuv420p10le",
                "width": WIDTH, "height": HEIGHT, "color_primaries": "bt2020",
                "color_transfer": "bt709", "color_space": "bt2020nc", "color_range": "tv",
                "side_data_list": [{
                    "side_data_type": "Mastering display metadata", "max_luminance": 1000.0,
                }],
            }],
            "format": {"format_name": "mov", "duration": "1.0"},
        }
        fake = mock.Mock(returncode=0, stdout=json.dumps(payload), stderr="")
        with mock.patch("montage.color_pipeline.subprocess.run", return_value=fake):
            profile = probe_color_profile("not-read.mp4")
        self.assertEqual(profile["bit_depth"], 10)
        self.assertTrue(profile["hdr"])
        self.assertEqual(profile["hdr_metadata"][0]["max_luminance"], 1000.0)

    def test_sdr_bt2020_is_pixel_converted_before_bt709_tagging(self):
        source_709 = _write_sdr_chart(self.root / "source709.mp4", "tv")
        source_2020 = self.root / "source2020.mp4"
        _run([
            "ffmpeg", "-y", "-v", "error", "-i", str(source_709),
            "-vf", (
                "zscale=matrixin=bt709:primariesin=bt709:transferin=bt709:rangein=limited:"
                "matrix=gbr:primaries=bt2020:transfer=bt709,format=gbrpf32le,"
                "zscale=primaries=bt2020:transfer=bt709:matrix=2020_ncl:range=limited,format=yuv420p"
            ),
            "-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt2020", "-color_trc", "bt709",
            "-colorspace", "bt2020nc", "-color_range", "tv",
            "-x264-params", "colorprim=bt2020:transfer=bt709:colormatrix=bt2020nc:fullrange=off",
            "-an", str(source_2020),
        ])
        profile_2020 = probe_color_profile(source_2020)
        self.assertEqual(profile_2020["primaries"], "bt2020")
        self.assertFalse(profile_2020["hdr"])
        transform = sdr_to_bt709_filter(profile_2020)
        self.assertIsNotNone(transform)
        target = self.root / "converted709.mp4"
        _run([
            "ffmpeg", "-y", "-v", "error", "-i", str(source_2020),
            "-vf", transform, "-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv420p",
            *encoder_color_args(bt709_sdr_profile(), "h264"), "-an", str(target),
        ])
        target_profile = probe_color_profile(target)
        self.assertEqual((target_profile["primaries"], target_profile["transfer"], target_profile["matrix"]),
                         ("bt709", "bt709", "bt709"))
        ref = _read_bgr(source_709, probe_color_profile(source_709))
        actual = _read_bgr(target, target_profile)
        # Two 4:2:0 encodes plus BT.2020/709 gamut adaptation are measured in
        # the common decoded BGR space; the test guards against a gross shift.
        self.assertLess(_mae(ref, actual), 7.0)

    def test_pq_and_hlg_tone_map_to_bt709_metadata_and_pixels(self):
        source = _write_sdr_chart(self.root / "sdr-reference.mp4", "tv")
        reference = _read_bgr(source, probe_color_profile(source))
        for transfer in ("smpte2084", "arib-std-b67"):
            with self.subTest(transfer=transfer):
                hdr = self.root / (transfer + ".mp4")
                encoded = (
                    "zscale=matrixin=bt709:primariesin=bt709:transferin=bt709:rangein=limited:"
                    "matrix=gbr:primaries=bt2020:transfer=linear,format=gbrpf32le,"
                    "zscale=primaries=bt2020:transfer={}:matrix=2020_ncl:range=limited,format=yuv420p10le"
                ).format(transfer)
                _run([
                    "ffmpeg", "-y", "-v", "error", "-i", str(source), "-vf", encoded,
                    "-c:v", "libx265", "-pix_fmt", "yuv420p10le",
                    "-color_primaries", "bt2020", "-color_trc", transfer,
                    "-colorspace", "bt2020nc", "-color_range", "tv",
                    "-x265-params", "colorprim=bt2020:transfer={}:colormatrix=bt2020nc:range=limited".format(transfer),
                    "-an", str(hdr),
                ])
                hdr_profile = probe_color_profile(hdr)
                self.assertTrue(hdr_profile["hdr"])
                self.assertEqual(hdr_profile["bit_depth"], 10)
                sdr = self.root / (transfer + "-sdr.mp4")
                _run([
                    "ffmpeg", "-y", "-v", "error", "-i", str(hdr), "-vf",
                    hdr_to_bt709_filter(hdr_profile), "-c:v", "libx264", "-crf", "0",
                    "-pix_fmt", "yuv420p", *encoder_color_args(bt709_sdr_profile(), "h264"),
                    "-an", str(sdr),
                ])
                output_profile = probe_color_profile(sdr)
                self.assertFalse(output_profile["hdr"])
                self.assertEqual((output_profile["primaries"], output_profile["transfer"], output_profile["matrix"]),
                                 ("bt709", "bt709", "bt709"))
                mae = _mae(reference, _read_bgr(sdr, output_profile))
                self.assertLess(mae, 30.0)


if __name__ == "__main__":
    unittest.main()
