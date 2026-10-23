import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app


class MontageMasterRestartTests(unittest.TestCase):
    """B.2 restart scenario: render finished, app restarted, later platform
    preparation still resolves the recorded master (spec section B.2)."""

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

    def build_state(self, job_id="restart01"):
        workdir = app.MONTAGE_WORK / job_id
        workdir.mkdir()
        master = workdir / "sub_words.mp4"
        master.write_bytes(b"master-stream")
        final = workdir / "final.mp4"
        preview = workdir / "preview.mp4"
        final.write_bytes(b"final-video")
        preview.write_bytes(b"preview-video")
        state = {
            "id": job_id,
            "kind": "montage",
            "status": "done",
            "progress": 100,
            "stage": "main_ready",
            "detail": "Основной ролик готов",
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
            "publish_errors": None,
            "publish_progress": None,
            "covers": [],
            "cover_status": "idle",
        }
        (workdir / "job_state.json").write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return job_id, workdir, master

    def test_restart_recovers_master_and_routes_preparation(self):
        job_id, workdir, master = self.build_state()
        with mock.patch.object(app, "MONTAGE_WORK", app.MONTAGE_WORK), mock.patch.object(app, "jobs", {}):
            app._recover_montage_jobs()
            job = app.jobs.get(job_id)
            self.assertIsNotNone(job)
            self.assertEqual(app._master_video_path(job), master)
            with mock.patch.object(app.montage_engine, "usable_video", return_value=True):
                self.assertEqual(app._prep_source_video(job, False), master)
            # Cleanup after a restarted job must keep the master alive.
            summary = app._cleanup_render_artifacts(job_id, job)
            self.assertFalse(summary["errors"])
            self.assertIn(master.name, summary["protected"])
            self.assertTrue(master.is_file())

    def test_missing_master_after_restart_falls_back(self):
        job_id, workdir, master = self.build_state(job_id="restart02")
        master.unlink()
        with mock.patch.object(app, "jobs", {}):
            app._recover_montage_jobs()
            job = app.jobs.get(job_id)
            self.assertIsNotNone(job)
            self.assertIsNone(app._master_video_path(job))
            self.assertEqual(app._prep_source_video(job, False), Path(job["result"]["result_video"]))


if __name__ == "__main__":
    unittest.main()
