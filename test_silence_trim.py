import math
import shutil
import struct
import subprocess
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

from montage.silence_trim import (
    analyze_pcm_chunks,
    build_concat_filter,
    effective_frame_intervals,
    normalize_cut_list,
    parse_frame_rate,
    retained_frame_segments,
)


def f32le(values):
    return struct.pack("<{}f".format(len(values)), *values)


class PcmAnalysisTests(unittest.TestCase):
    def test_known_signal_reports_rms_peak_and_silence_bins(self):
        signal = f32le([0.5] * 480 + [0.0] * 480)
        result = analyze_pcm_chunks((signal[:713], signal[713:]), channels=1)
        self.assertEqual(result["envelope_dbfs_x100"], [-602, -12000])
        self.assertAlmostEqual(result["mean_rms_dbfs"], -9.03, places=2)
        self.assertAlmostEqual(result["max_peak_dbfs"], -6.02, places=2)
        self.assertEqual(result["audio_duration_ms"], 20)

    def test_multichannel_is_measured_without_downmixing(self):
        values = []
        for _ in range(480):
            values.extend((0.5, 0.25))
        result = analyze_pcm_chunks((f32le(values),), channels=2)
        expected_rms = math.sqrt((0.5 ** 2 + 0.25 ** 2) / 2.0)
        self.assertAlmostEqual(result["mean_rms_dbfs"], 20 * math.log10(expected_rms), places=2)
        self.assertAlmostEqual(result["max_peak_dbfs"], 20 * math.log10(0.5), places=2)
        self.assertEqual(result["channels"], 2)

    def test_silence_has_explicit_floor_and_invalid_channels_fail(self):
        result = analyze_pcm_chunks((f32le([0.0] * 480),), channels=1)
        self.assertEqual(result["mean_rms_dbfs"], -120.0)
        self.assertEqual(result["max_peak_dbfs"], -120.0)
        with self.assertRaises(ValueError):
            analyze_pcm_chunks((), channels=9)


class CutListTests(unittest.TestCase):
    def test_overlaps_and_adjacent_ranges_union_with_provenance(self):
        cuts = [
            {"cut_id": "b", "start_ms": 1200, "end_ms": 1800, "origin": "manual"},
            {"cut_id": "a", "start_ms": 500, "end_ms": 1200, "origin": "auto"},
            {"cut_id": "c", "start_ms": 1000, "end_ms": 1400, "origin": "edited"},
        ]
        self.assertEqual(normalize_cut_list(cuts, 5000), [{
            "start_ms": 500,
            "end_ms": 1800,
            "cut_ids": ["a", "c", "b"],
            "origins": ["auto", "edited", "manual"],
        }])

    def test_malformed_edl_is_rejected(self):
        bad = [
            [{"cut_id": "x", "start_ms": True, "end_ms": 3, "origin": "manual"}],
            [{"cut_id": "x", "start_ms": 4, "end_ms": 3, "origin": "manual"}],
            [{"cut_id": "x", "start_ms": 0, "end_ms": 5001, "origin": "manual"}],
            [{"cut_id": "x", "start_ms": 0, "end_ms": 3, "origin": "unknown"}],
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_cut_list(value, 5000)

    def test_cfr_boundaries_use_ceil_and_preserve_half_open_semantics(self):
        fps = parse_frame_rate("30000/1001")
        raw = normalize_cut_list([
            {"cut_id": "cut", "start_ms": 1001, "end_ms": 2002, "origin": "auto"}
        ], 10_000)
        effective = effective_frame_intervals(raw, fps, 10_000)
        self.assertEqual(effective[0]["start_frame"], 30)
        self.assertEqual(effective[0]["end_frame"], 60)
        self.assertEqual(effective[0]["cut_ids"], ["cut"])

    def test_subframe_cut_can_become_ineffective(self):
        raw = normalize_cut_list([
            {"cut_id": "tiny", "start_ms": 1, "end_ms": 2, "origin": "manual"}
        ], 10_000)
        self.assertEqual(effective_frame_intervals(raw, Fraction(30, 1), 10_000), [])

    def test_full_duration_cut_is_rejected(self):
        effective = [{"start_frame": 0, "end_frame": 60}]
        with self.assertRaisesRegex(ValueError, "entire video"):
            retained_frame_segments(effective, 60)

    def test_concat_graph_interleaves_audio_video_segments(self):
        raw = normalize_cut_list([
            {"cut_id": "middle", "start_ms": 1000, "end_ms": 2000, "origin": "manual"}
        ], 3000)
        effective = effective_frame_intervals(raw, Fraction(30, 1), 3000)
        graph, segments = build_concat_filter(
            effective, fps=Fraction(30, 1), total_frames=90, has_audio=True,
            audio_start_offset_seconds=0.1,
        )
        self.assertEqual(segments, [(0, 30), (60, 90)])
        self.assertIn("concat=n=2:v=1:a=1[vout][aout]", graph)
        self.assertIn("[v0][a0][v1][a1]concat", graph)
        self.assertIn("start_sample=96000:end_sample=144000", graph)
        self.assertIn("PTS-STARTPTS+(0.100000000)/TB", graph)


class SilenceRenderBypassTests(unittest.TestCase):
    def test_disabled_trim_returns_original_master_without_ffmpeg(self):
        from montage import engine

        original = Path("/tmp/silence-bypass-master.mp4")
        output, metadata = engine._apply_silence_edl(
            original, Path("/tmp"), {"silence_trim_enabled": False},
            job_id="unused", duration=2.0, progress_cb=lambda *_: None,
            cancel_cb=lambda: False, log_cb=lambda *_: None,
        )
        self.assertEqual(output, original)
        self.assertFalse(metadata["applied"])

    def test_enabled_empty_edl_also_bypasses_cut_encode(self):
        from montage import engine

        original = Path("/tmp/silence-empty-edl-master.mp4")
        output, metadata = engine._apply_silence_edl(
            original,
            Path("/tmp"),
            {"silence_trim_enabled": True, "silence_source_duration_ms": 2000, "silence_cuts": []},
            job_id="unused-empty-edl", duration=2.0,
            progress_cb=lambda *_: None, cancel_cb=lambda: False, log_cb=lambda *_: None,
        )
        self.assertEqual(output, original)
        self.assertFalse(metadata["applied"])
        self.assertEqual(metadata["reason"], "empty_cut_list")

    def test_progress_stage_is_opt_in(self):
        from montage.progress_tracker import montage_plan

        legacy = montage_plan({"silence_trim_enabled": False})
        trimmed = montage_plan({"silence_trim_enabled": True, "silence_cuts": []})
        self.assertNotIn("silence_trim", [stage.key for stage in legacy])
        self.assertIn("silence_trim", [stage.key for stage in trimmed])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
class FfmpegCutIntegrationTests(unittest.TestCase):
    def test_session_target_fps_is_used_for_render_frame_snapping(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "cfr-source.mp4"
            subprocess.run([
                shutil.which("ffmpeg"), "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=c=red:s=64x64:r=30:d=2",
                "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=2",
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-t", "2", str(source),
            ], check=True, capture_output=True)
            from montage import engine
            output, metadata = engine._apply_silence_edl(
                source,
                Path(temp),
                {
                    "silence_trim_enabled": True,
                    "silence_source_duration_ms": 2000,
                    "silence_target_fps": "25/1",
                    "silence_cuts": [{"cut_id":"fractional","start_ms":500,"end_ms":1000,"origin":"manual"}],
                },
                job_id="silence-target-fps",
                duration=2.0,
                progress_cb=lambda *_: None,
                cancel_cb=lambda: False,
                log_cb=lambda *_: None,
            )
            self.assertTrue(metadata["applied"])
            self.assertEqual(metadata["master_fps"], "25/1")
            self.assertEqual(metadata["removed_duration_ms"], 480)
            self.assertEqual(metadata["output_duration_ms"], 1520)
            probe = subprocess.run([
                shutil.which("ffprobe"), "-v", "error", "-show_entries",
                "stream=codec_type,r_frame_rate,avg_frame_rate,time_base,duration,duration_ts,nb_frames", "-show_format", "-of", "json", str(output),
            ], check=True, capture_output=True, text=True)
            import json
            data = json.loads(probe.stdout)
            video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
            audio = next(stream for stream in data["streams"] if stream["codec_type"] == "audio")
            self.assertEqual(video["r_frame_rate"], "25/1")
            self.assertEqual(int(video["nb_frames"]), 38)
            self.assertAlmostEqual(int(video["nb_frames"]) / 25.0, metadata["output_duration_ms"] / 1000.0, delta=1 / 25)
            self.assertLess(abs(int(video["nb_frames"]) / 25.0 - float(audio["duration"])), 1 / 48000 + 0.002)

    def test_cut_ending_at_analyzed_duration_clips_to_normalized_master_frames(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.mp4"
            subprocess.run([
                shutil.which("ffmpeg"), "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=30:d=2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-t", "2", str(source),
            ], check=True, capture_output=True)
            from montage import engine
            output, metadata = engine._apply_silence_edl(
                source,
                Path(temp),
                {
                    "silence_trim_enabled": True,
                    "silence_source_duration_ms": 2001,
                    "silence_cuts": [{
                        "cut_id": "tail", "start_ms": 1500, "end_ms": 2001, "origin": "auto",
                    }],
                },
                job_id="silence-tail-boundary",
                duration=2.001,
                progress_cb=lambda *_: None,
                cancel_cb=lambda: False,
                log_cb=lambda *_: None,
            )
            self.assertTrue(metadata["applied"])
            self.assertEqual(metadata["removed_duration_ms"], 500)
            self.assertEqual(metadata["output_duration_ms"], 1500)

    def test_vfr_master_is_normalized_before_cut_snap(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "vfr-source.mp4"
            workdir = Path(temp)
            subprocess.run([
                shutil.which("ffmpeg"), "-v", "error", "-y",
                "-f", "lavfi", "-i", "testsrc2=size=64x64:rate=30:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                "-vf", "select='not(eq(mod(n,3),1))'", "-fps_mode", "vfr",
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-t", "2", str(source),
            ], check=True, capture_output=True)
            from montage import engine
            output, metadata = engine._apply_silence_edl(
                source,
                workdir,
                {
                    "silence_trim_enabled": True,
                    "silence_source_duration_ms": 2000,
                    "silence_cuts": [{"cut_id":"vfr-cut", "start_ms":500, "end_ms":1000, "origin":"manual"}],
                },
                job_id="silence-vfr",
                duration=2.0,
                progress_cb=lambda *_: None,
                cancel_cb=lambda: False,
                log_cb=lambda *_: None,
            )
            self.assertTrue(metadata["applied"])
            self.assertRegex(metadata["master_fps"], r"\d+/\d+")
            output_probe = subprocess.run([
                shutil.which("ffprobe"), "-v", "error", "-show_streams", "-show_format",
                "-of", "json", str(output),
            ], check=True, capture_output=True, text=True)
            import json
            result = json.loads(output_probe.stdout)
            streams = result["streams"]
            self.assertEqual({stream["codec_type"] for stream in streams}, {"video", "audio"})
            video = next(stream for stream in streams if stream["codec_type"] == "video")
            audio = next(stream for stream in streams if stream["codec_type"] == "audio")
            video_frame_duration = int(video["nb_frames"]) / float(Fraction(metadata["master_fps"]))
            self.assertLess(
                abs(video_frame_duration - float(audio["duration"])), 1 / 48000 + 0.002,
                repr({"video":video,"audio":audio,"fps":metadata["master_fps"],"meta":metadata}),
            )
            self.assertAlmostEqual(float(result["format"]["duration"]), metadata["output_duration_ms"] / 1000.0, delta=0.08)

    def test_video_only_manual_cut_is_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "video-only.mp4"
            subprocess.run([
                shutil.which("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                "color=c=blue:s=64x64:r=30:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-t", "2", str(source),
            ], check=True, capture_output=True)
            from montage import engine
            output, metadata = engine._apply_silence_edl(
                source,
                Path(temp),
                {
                    "silence_trim_enabled": True,
                    "silence_source_duration_ms": 2000,
                    "silence_cuts": [{
                        "cut_id": "manual-video-only", "start_ms": 500, "end_ms": 1000,
                        "origin": "manual",
                    }],
                },
                job_id="silence-video-only",
                duration=2.0,
                progress_cb=lambda *_: None,
                cancel_cb=lambda: False,
                log_cb=lambda *_: None,
            )
            self.assertTrue(metadata["applied"])
            result = subprocess.run([
                shutil.which("ffprobe"), "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                "-of", "json", str(output),
            ], check=True, capture_output=True, text=True)
            import json
            data = json.loads(result.stdout)
            self.assertEqual([s["codec_type"] for s in data["streams"]], ["video"])
            self.assertAlmostEqual(float(data["format"]["duration"]), 1.5, delta=0.04)

    def test_one_pass_cut_keeps_video_audio_durations_aligned(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.mp4"
            workdir = Path(temp)
            subprocess.run([
                shutil.which("ffmpeg"), "-v", "error", "-y",
                "-f", "lavfi", "-i", "color=c=red:s=64x64:r=30:d=2",
                "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=2",
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-t", "2", str(source),
            ], check=True, capture_output=True)
            from montage import engine
            output, metadata = engine._apply_silence_edl(
                source,
                workdir,
                {
                    "silence_trim_enabled": True,
                    "silence_source_duration_ms": 2000,
                    "silence_cuts": [{
                        "cut_id": "middle", "start_ms": 500, "end_ms": 1000, "origin": "manual"
                    }],
                },
                job_id="silence-fixture",
                duration=2.0,
                progress_cb=lambda *_: None,
                cancel_cb=lambda: False,
                log_cb=lambda *_: None,
            )
            self.assertTrue(metadata["applied"])
            self.assertEqual(metadata["removed_duration_ms"], 500)
            probe = subprocess.run([
                shutil.which("ffprobe"), "-v", "error", "-show_streams", "-show_format",
                "-of", "json", str(output),
            ], check=True, capture_output=True, text=True)
            frame_details = subprocess.run([
                shutil.which("ffprobe"), "-v", "error", "-count_frames", "-show_entries",
                "stream=codec_type,nb_read_frames,duration", "-of", "json", str(output),
            ], check=True, capture_output=True, text=True)
            import json
            data = json.loads(probe.stdout)
            audio_duration = float(next(s["duration"] for s in data["streams"] if s.get("codec_type") == "audio"))
            video_frames = int(next(
                s["nb_read_frames"] for s in json.loads(frame_details.stdout)["streams"]
                if s.get("codec_type") == "video"
            ))
            video_duration = video_frames / 30.0
            durations = [video_duration, audio_duration]
            self.assertEqual(len(durations), 2)
            diagnostic = repr({"durations": durations, "output_streams": frame_details.stdout,
                               "metadata": metadata})
            self.assertLess(abs(durations[0] - durations[1]), 1 / 48000 + 0.002, diagnostic)
            self.assertAlmostEqual(float(data["format"]["duration"]), 1.5, delta=0.04, msg=diagnostic)


if __name__ == "__main__":
    unittest.main()
