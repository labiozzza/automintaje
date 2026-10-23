"""TOCTOU guard for in-flight master usage (QA F9 fix, B.2 follow-up).

The retention sweep must not delete a master an active platform preparation
resolves or reads (FFmpeg-open window). Registration/release and the sweep's
removal decision run under the same lock, so they are linearized; the claim
covers success, error and cancellation exits.
"""

import asyncio
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import app


class PrepMasterGuardCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.old_work = app.MONTAGE_WORK
        self.old_jobs = app.jobs
        app.MONTAGE_WORK = self.root / "montage_work"
        app.MONTAGE_WORK.mkdir(parents=True)
        app.jobs = {}
        app._PREP_ACTIVE_MASTER_JOBS.clear()

    def tearDown(self):
        app.MONTAGE_WORK = self.old_work
        app.jobs = self.old_jobs
        app._PREP_ACTIVE_MASTER_JOBS.clear()
        self.tempdir.cleanup()

    def build_settled_job(self, job_id="toct01"):
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
            "stage": "published",
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
                "preview_video": str(workdir / "preview.mp4"),
                "master_video": master.name,
                "master_mode": "inplace",
                "timeline": {},
            },
            "params": {"face_tracking": False, "mirror_horizontal": False, "overlays": []},
            "publish": {"instagram": {"ok": True}},
            "publish_errors": None,
            "publish_unknown": None,
            "publish_progress": None,
            "covers": [],
            "cover_status": "idle",
        }
        app.jobs[job_id] = job
        app._save_montage_job(job_id)
        return job, workdir, master

    def age_master(self, master):
        stale = time.time() - 2 * 86400
        os.utime(master, (stale, stale))

    def aggressive_env(self):
        return mock.patch.dict(os.environ, {
            "AUTOMONTAGE_MASTER_MAX_AGE_DAYS": "0.00001",   # ~0.9 s TTL
            "AUTOMONTAGE_MASTER_MAX_TOTAL_BYTES": "64",     # far under payload
            "AUTOMONTAGE_MASTER_MIN_FREE_BYTES": str(10 ** 18),
        })

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

    def run_sweep_in_thread(self):
        summary: dict = {}
        def strike():
            with self.aggressive_env():
                summary["retention"] = app._retire_masters()
        thread = threading.Thread(target=strike)
        thread.start()
        self.addCleanup(thread.join)
        return summary, thread


class RetentionVsActivePrepTests(PrepMasterGuardCase):
    """Sweep vs preparation concurrency with artificially aggressive limits."""

    async def test_sweep_striking_inside_ffmpeg_window_keeps_master(self):
        job, workdir, master = self.build_settled_job("toct01")
        self.age_master(master)
        summary = {}
        window_seen = {}
        events = threading.Event()

        def render(purpose, source, target_dir, stem, mirrored,
                   progress, cancel_cb=None):
            # The resolved source must be the master (no silent fallback to
            # result_video) exactly at the FFmpeg-open point.
            window_seen["source"] = Path(source)
            output = Path(target_dir) / f"{stem}.mp4"
            with self.aggressive_env():
                # A retention strike lands in the middle of the FFmpeg-open
                # window: same-thread linearization proof for the registry.
                summary["mid_window"] = app._retire_masters()
            events.set()
            output.write_bytes(b"prepared")
            return output

        self.install_prepare_stubs(render_side_effect=render)
        targets = [{"id": "instagram", "kind": "instagram", "mirrored": False,
                    "label": "Instagram"}]
        await app.process_prepare_publish("toct01", targets)
        self.assertTrue(events.is_set())
        self.assertEqual(window_seen["source"], master, "master must stay the resolved source")
        mid = summary["mid_window"]
        self.assertEqual(mid["removed"], [])
        self.assertIn(f"toct01/{master.name} (prep in flight)", mid["kept"])
        self.assertTrue(master.is_file())
        self.assertEqual(job["prepare_status"], "done")
        self.assertTrue(job["publish_prep"]["instagram:main"]["ready"])

    async def test_release_after_success_enables_policy_removal(self):
        self.build_settled_job("toct07")
        self.age_master(app.MONTAGE_WORK / "toct07" / "sub_words.mp4")
        master = app.MONTAGE_WORK / "toct07" / "sub_words.mp4"
        self.install_prepare_stubs()
        targets = [{"id": "instagram", "kind": "instagram", "mirrored": False,
                    "label": "Instagram"}]
        await app.process_prepare_publish("toct07", targets)
        self.assertNotIn("toct07", app._PREP_ACTIVE_MASTER_JOBS,
                         "protection must be released after success")
        with self.aggressive_env():
            summary = app._retire_masters()
        self.assertFalse(master.is_file(), "after the prep ends policy own the master again")
        self.assertTrue(any("toct07" in item for item in summary["removed"]))

    async def test_registry_claim_is_synchronized_no_torn_state(self):
        job, workdir, master = self.build_settled_job("toct09")
        self.age_master(master)
        # Claim held from a worker thread, sweep from the main thread: the
        # locked snapshot inside _retire_masters must see the claim.
        await asyncio.to_thread(app._register_prep_master, "toct09")
        with self.aggressive_env():
            summary = app._retire_masters()
        self.assertTrue(master.is_file())
        self.assertIn(f"toct09/{master.name} (prep in flight)", summary["kept"])
        self.assertEqual(summary["removed"], [])


class ReleaseOnFailureTests(PrepMasterGuardCase):
    """Error and cancellation paths must release the claim (finally)."""

    async def test_error_releases_claim_and_policy_can_remove(self):
        self.build_settled_job("toct04")
        self.age_master(app.MONTAGE_WORK / "toct04" / "sub_words.mp4")
        master = app.MONTAGE_WORK / "toct04" / "sub_words.mp4"
        self.install_prepare_stubs(render_side_effect=RuntimeError("ffmpeg boom"))
        targets = [{"id": "instagram", "kind": "instagram", "mirrored": False,
                    "label": "Instagram"}]
        await app.process_prepare_publish("toct04", targets)
        self.assertEqual(app.jobs["toct04"]["prepare_status"], "error")
        self.assertNotIn("toct04", app._PREP_ACTIVE_MASTER_JOBS)
        with self.aggressive_env():
            app._retire_masters()
        self.assertFalse(master.is_file(), "protection released on error path")

    async def test_cancellation_releases_claim_and_policy_can_remove(self):
        self.build_settled_job("toct05")
        self.age_master(app.MONTAGE_WORK / "toct05" / "sub_words.mp4")
        master = app.MONTAGE_WORK / "toct05" / "sub_words.mp4"
        app.jobs["toct05"]["cancel_requested"] = True
        self.install_prepare_stubs()
        targets = [{"id": "instagram", "kind": "instagram", "mirrored": False,
                    "label": "Instagram"}]
        with self.assertRaises(app.montage_engine.MontageCancelled):
            await app.process_prepare_publish("toct05", targets)
        self.assertEqual(app.jobs["toct05"]["prepare_status"], "cancelled")
        self.assertNotIn("toct05", app._PREP_ACTIVE_MASTER_JOBS)
        with self.aggressive_env():
            app._retire_masters()
        self.assertFalse(master.is_file(), "protection released on cancel path")

    async def test_no_needs_early_return_releases_claim(self):
        self.build_settled_job("toct06")
        self.install_prepare_stubs()
        await app.process_prepare_publish("toct06", [])
        self.assertNotIn("toct06", app._PREP_ACTIVE_MASTER_JOBS)


class LockOffsetSanityTests(unittest.TestCase):
    """Basic synchronization invariants of the registration/release helpers."""

    def test_release_is_idempotent(self):
        app._register_prep_master("ls01")
        app._release_prep_master("ls01")
        app._release_prep_master("ls01")
        self.assertNotIn("ls01", app._PREP_ACTIVE_MASTER_JOBS)

    def test_register_blocks_until_running_sweep_finishes(self):
        # Simulate a sweep in progress: it holds the lock. Registration must
        # not add the claim *while* the sweep decides (linearization), so it
        # blocks; after the sweep releases, the claim lands.
        def slow_sweep():
            with app._MASTER_RETENTION_LOCK:
                time.sleep(0.3)  # pretend a full retention pass runs
                claimed = app._PREP_ACTIVE_MASTER_JOBS.copy()
            return claimed
        thread = threading.Thread(target=lambda: app._register_prep_master("ls02"))
        result: dict = {}
        def sweep():
            result["seen"] = slow_sweep()
        sweep_thread = threading.Thread(target=sweep)
        sweep_thread.start()
        time.sleep(0.1)  # make sure sweep holds the lock
        thread.start()
        thread.join()
        sweep_thread.join()
        try:
            self.assertNotIn("ls02", result["seen"],
                             "claim must not be visible to a sweep that already started")
            self.assertIn("ls02", app._PREP_ACTIVE_MASTER_JOBS)
        finally:
            app._PREP_ACTIVE_MASTER_JOBS.clear()


if __name__ == "__main__":
    unittest.main()
