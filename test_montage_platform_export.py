import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app import (
    PREP_EXPORT_PROFILE,
    _mp4_copy_ready,
    _platform_copy_probe,
    _publish_prep_signature,
)
import app as app_module

if shutil.which("ffmpeg") is None:
    raise unittest.SkipTest("ffmpeg не найден — платформенные экспортные тесты пропущены")


def _make_video(path: Path, *, fps: int = 25, pix_fmt: str = "yuv420p",
                width: int = 720, height: int = 1280, silent: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={fps}:duration=4",
    ]
    if silent:
        command += ["-an", "-c:v", "libx264", "-pix_fmt", pix_fmt, str(path)]
    else:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                    "-c:v", "libx264", "-pix_fmt", pix_fmt,
                    "-c:a", "aac", "-ar", "48000", "-shortest", str(path)]
    subprocess.run(command, check=True, capture_output=True)
    return path


class InstagramFastPathTests(unittest.TestCase):
    def setUp(self):
        from montage.instagram_publisher import InstagramPublisher
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        publisher = InstagramPublisher.__new__(InstagramPublisher)
        publisher.ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        publisher.log_path = self.root / "log.txt"
        publisher.on_log = lambda message: None
        self.publisher = publisher

    def tearDown(self):
        self.tempdir.cleanup()

    def test_compatible_25fps_master_takes_copy_path(self):
        source = _make_video(self.root / "master.mp4")
        profile = self.publisher._stream_profile(source)
        self.assertTrue(self.publisher._copy_ready_for_ig(source, profile))
        self.assertEqual(self.publisher._ig_fps(source, profile), "25")

    def test_incompatible_pix_fmt_falls_back_to_encode(self):
        source = _make_video(self.root / "master444.mp4", pix_fmt="yuv444p")
        profile = self.publisher._stream_profile(source)
        self.assertFalse(self.publisher._copy_ready_for_ig(source, profile))

    def test_oversize_master_falls_back_to_encode(self):
        source = _make_video(self.root / "big.mp4")
        profile = self.publisher._stream_profile(source)
        # Simulate size overflow without generating a >100MB fixture.
        with mock.patch("montage.instagram_publisher.GITHUB_LIMIT_BYTES", 1):
            self.assertFalse(self.publisher._copy_ready_for_ig(source, profile))

    def test_extreme_fps_is_clamped_to_ig_range(self):
        source = _make_video(self.root / "hi90.mp4", fps=90)
        profile = self.publisher._stream_profile(source)
        self.assertFalse(self.publisher._copy_ready_for_ig(source, profile))
        self.assertEqual(self.publisher._ig_fps(source, profile), "60")

    def test_low_fps_is_clamped_to_ig_range(self):
        source = _make_video(self.root / "lo15.mp4", fps=15)
        profile = self.publisher._stream_profile(source)
        self.assertFalse(self.publisher._copy_ready_for_ig(source, profile))
        self.assertEqual(self.publisher._ig_fps(source, profile), "24")

    def test_remux_copy_output_is_compatible(self):
        source = _make_video(self.root / "master.mp4")
        out = self.root / "staged.mp4"
        self.publisher._remux_copy(source, out, lambda p, s: None)
        profile = self.publisher._stream_profile(out)
        self.assertTrue(self.publisher._copy_ready_for_ig(out, profile))
        self.assertEqual(
            self.publisher._stream_profile(source)["video"]["avg_frame_rate"],
            profile["video"]["avg_frame_rate"],
        )

    def test_high444_profile_is_not_copy_compatible(self):
        source = _make_video(self.root / "master.mp4")
        profile = self.publisher._stream_profile(source)
        profile["video"]["profile"] = "High 4:4:4 Predictive"
        self.assertFalse(self.publisher._copy_ready_for_ig(source, profile))

    def test_oversize_fallback_reencodes_from_master(self):
        from types import SimpleNamespace

        source = _make_video(self.root / "master.mp4")
        self.publisher.main = SimpleNamespace(validate_local_video=lambda _path: None)
        reencode_sources = []

        def normalize(_source, output, _progress):
            output.write_bytes(b"normalized-output-too-large")

        def transcode(master, output, _progress):
            reencode_sources.append(Path(master))
            output.write_bytes(b"size-limited-from-master")

        with mock.patch.object(self.publisher, "_copy_ready_for_ig", return_value=False), \
             mock.patch.object(self.publisher, "_normalize_for_publication", side_effect=normalize), \
             mock.patch.object(self.publisher, "_transcode_for_github", side_effect=transcode), \
             mock.patch("montage.instagram_publisher.GITHUB_LIMIT_BYTES", 1):
            output = self.publisher.prepare_variant(
                source, self.root / "staging", "job", "normal", lambda p, s: None,
            )
        self.assertEqual(reencode_sources, [source])
        self.assertTrue(output.is_file())

    def test_normalize_keeps_master_fps(self):
        source = _make_video(self.root / "keep25.mp4", fps=25, pix_fmt="yuv444p")
        out = self.root / "normalized25.mp4"
        self.publisher._normalize_for_publication(source, out, lambda p, s: None)
        profile = self.publisher._stream_profile(out)
        self.assertEqual(str(profile["video"].get("avg_frame_rate")), "25/1")


class YouTubeFastPathTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def make_publisher(self):
        from montage.youtube_publisher import YouTubePublisher
        return YouTubePublisher.__new__(YouTubePublisher)

    def test_youtube_accepts_hevc_master_for_copy(self):
        source = self.root / "hevc.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=25:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx265", "-pix_fmt", "yuv420p", "-tag:v", "hvc1",
            "-c:a", "aac", "-ar", "48000", "-shortest", str(source),
        ], check=True, capture_output=True)
        publisher = self.make_publisher()
        profile = publisher._stream_profile(source)
        self.assertTrue(publisher._copy_ready_for_youtube(profile))
        out = self.root / "staged_yt.mp4"
        publisher._remux_copy(source, out, lambda p, s: None)
        after = publisher._stream_profile(out)
        self.assertEqual(after["video"]["codec_name"], "hevc")

    def test_youtube_rejects_non_aac_audio_for_copy(self):
        source = self.root / "mp3.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=25:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "libmp3lame", "-shortest", str(source),
        ], check=True, capture_output=True)
        publisher = self.make_publisher()
        profile = publisher._stream_profile(source)
        self.assertFalse(publisher._copy_ready_for_youtube(profile))

    def test_youtube_rejects_high444_profile(self):
        source = self.root / "profile.mp4"
        _make_video(source)
        publisher = self.make_publisher()
        profile = publisher._stream_profile(source)
        profile["video"]["profile"] = "High 4:4:4 Predictive"
        self.assertFalse(publisher._copy_ready_for_youtube(profile))


class AppRemuxProbeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_tg_copy_ready_probe_accepts_compatible_mp4(self):
        source = _make_video(self.root / "tg.mp4")
        probe = _platform_copy_probe(source)
        self.assertTrue(_mp4_copy_ready(probe))

    def test_probe_rejects_mkv_container(self):
        source = self.root / "wrong.mkv"
        subprocess.run([
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=720x1280:rate=25:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
        ], check=True, capture_output=True)
        probe = _platform_copy_probe(source)
        self.assertFalse(_mp4_copy_ready(probe))

    def test_signature_bumps_with_export_profile(self):
        job = {"id": "sig"}
        source = self.root / "master.mp4"
        source.write_bytes(b"video-bytes")
        plain = _publish_prep_signature(job, "instagram:normal:video", source)
        old_profile = app_module.PREP_EXPORT_PROFILE
        try:
            app_module.PREP_EXPORT_PROFILE = "b3-test"
            changed = _publish_prep_signature(job, "instagram:normal:video", source)
        finally:
            app_module.PREP_EXPORT_PROFILE = old_profile
        self.assertNotEqual(PREP_EXPORT_PROFILE, "")
        self.assertNotEqual(plain, changed)


if __name__ == "__main__":
    unittest.main()
