"""Mock tests: POST /api/montage/{job_id}/cancel stops processes and cleans up.

Verifies, without any real FFmpeg/network work:

* cancel of an idle job returns 409 (nothing to stop);
* cancel during the render phase (no usable result yet) stops the tracked
  task, deletes the whole workdir and forgets the job + auto-project, so the
  user can start from scratch;
* cancel when a finished result exists keeps the files and only marks the
  interrupted processes as cancelled.

Run:
  python -m unittest test_montage_cancel
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

import app as app_module


def make_job(job_id, workdir, **extra):
    job = {
        "id": job_id,
        "kind": "montage",
        "status": "processing",
        "phase": "rendering",
        "stage": "rendering",
        "detail": "",
        "progress": 10,
        "error": None,
        "result": None,
        "video_path": str(workdir / "source.mp4"),
        "srt_text": "test",
        "params": {},
        "publish": None,
        "publish_errors": None,
        "publish_progress": {},
        "covers": [],
        "cover_status": "idle",
        "variants": {},
        "mirror_variant": None,
        "publish_prep": {},
        "prepare_operation": None,
        "prepare_status": "idle",
        "prepare_error": None,
        "cancel_requested": False,
    }
    job.update(extra)
    app_module.jobs[job_id] = job
    return job


class CancelEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work_root = Path(self.tmp.name)
        self._montage_work = app_module.MONTAGE_WORK
        app_module.MONTAGE_WORK = self.work_root
        self._jobs = dict(app_module.jobs)
        self._tasks = dict(app_module.MONTAGE_TASKS)
        self._projects = dict(app_module.PROJECTS)
        app_module.MONTAGE_TASKS.clear()
        self._save_projects = app_module._save_projects
        app_module._save_projects = lambda: None

    async def asyncTearDown(self):
        app_module.MONTAGE_WORK = self._montage_work
        app_module.jobs.clear()
        app_module.jobs.update(self._jobs)
        app_module.MONTAGE_TASKS.clear()
        app_module.MONTAGE_TASKS.update(self._tasks)
        app_module.PROJECTS.clear()
        app_module.PROJECTS.update(self._projects)
        app_module._save_projects = self._save_projects
        self.tmp.cleanup()

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module.app), base_url="http://t")

    async def test_cancel_idle_job_conflict(self):
        workdir = self.work_root / "idle1"
        workdir.mkdir(parents=True)
        make_job("idle1", workdir, status="done", phase="ready", stage="done",
                 result={"result_video": str(workdir / "final.mp4")})
        async with self.client() as client:
            resp = await client.post("/api/montage/idle1/cancel")
        self.assertEqual(resp.status_code, 409)

    async def test_cancel_unknown_job_404(self):
        async with self.client() as client:
            resp = await client.post("/api/montage/nope1234/cancel")
        self.assertEqual(resp.status_code, 404)

    async def test_cancel_render_wipes_everything(self):
        workdir = self.work_root / "rend1"
        workdir.mkdir(parents=True)
        (workdir / "source.mp4").write_bytes(b"fake-source")
        (workdir / "sub_words.mp4").write_bytes(b"partial-render")
        make_job("rend1", workdir)
        app_module.PROJECTS["p_rend1"] = {
            "id": "p_rend1", "name": "upload.mp4",
            "video_path": str(workdir / "source.mp4"),
        }

        stopped = asyncio.Event()

        async def fake_render():
            while not app_module._cancel_requested("rend1"):
                await asyncio.sleep(0.02)
            stopped.set()

        app_module._track_montage_task("rend1", fake_render())
        self.assertIn("rend1", app_module.MONTAGE_TASKS)

        async with self.client() as client:
            resp = await client.post("/api/montage/rend1/cancel")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body.get("cancelled"))
        self.assertTrue(body.get("wiped"))
        self.assertTrue(stopped.is_set)
        self.assertFalse(workdir.exists(), "workdir must be deleted to start from scratch")
        self.assertNotIn("rend1", app_module.jobs)
        self.assertNotIn("p_rend1", app_module.PROJECTS)
        self.assertNotIn("rend1", app_module.MONTAGE_TASKS)

    async def test_cancel_with_result_keeps_files(self):
        workdir = self.work_root / "pub1"
        workdir.mkdir(parents=True)
        result_video = workdir / "final.mp4"
        result_video.write_bytes(b"finished-video")
        make_job("pub1", workdir, status="publishing", stage="publishing",
                 result={"result_video": str(result_video)},
                 publish_progress={"instagram_feed": {
                     "label": "Instagram", "progress": 10, "status": "publishing",
                     "detail": "Загрузка", "steps": []}},
                 publish_status={"instagram_feed": "publishing"},
                 publish_errors={})

        async def fake_publish():
            while not app_module._cancel_requested("pub1"):
                await asyncio.sleep(0.02)
            app_module._mark_publish_cancelled(
                "pub1", [{"id": "instagram_feed", "kind": "instagram"}])

        app_module._track_montage_task("pub1", fake_publish())

        async with self.client() as client:
            resp = await client.post("/api/montage/pub1/cancel")
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        self.assertTrue(body.get("cancelled"))
        self.assertFalse(body.get("wiped"))
        self.assertTrue(result_video.exists(), "finished result must survive cancel")
        job = app_module.jobs["pub1"]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["publish_status"]["instagram_feed"], "failed")
        self.assertEqual(job["publish_errors"]["instagram_feed"], "Отменено пользователем")


if __name__ == "__main__":
    unittest.main()
