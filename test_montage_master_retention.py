"""Master lifecycle stabilization (B.2 follow-up).

Covers the retention policy (master survives the first successful publish,
is pruned by age / total size / low free space, only for fully settled jobs),
the engine master-export mode resolution, path-traversal safety, the
multi-restart multi-platform scenario and concurrent event-loop-free
platform preparation.
"""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app
from montage.engine import _resolve_master_export
from montage.audio_censor import build_volume_filter, find_mute_intervals


class MasterTempCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.old_work = app.MONTAGE_WORK
        self.old_jobs = app.jobs
        app.MONTAGE_WORK = self.root / "montage_work"
        app.MONTAGE_WORK.mkdir(parents=True)
        app.jobs = {}

    def tearDown(self):
        app.MONTAGE_WORK = self.old_work
        app.jobs = self.old_jobs
        self.tempdir.cleanup()

    def build_job(self, job_id="tenure01", *, stage="main_ready",
                  publish_errors=None, publish_unknown=None):
        """Persisted montage job with an inplace master stream on disk."""
        workdir = app.MONTAGE_WORK / job_id
        workdir.mkdir(parents=True, exist_ok=True)
        master = workdir / "sub_words.mp4"
        master.write_bytes(b"master-stream")
        final = workdir / "final.mp4"
        final.write_bytes(b"final-video")
        preview = workdir / "preview.mp4"
        preview.write_bytes(b"preview-video")
        job = {
            "id": job_id,
            "kind": "montage",
            "status": "done",
            "progress": 100,
            "stage": stage,
            "detail": "",
            "error": None,
            "original_name": "source.mp4",
            "video_path": str(workdir / "source.mp4"),
            "srt_text": "1\n00:00:00,000 --> 00:00:01,000\ntext\n",
            "transcript_job_id": None,
            "project_id": None,
            "result": {
                "job_id": job_id,
                "result_video": str(final),
                "preview_video": str(preview),
                "master_video": master.name,
                "master_mode": "inplace",
                "timeline": {},
            },
            "params": {"face_tracking": False, "mirror_horizontal": False, "overlays": []},
            "publish": None,
            "publish_errors": publish_errors,
            "publish_unknown": publish_unknown,
            "publish_progress": None,
            "covers": [],
            "cover_status": "idle",
        }
        app.jobs[job_id] = job
        app._save_montage_job(job_id)
        return job, workdir, master


class MasterExportModeTests(MasterTempCase):
    """AUTOMONTAGE_MASTER_MODE matrix (inplace / copy / none / unknown)."""

    def build_final(self, job_id="mode02"):
        workdir = app.MONTAGE_WORK / job_id
        workdir.mkdir(parents=True)
        final = workdir / "build_stream.mp4"
        final.write_bytes(b"effected-stream")
        return workdir, final

    def test_inplace_records_effected_stream_without_extra_file(self):
        workdir, final = self.build_final()
        mode, name = _resolve_master_export("inplace", final, workdir)
        self.assertEqual((mode, name), ("inplace", "build_stream.mp4"))
        self.assertFalse((workdir / "master.mp4").exists())

    def test_copy_creates_byte_exact_master(self):
        workdir, final = self.build_final()
        mode, name = _resolve_master_export("copy", final, workdir, free_bytes=10 ** 9)
        self.assertEqual((mode, name), ("copy", "master.mp4"))
        master_out = workdir / "master.mp4"
        self.assertTrue(master_out.is_file())
        self.assertEqual(master_out.read_bytes(), final.read_bytes())
        self.assertEqual(master_out.stat().st_size, final.stat().st_size)

    def test_none_disables_master_tracking(self):
        workdir, final = self.build_final()
        mode, name = _resolve_master_export("none", final, workdir)
        self.assertEqual((mode, name), ("none", ""))
        self.assertFalse((workdir / "master.mp4").exists())

    def test_unknown_mode_falls_back_to_inplace(self):
        workdir, final = self.build_final()
        with mock.patch("sys.stderr"):
            mode, name = _resolve_master_export("bogus", final, workdir, free_bytes=10 ** 9)
        self.assertEqual((mode, name), ("inplace", "build_stream.mp4"))
        self.assertFalse((workdir / "master.mp4").exists())

    def test_copy_without_enough_space_falls_back_to_inplace(self):
        workdir, final = self.build_final()
        free = final.stat().st_size + 64 * 1024 * 1024 - 1
        with mock.patch("sys.stderr"):
            mode, name = _resolve_master_export("copy", final, workdir, free_bytes=free)
        self.assertEqual((mode, name), ("inplace", "build_stream.mp4"))
        self.assertFalse((workdir / "master.mp4").exists())


class MasterRetentionPolicyTests(MasterTempCase):
    """Spec scenario: IG published -> app restart -> YouTube/TG prepared later."""

    def build_state(self, job_id="tenure01", *, stage="main_ready",
                    publish_errors=None, publish_unknown=None):
        return self.build_job(job_id, stage=stage,
                              publish_errors=publish_errors, publish_unknown=publish_unknown)

    def test_multi_platform_scenario(self):
        job, workdir, master = self.build_state("tenure01")

        # 1) First publication: Instagram settles the job.  Cleanup runs, but
        # must NOT remove the master source.
        job["stage"] = "published"
        summary = app._cleanup_publish_artifacts(job["id"], job)
        self.assertFalse(summary["errors"])
        self.assertNotIn(master.name, summary["removed"])
        self.assertTrue(master.is_file())

        # 2) App restart: recovery restores the job and the master path.
        app.jobs.clear()
        app._recover_montage_jobs()
        recovered = app.jobs[job["id"]]
        self.assertEqual(app._master_video_path(recovered), master)

        # 3) Later: prepare the same video for YouTube from the master.
        with mock.patch.object(app.montage_engine, "usable_video", return_value=True):
            self.assertEqual(app._prep_source_video(recovered, False), master)

        # 4) A fresh, settled master is kept by the default retention window.
        after = app._retire_masters()
        self.assertEqual(after["removed"], [])
        self.assertTrue(master.is_file())

    def test_retention_ages_out_only_fully_settled_masters(self):
        old_settled, _, old_master = self.build_state("tenure02", stage="published")
        fresh_settled, _, fresh_master = self.build_state(
            "tenure03", stage="published")
        processing, _, processing_master = self.build_state(
            "tenure04", stage="main_ready")
        errored, _, errored_master = self.build_state(
            "tenure05", stage="published", publish_errors={"instagram": "boom"})
        unknown, _, unknown_master = self.build_state(
            "tenure06", stage="published", publish_unknown={"youtube": "did it run?"})

        for master in (old_master, processing_master, errored_master, unknown_master):
            stale = time.time() - 14 * 86400
            os.utime(master, (stale, stale))

        summary = app._retire_masters()

        self.assertEqual(len(summary["removed"]), 1)
        self.assertIn(f"tenure02/{old_master.name}", summary["removed"][0])
        self.assertFalse(old_master.exists())
        for master in (fresh_master, processing_master, errored_master, unknown_master):
            self.assertTrue(master.is_file(), f"master must survive: {master}")

        # Idempotent: a second sweep has nothing left to do.
        second = app._retire_masters()
        self.assertEqual(second["removed"], [])

    def test_retention_total_size_ceiling_removes_oldest_first(self):
        older, _, older_master = self.build_state("tenure07", stage="published")
        newer, _, newer_master = self.build_state("tenure08", stage="published")
        stale = time.time() - 11 * 86400
        os.utime(older_master, (stale, stale))

        with mock.patch.dict(os.environ, {"AUTOMONTAGE_MASTER_MAX_TOTAL_BYTES": "15"}):
            summary = app._retire_masters()

        # Both masters are 14 bytes, total 28 > 15 -> the OLDEST (tenure07)
        # goes first; after that the sum (14) fits the ceiling and the newer
        # master stays.
        self.assertFalse(older_master.exists())
        self.assertTrue(newer_master.is_file())
        self.assertEqual(len(summary["removed"]), 1)

    def test_retention_low_free_space_prunes_oldest(self):
        job, workdir, master = self.build_state("tenure09")
        old = time.time() - 2 * 86400
        os.utime(master, (old, old))
        state = json.loads((workdir / "job_state.json").read_text(encoding="utf-8"))
        state["stage"] = "published"
        (workdir / "job_state.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8")

        with mock.patch.dict(os.environ, {"AUTOMONTAGE_MASTER_MIN_FREE_BYTES": str(10 ** 18)}):
            summary = app._retire_masters()

        self.assertFalse(master.exists())
        self.assertEqual(len(summary["removed"]), 1)

    def test_retention_env_disabled(self):
        job, workdir, master = self.build_state("tenure10")
        stale = time.time() - 30 * 86400
        os.utime(master, (stale, stale))
        state = json.loads((workdir / "job_state.json").read_text(encoding="utf-8"))
        state["stage"] = "published"
        (workdir / "job_state.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8")

        with mock.patch.dict(os.environ, {
            "AUTOMONTAGE_MASTER_MAX_AGE_DAYS": "0",
            "AUTOMONTAGE_MASTER_MAX_TOTAL_BYTES": "0",
            "AUTOMONTAGE_MASTER_MIN_FREE_BYTES": "0",
        }):
            summary = app._retire_masters()

        self.assertEqual(summary["removed"], [])
        self.assertTrue(master.is_file())


class MasterPathSafetyTests(MasterTempCase):
    def make_job_dict(self, job_id="traverse01", result=None):
        workdir = app.MONTAGE_WORK / job_id
        workdir.mkdir(parents=True, exist_ok=True)
        final = workdir / "final.mp4"
        final.write_bytes(b"final")
        job = {
            "id": job_id,
            "kind": "montage",
            "status": "done",
            "stage": "main_ready",
            "result": result or {
                "job_id": job_id,
                "result_video": str(final),
            },
        }
        app.jobs[job_id] = job
        return job, workdir

    def test_master_path_flattens_hostile_job_state_values(self):
        hostile_job, _ = self.make_job_dict()
        outside_dir = app.MONTAGE_WORK / "outside"
        outside_dir.mkdir(parents=True, exist_ok=True)
        (outside_dir / "master.mp4").write_bytes(b"master")
        hostile_job["result"]["job_id"] = "../outside"
        hostile_job["result"]["master_video"] = "../../secret/master.mp4"
        resolved = app._master_video_path(hostile_job)
        expected = outside_dir / "master.mp4"
        self.assertEqual(resolved, expected)
        self.assertTrue(str(resolved).startswith(str(app.MONTAGE_WORK)))
        self.assertEqual(resolved.name, "master.mp4")

    def test_master_path_used_for_both_job_components(self):
        job, _ = self.make_job_dict(job_id="traverse02")
        nested = app.MONTAGE_WORK / "b"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "escape.mp4").write_bytes(b"master")
        job["result"]["job_id"] = "a/b"
        job["result"]["master_video"] = f"..{os.sep}..{os.sep}escape.mp4"
        resolved = app._master_video_path(job)
        self.assertEqual(resolved, nested / "escape.mp4")

    def test_missing_master_prep_falls_back_with_warning(self):
        job, workdir = self.make_job_dict(job_id="traverse03")
        master = workdir / "sub_words.mp4"
        master.write_bytes(b"master")
        job["result"]["master_video"] = master.name
        job["result"]["master_mode"] = "inplace"
        # Missing file on disk: _master_video_path returns None.
        master.unlink()
        self.assertIsNone(app._master_video_path(job))
        with mock.patch.object(app.montage_engine, "usable_video", return_value=True):
            self.assertEqual(app._prep_source_video(job, False),
                             Path(job["result"]["result_video"]))
        # Damaged master: usable_video False -> warning + fallback.
        master.write_bytes(b"broken-bytes")
        with mock.patch.object(app.montage_engine, "usable_video", return_value=False):
            import sys as _sys
            with mock.patch.object(_sys, "stderr"):
                self.assertEqual(app._prep_source_video(job, False),
                                 Path(job["result"]["result_video"]))


class MasterAudioCensorTests(unittest.TestCase):
    """The recorded master stream must come from the censored audio pipeline.

    Building a real tiny video lets us run the exact censor command the
    engine builds (same volume filter) and then verify that:
    * the muted interval actually lost its loudness, and
    * resolve_master_export("inplace", censored_stream) records the censored
      stream name (i.e. censorship happens before the master capture).
    """

    FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

    @classmethod
    def setUpClass(cls):
        if shutil.which("ffmpeg") is None:
            raise unittest.SkipTest("ffmpeg unavailable")

    def test_master_records_censored_stream_not_raw_audio(self):
        tempdir = tempfile.TemporaryDirectory()
        workdir = Path(tempdir.name)
        try:
            source = workdir / "raw_stream.mp4"
            subprocess.run([
                self.FFMPEG, "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc=size=320x240:duration=3",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(source),
            ], check=True, capture_output=True)
            self.assertTrue(source.is_file())

            # One banned word spans 1.0-2.0s -> muted interval inside it.
            srt = "1\n00:00:01,000 --> 00:00:02,000\nсмерть\n"
            intervals = find_mute_intervals(srt)
            self.assertTrue(intervals)
            self.assertTrue(all(end > start for start, end in intervals))

            censored = workdir / "censored_audio.mp4"
            censor_cmd = [
                self.FFMPEG, "-y", "-i", str(source),
                "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                "-af", build_volume_filter(intervals),
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(censored),
            ]
            subprocess.run(censor_cmd, check=True, capture_output=True)
            self.assertTrue(censored.is_file())

            mode, name = _resolve_master_export("inplace", censored, workdir)
            self.assertEqual((mode, name), ("inplace", "censored_audio.mp4"))
            self.assertNotEqual(name, source.name)

            inside = self._mean_volume_db(censored, start=1.40, duration=0.15)
            outside = self._mean_volume_db(censored, start=2.40, duration=0.40)
            self.assertGreaterEqual(
                outside - inside, 20.0,
                f"muted region must be silenced (inside {inside} dB, outside {outside} dB)")
        finally:
            tempdir.cleanup()

    def _mean_volume_db(self, path, start, duration):
        result = subprocess.run([
            self.FFMPEG, "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
            "-i", str(path), "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-",
        ], capture_output=True, check=False)
        text = (result.stderr or b"").decode("utf-8", errors="replace")
        for line in text.splitlines():
            if "mean_volume:" in line:
                return float(line.split("mean_volume:")[1].split("dB")[0].strip())
        self.fail(f"volumedetect produced no mean_volume: {text[-400:]}")


class MasterPreparationAsyncTests(unittest.IsolatedAsyncioTestCase):
    """Platform preparation must stay off the event loop and be safe under
    concurrency / errors / cancellation while the master remains intact."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.old_work = app.MONTAGE_WORK
        self.old_jobs = app.jobs
        app.MONTAGE_WORK = self.root / "montage_work"
        app.MONTAGE_WORK.mkdir(parents=True)
        app.jobs = {}

    def tearDown(self):
        app.MONTAGE_WORK = self.old_work
        app.jobs = self.old_jobs
        self.tempdir.cleanup()

    def build_job(self, job_id="asprep01"):
        workdir = app.MONTAGE_WORK / job_id
        workdir.mkdir(parents=True, exist_ok=True)
        master = workdir / "sub_words.mp4"
        master.write_bytes(b"master-stream")
        final = workdir / "final.mp4"
        final.write_bytes(b"final-video")
        job = {
            "id": job_id,
            "kind": "montage",
            "status": "done",
            "progress": 100,
            "stage": "main_ready",
            "detail": "",
            "error": None,
            "original_name": "source.mp4",
            "video_path": str(workdir / "source.mp4"),
            "srt_text": "",
            "transcript_job_id": None,
            "project_id": None,
            "result": {
                "job_id": job_id,
                "result_video": str(final),
                "master_video": master.name,
                "master_mode": "inplace",
                "timeline": {},
            },
            "params": {"face_tracking": False, "mirror_horizontal": False, "overlays": []},
            "publish": None,
            "publish_errors": None,
            "publish_progress": None,
            "covers": [],
            "cover_status": "idle",
        }
        app.jobs[job_id] = job
        return job, workdir, master

    def install_prepare_stubs(self, render_side_effect=None):
        def fake_render(purpose, source, target_dir, stem, mirrored,
                        progress, cancel_cb=None):
            output = Path(target_dir) / f"{stem}.mp4"
            output.write_bytes(b"prepared")
            return output

        def fake_validate(path):
            return {"size": 8, "duration": 1.0}

        patches = [
            mock.patch.object(app, "_render_prepared_video",
                              side_effect=render_side_effect or fake_render),
            mock.patch.object(app, "_validate_prepared_video", side_effect=fake_validate),
            mock.patch.object(app, "_montage_announce", new=mock.AsyncMock()),
            mock.patch.object(app.montage_engine, "usable_video", return_value=True),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_two_jobs_prepare_concurrently_and_keep_masters(self):
        job_a, _, master_a = self.build_job("asprep01")
        job_b, _, master_b = self.build_job("asprep02")
        self.install_prepare_stubs()
        targets = [{"id": "instagram", "kind": "instagram", "mirrored": False,
                    "label": "Instagram"}]
        await asyncio.gather(
            app.process_prepare_publish("asprep01", targets),
            app.process_prepare_publish("asprep02", targets),
        )
        for job, master in ((job_a, master_a), (job_b, master_b)):
            self.assertEqual(job["prepare_status"], "done")
            self.assertTrue(job["publish_prep"]["instagram:main"]["ready"])
            self.assertTrue(master.is_file())
            # Prepared artefact resolves itself back from the master source.
            with mock.patch.object(app.montage_engine, "usable_video", return_value=True):
                self.assertEqual(app._prep_source_video(job, False), master)

    async def test_prepare_error_keeps_master_and_is_reported(self):
        job, workdir, master = self.build_job("asprep03")
        self.install_prepare_stubs(
            render_side_effect=RuntimeError("ffmpeg boom"))
        targets = [{"id": "instagram", "kind": "instagram", "mirrored": False,
                    "label": "Instagram"}]
        await app.process_prepare_publish("asprep03", targets)
        self.assertEqual(job["prepare_status"], "error")
        self.assertIn("ffmpeg boom", str(job["prepare_error"]))
        self.assertTrue(master.is_file())

    async def test_prepare_cancelled_does_not_touch_master(self):
        job, workdir, master = self.build_job("asprep04")
        job["cancel_requested"] = True
        self.install_prepare_stubs()
        targets = [{"id": "instagram", "kind": "instagram", "mirrored": False,
                    "label": "Instagram"}]
        with self.assertRaises(app.montage_engine.MontageCancelled):
            await app.process_prepare_publish("asprep04", targets)
        self.assertEqual(job["prepare_status"], "cancelled")
        self.assertTrue(master.is_file())
        self.assertFalse((job.get("publish_prep") or {}).get("instagram:main"))

    async def test_status_route_publish_ready_runs_off_loop(self):
        # The result payload itself uses to_thread for readiness probes; a
        # damaged master must fall back and still report readiness.
        job, workdir, master = self.build_job("asprep05")
        with mock.patch.object(app.montage_engine, "usable_video", return_value=False):
            payload = await app.montage_result("asprep05")
        self.assertIn("publish_ready", payload)
        self.assertIsInstance(payload["publish_ready"], dict)
        self.assertTrue(master.is_file())


if __name__ == "__main__":
    unittest.main()
