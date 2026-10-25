import tempfile
import time
import unittest
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

import app as app_module


class SilenceAnalysisApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "montage_work"
        self.root.mkdir()
        self.old_work = app_module.MONTAGE_WORK
        self.old_analysis_base = app_module.SILENCE_ANALYSIS_BASE_DIR
        self.old_analysis_dir = app_module.SILENCE_ANALYSIS_DIR
        app_module.MONTAGE_WORK = self.root
        app_module.SILENCE_ANALYSIS_BASE_DIR = self.root / ".silence_analysis"
        app_module.SILENCE_ANALYSIS_DIR = self.root / ".silence_analysis"
        app_module._SILENCE_SESSIONS.clear()

    def tearDown(self):
        app_module._SILENCE_SESSIONS.clear()
        app_module.MONTAGE_WORK = self.old_work
        app_module.SILENCE_ANALYSIS_BASE_DIR = self.old_analysis_base
        app_module.SILENCE_ANALYSIS_DIR = self.old_analysis_dir
        self.temp.cleanup()

    def post_video(self, content=b"small-video"):
        return self.client.post(
            "/api/montage/silence/analyze",
            content=content,
            headers={"content-type": "application/octet-stream", "x-filename": "clip.mp4"},
        )

    def test_worker_rss_probe_converts_ps_kilobytes_to_bytes(self):
        result = subprocess.CompletedProcess([], 0, stdout="65536\n", stderr="")
        with mock.patch.object(app_module.subprocess, "run", return_value=result):
            self.assertEqual(app_module._silence_worker_rss_bytes(123), 64 * 1024 * 1024)

    def test_session_cleanup_preserves_other_live_worker_namespace(self):
        with tempfile.TemporaryDirectory() as temp:
            old_base = app_module.SILENCE_ANALYSIS_BASE_DIR
            old_dir = app_module.SILENCE_ANALYSIS_DIR
            base = Path(temp) / "sessions"
            current = base / app_module.SILENCE_PROCESS_NAMESPACE
            current.mkdir(parents=True)
            fake_pid = 98765432
            other_worker = base / f"{fake_pid}-other-process"
            other_worker.mkdir()
            (other_worker / "snapshot.bin").write_bytes(b"keep")
            legacy_orphan = base / "old-unnamespaced-session"
            legacy_orphan.mkdir()
            (legacy_orphan / "snapshot.bin").write_bytes(b"remove")
            app_module._SILENCE_SESSIONS.clear()
            app_module.SILENCE_ANALYSIS_BASE_DIR = base
            app_module.SILENCE_ANALYSIS_DIR = current
            original_kill = os.kill
            try:
                with mock.patch.object(app_module.os, "kill", side_effect=lambda pid, sig: None if pid == fake_pid else original_kill(pid, sig)):
                    with TestClient(app_module.app):
                        pass
                self.assertTrue((other_worker / "snapshot.bin").is_file())
                self.assertFalse(legacy_orphan.exists())
            finally:
                app_module.SILENCE_ANALYSIS_BASE_DIR = old_base
                app_module.SILENCE_ANALYSIS_DIR = old_dir

    def test_progress_endpoint_exposes_phase_and_percent(self):
        request_id = str(uuid.uuid4())
        app_module._record_silence_progress(
            request_id, status="running", phase="decoding", percent=43.5,
            detail="Обработано 4.3 из 10.0 с",
        )
        with TestClient(app_module.app) as client:
            response = client.get(f"/api/montage/silence/progress/{request_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["phase"], "decoding")
        self.assertEqual(response.json()["percent"], 43.5)

    def test_capacity_error_reports_time_until_oldest_session_expiry(self):
        now = time.time()
        app_module._SILENCE_SESSIONS.clear()
        app_module._SILENCE_SESSIONS["busy-1"] = {
            "directory":str(app_module.SILENCE_ANALYSIS_DIR / "busy1"),
            "expires_at":now + 45, "status":"ready",
        }
        app_module._SILENCE_SESSIONS["busy-2"] = {
            "directory":str(app_module.SILENCE_ANALYSIS_DIR / "busy2"),
            "expires_at":now + 1800, "status":"ready",
        }
        request_id = str(uuid.uuid4())
        try:
            with TestClient(app_module.app) as client:
                response = client.post(
                    "/api/montage/silence/analyze", content=b"x",
                    headers={"content-type":"application/octet-stream", "x-filename":"x.mp4",
                             "x-analysis-request":request_id},
                )
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.json()["context"]["wait_seconds"], int(response.headers["retry-after"]))
            self.assertLessEqual(response.json()["context"]["wait_seconds"], 45)
            with TestClient(app_module.app) as client:
                progress = client.get(f"/api/montage/silence/progress/{request_id}")
            self.assertEqual(progress.json()["status"], "capacity")
            self.assertEqual(progress.json()["wait_seconds"], response.json()["context"]["wait_seconds"])
        finally:
            app_module._SILENCE_SESSIONS.clear()

    def test_ready_session_can_be_released_to_free_capacity(self):
        analysis_id = "release-test-id"
        session_dir = app_module.SILENCE_ANALYSIS_DIR / "release-session"
        session_dir.mkdir(parents=True)
        source = session_dir / "source.mp4"
        source.write_bytes(b"snapshot")
        app_module._SILENCE_SESSIONS[analysis_id] = {
            "analysis_id":analysis_id, "directory":str(session_dir), "source_path":str(source),
            "status":"ready", "expires_at":time.time()+3600, "request_id":str(uuid.uuid4()),
        }
        with TestClient(app_module.app) as client:
            response = client.delete(f"/api/montage/silence/analyze/{analysis_id}")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["released"])
        self.assertFalse(session_dir.exists())
        self.assertNotIn(analysis_id, app_module._SILENCE_SESSIONS)

    def test_upload_returns_analysis_and_binds_immutable_snapshot(self):
        analysis = {
            "sample_rate_hz": 48000,
            "channels": 1,
            "audio_duration_ms": 20,
            "mean_rms_dbfs": -9.03,
            "max_peak_dbfs": -6.02,
            "envelope_dbfs_x100": [-602, -12000],
        }
        with TestClient(app_module.app) as self.client, \
                mock.patch.object(app_module, "_probe_silence_source", return_value={
                    "duration_ms": 1000, "fps": "30/1", "audio": {"channels": 1},
                }), \
                mock.patch.object(app_module, "_decode_silence_audio", new=AsyncMock(return_value=analysis)):
            response = self.post_video(b"source bytes")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["audio_available"])
        self.assertEqual(body["envelope_dbfs_x100"], [-602, -12000])
        session = app_module._SILENCE_SESSIONS[body["analysis_id"]]
        source = Path(session["source_path"])
        self.assertEqual(source.read_bytes(), b"source bytes")
        self.assertEqual(session["source_sha256"], app_module._file_sha256(str(source)))
        self.assertEqual(session["status"], "ready")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required")
    def test_real_short_upload_runs_probe_and_streaming_decode(self):
        media = Path(self.temp.name) / "fixture.mp4"
        subprocess.run([
            shutil.which("ffmpeg"), "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=64x64:r=30:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-t", "1", str(media),
        ], check=True, capture_output=True)
        with TestClient(app_module.app) as client:
            response = client.post(
                "/api/montage/silence/analyze", content=media.read_bytes(),
                headers={"content-type":"application/octet-stream", "x-filename":"fixture.mp4"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["audio_available"])
        self.assertEqual(body["sample_rate_hz"], 48000)
        self.assertTrue(body["envelope_dbfs_x100"])
        self.assertGreater(body["max_peak_dbfs"], -20)

    def test_capacity_and_upload_bounds_return_problem_details(self):
        analysis = {"sample_rate_hz": 48000, "channels": 1, "audio_duration_ms": 0,
                    "mean_rms_dbfs": -120.0, "max_peak_dbfs": -120.0,
                    "envelope_dbfs_x100": []}
        with TestClient(app_module.app) as self.client, \
                mock.patch.object(app_module, "_probe_silence_source", return_value={
                    "duration_ms": 1000, "fps": "30/1", "audio": None,
                }), \
                mock.patch.object(app_module, "_decode_silence_audio", new=AsyncMock(return_value=analysis)):
            self.assertEqual(self.post_video(b"one").status_code, 200)
            self.assertEqual(self.post_video(b"two").status_code, 200)
            limited = self.post_video(b"three")
            self.assertEqual(limited.status_code, 429)
            self.assertEqual(limited.headers["content-type"], "application/problem+json")
            self.assertEqual(limited.json()["code"], "analysis_capacity")
            app_module._SILENCE_SESSIONS.pop(next(iter(app_module._SILENCE_SESSIONS)))
            with mock.patch.object(app_module, "SILENCE_SOURCE_MAX_BYTES", 4):
                too_large = self.post_video(b"12345")
            self.assertEqual(too_large.status_code, 413)
            self.assertEqual(too_large.json()["code"], "source_too_large")
        self.assertEqual(len(app_module._SILENCE_SESSIONS), 1)

    def test_no_audio_is_a_successful_manual_edit_session(self):
        analysis = {"sample_rate_hz": 48000, "channels": 0, "audio_duration_ms": 0,
                    "mean_rms_dbfs": None, "max_peak_dbfs": None,
                    "envelope_dbfs_x100": []}
        with TestClient(app_module.app) as self.client, \
                mock.patch.object(app_module, "_probe_silence_source", return_value={
                    "duration_ms": 1000, "fps": "30/1", "audio": None,
                }):
            response = self.post_video()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["audio_available"])
        self.assertIsNone(response.json()["mean_rms_dbfs"])
        self.assertIn(response.json()["analysis_id"], app_module._SILENCE_SESSIONS)

    def test_project_id_is_resolved_and_copied_into_server_snapshot(self):
        project_video = Path(self.temp.name) / "project-source.mp4"
        project_video.write_bytes(b"project video bytes")
        old_projects = dict(app_module.PROJECTS)
        app_module.PROJECTS.clear()
        app_module.PROJECTS["p_test"] = {
            "id": "p_test", "name": "Project", "video_path": str(project_video),
            "transcript_job_id": "t_test", "covers": [], "covers_dir": "",
        }
        analysis = {"sample_rate_hz": 48000, "channels": 1, "audio_duration_ms": 1000,
                    "mean_rms_dbfs": -20.0, "max_peak_dbfs": -5.0,
                    "envelope_dbfs_x100": [-2000] * 100}
        try:
            with TestClient(app_module.app) as client, \
                    mock.patch.object(app_module, "_probe_silence_source", return_value={
                        "duration_ms": 1000, "fps": "30/1", "audio": {"channels": 1},
                    }), \
                    mock.patch.object(app_module, "_decode_silence_audio", new=AsyncMock(return_value=analysis)):
                response = client.post(
                    "/api/montage/silence/analyze",
                    json={"project_id": "p_test"},
                )
            self.assertEqual(response.status_code, 200, response.text)
            session = app_module._SILENCE_SESSIONS[response.json()["analysis_id"]]
            self.assertEqual(session["project_id"], "p_test")
            self.assertNotEqual(Path(session["source_path"]).resolve(), project_video.resolve())
            self.assertEqual(Path(session["source_path"]).read_bytes(), project_video.read_bytes())
        finally:
            app_module.PROJECTS.clear()
            app_module.PROJECTS.update(old_projects)

    def test_trimmed_render_atomically_hands_snapshot_to_job(self):
        analysis_id = "handoff-test-id"
        session_dir = app_module.SILENCE_ANALYSIS_DIR / "handoff-session"
        session_dir.mkdir(parents=True)
        source = session_dir / "source.mp4"
        source.write_bytes(b"immutable analyzed source")
        digest = app_module._file_sha256(str(source))
        app_module._SILENCE_SESSIONS[analysis_id] = {
            "analysis_id": analysis_id,
            "directory": str(session_dir),
            "source_path": str(source),
            "source_name": "fixture.mp4",
            "filename": "fixture.mp4",
            "project_id": None,
            "source_sha256": digest,
            "source_duration_ms": 1000,
            "fps": "30/1",
            "analysis": {},
            "status": "ready",
            "expires_at": time.time() + 3600,
        }
        old_jobs = dict(app_module.jobs)
        old_projects = dict(app_module.PROJECTS)
        app_module.jobs.clear()
        app_module.PROJECTS.clear()

        def do_not_start_background(job_id, coroutine):
            if hasattr(coroutine, "close"):
                coroutine.close()
            return None

        try:
            with TestClient(app_module.app) as client, \
                    mock.patch.object(app_module, "_save_projects", lambda: None), \
                    mock.patch.object(app_module, "_track_montage_task", do_not_start_background):
                response = client.post(
                    "/api/montage/render",
                    data={
                        "silence_trim_enabled":"true",
                        "silence_analysis_id":analysis_id,
                        "silence_cuts_json":"[{\"cut_id\":\"manual-1\",\"start_ms\":200,\"end_ms\":400,\"origin\":\"manual\"}]",
                    },
                    files={"srt": ("segments.srt", b"1\n00:00:00,000 --> 00:00:01,000\nhello\n", "text/plain")},
                )
            self.assertEqual(response.status_code, 200, response.text)
            job_id = response.json()["job_id"]
            job = app_module.jobs[job_id]
            job_source = Path(job["video_path"])
            self.assertEqual(job_source.read_bytes(), b"immutable analyzed source")
            self.assertEqual(job["params"]["silence_cuts"][0]["cut_id"], "manual-1")
            self.assertEqual(job["params"]["silence_source_sha256"], digest)
            self.assertNotIn(analysis_id, app_module._SILENCE_SESSIONS)
            self.assertFalse(source.exists())
            persisted = json.loads((Path(app_module.MONTAGE_WORK) / job_id / "job_state.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["params"]["silence_cuts"][0]["cut_id"], "manual-1")
            self.assertNotIn("silence_analysis_id", json.dumps(persisted))
        finally:
            app_module.jobs.clear()
            app_module.jobs.update(old_jobs)
            app_module.PROJECTS.clear()
            app_module.PROJECTS.update(old_projects)


class SilenceRenderValidationTests(unittest.TestCase):
    def test_invalid_cut_fails_before_job_creation(self):
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as temp:
            old_work = app_module.MONTAGE_WORK
            old_base = app_module.SILENCE_ANALYSIS_BASE_DIR
            old_dir = app_module.SILENCE_ANALYSIS_DIR
            work = Path(temp) / "work"
            work.mkdir()
            app_module.MONTAGE_WORK = work
            app_module.SILENCE_ANALYSIS_BASE_DIR = work / ".silence_analysis"
            app_module.SILENCE_ANALYSIS_DIR = work / ".silence_analysis"
            analysis_id = "test-analysis-id"
            app_module._SILENCE_SESSIONS.clear()
            app_module._SILENCE_SESSIONS[analysis_id] = {
                "analysis_id": analysis_id,
                "directory": str(app_module.SILENCE_ANALYSIS_DIR / "session"),
                "source_path": str(app_module.SILENCE_ANALYSIS_DIR / "session" / "source.mp4"),
                "status": "ready", "expires_at": time.time() + 3600,
                "source_duration_ms": 1000, "fps": "30/1", "analysis": {},
            }
            try:
                with TestClient(app_module.app) as client:
                    response = client.post("/api/montage/render", data={
                        "silence_trim_enabled": "true",
                        "silence_analysis_id": analysis_id,
                        "silence_cuts_json": '[{"cut_id":"bad","start_ms":0,"end_ms":1001,"origin":"manual"}]',
                    })
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.headers["content-type"], "application/problem+json")
                self.assertEqual(response.json()["code"], "invalid_cut_list")
                self.assertEqual(list(work.glob("????????")), [])
            finally:
                app_module._SILENCE_SESSIONS.clear()
                app_module.MONTAGE_WORK = old_work
                app_module.SILENCE_ANALYSIS_BASE_DIR = old_base
                app_module.SILENCE_ANALYSIS_DIR = old_dir

    def test_same_size_source_mutation_is_rejected_at_handoff(self):
        with tempfile.TemporaryDirectory() as temp:
            old_work = app_module.MONTAGE_WORK
            old_base = app_module.SILENCE_ANALYSIS_BASE_DIR
            old_dir = app_module.SILENCE_ANALYSIS_DIR
            work = Path(temp) / "work"
            analysis_dir = work / ".silence_analysis"
            session_dir = analysis_dir / "session"
            session_dir.mkdir(parents=True)
            app_module.MONTAGE_WORK = work
            app_module.SILENCE_ANALYSIS_BASE_DIR = analysis_dir
            app_module.SILENCE_ANALYSIS_DIR = analysis_dir
            analysis_id = "mutation-test-id"
            source = session_dir / "source.mp4"
            source.write_bytes(b"before"); expected_hash = app_module._file_sha256(str(source))
            source.write_bytes(b"after!")
            app_module._SILENCE_SESSIONS.clear()
            app_module._SILENCE_SESSIONS[analysis_id] = {
                "analysis_id":analysis_id, "directory":str(session_dir), "source_path":str(source),
                "filename":"source.mp4", "source_name":"source.mp4", "project_id":None,
                "source_sha256":expected_hash, "source_duration_ms":1000, "fps":"30/1",
                "analysis":{}, "status":"ready", "expires_at":time.time()+3600,
            }
            try:
                with TestClient(app_module.app) as client:
                    response = client.post(
                        "/api/montage/render",
                        data={
                            "silence_trim_enabled":"true", "silence_analysis_id":analysis_id,
                            "silence_cuts_json":"[{\"cut_id\":\"m\",\"start_ms\":100,\"end_ms\":200,\"origin\":\"manual\"}]",
                        },
                        files={"srt":("segments.srt",b"1\n00:00:00,000 --> 00:00:01,000\nhello\n","text/plain")},
                    )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["code"], "source_changed")
                self.assertEqual(response.json()["context"]["cause"], "snapshot_hash_mismatch")
                self.assertEqual(list(p for p in work.iterdir() if p.name != ".silence_analysis"), [])
            finally:
                app_module._SILENCE_SESSIONS.clear()
                app_module.MONTAGE_WORK = old_work
                app_module.SILENCE_ANALYSIS_BASE_DIR = old_base
                app_module.SILENCE_ANALYSIS_DIR = old_dir

    def test_expired_analysis_is_cleaned_and_returns_410(self):
        with tempfile.TemporaryDirectory() as temp:
            old_work = app_module.MONTAGE_WORK
            old_base = app_module.SILENCE_ANALYSIS_BASE_DIR
            old_dir = app_module.SILENCE_ANALYSIS_DIR
            work = Path(temp) / "work"
            analysis_dir = work / ".silence_analysis"
            session_dir = analysis_dir / "expired-session"
            session_dir.mkdir(parents=True)
            app_module.MONTAGE_WORK = work
            app_module.SILENCE_ANALYSIS_BASE_DIR = analysis_dir
            app_module.SILENCE_ANALYSIS_DIR = analysis_dir
            analysis_id = "expired-test-id"
            source = session_dir / "source.mp4"
            source.write_bytes(b"expired")
            app_module._SILENCE_SESSIONS.clear()
            app_module._SILENCE_SESSIONS[analysis_id] = {
                "analysis_id":analysis_id, "directory":str(session_dir), "source_path":str(source),
                "filename":"source.mp4", "status":"ready", "expires_at":time.time()-1,
            }
            try:
                with TestClient(app_module.app) as client:
                    response = client.post("/api/montage/render", data={
                        "silence_trim_enabled":"true", "silence_analysis_id":analysis_id,
                        "silence_cuts_json":"[]",
                    })
                self.assertEqual(response.status_code, 410)
                self.assertEqual(response.json()["code"], "analysis_expired")
                self.assertFalse(session_dir.exists())
                self.assertNotIn(analysis_id, app_module._SILENCE_SESSIONS)
            finally:
                app_module._SILENCE_SESSIONS.clear()
                app_module.MONTAGE_WORK = old_work
                app_module.SILENCE_ANALYSIS_BASE_DIR = old_base
                app_module.SILENCE_ANALYSIS_DIR = old_dir


if __name__ == "__main__":
    unittest.main()
