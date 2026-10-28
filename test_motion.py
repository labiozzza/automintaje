import asyncio
import errno
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from motion.models import MotionPlan, parse_motion_plan
from motion.opencode_bridge import OpenCodeBridge
from motion.publisher_bridge import build_output_job
from motion.repository import MotionRepository
from motion.service import MotionService
from motion.source_snapshot import create_source_snapshot, probe_video, verify_snapshot
from motion.transcript_map import build_output_transcript


def valid_plan():
    return {
        "schema_version": "motion-plan.v1",
        "source_job_id": "parent123",
        "source_fingerprint": "sha256:" + ("a" * 64),
        "revision": 1,
        "profile": "minimal",
        "duration_ms": 5000,
        "output": {"width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
        "scenes": [{
            "scene_id": "s01", "start_ms": 1000, "end_ms": 2500,
            "review_status": "proposed", "spoken_evidence": "Пример",
            "semantic_goal": "Выделить ключевую мысль", "effect_type": "animated_text",
            "content": {"text": "Пример"},
            "placement": {"x": 0.5, "y": 0.35, "w": 0.7, "h": 0.2, "anchor": "center"},
            "animation": {"preset": "fade", "enter_ms": 300, "exit_ms": 200, "delay_ms": 0},
            "evidence_time_ms": 1200, "explanation": "Поддерживает речь", "asset_refs": [],
        }],
    }


class MotionSchemaTests(unittest.TestCase):
    def test_open_code_prompt_embeds_schema_without_unsupported_format_field(self):
        text = MotionService._prompt_with_schema("Верни Motion Plan.")
        marker = "Точная JSON Schema motion-plan.v1 (верни объект, соответствующий ей):\n"
        schema = json.loads(text.split(marker, 1)[1])
        self.assertEqual(schema["properties"]["schema_version"]["const"], "motion-plan.v1")

        with tempfile.TemporaryDirectory() as temp:
            bridge = OpenCodeBridge(Path(temp), Path(temp) / "state")
            with mock.patch.object(bridge, "_request", return_value=None) as request:
                bridge.prompt_async(
                    "ses_test", "openai/gpt-6-luna", [{"type": "text", "text": text}],
                    message_id="msg_test", system="system",
                )
            body = request.call_args.kwargs["body"]
        self.assertNotIn("format", body)
        self.assertEqual(body["model"], {"providerID": "openai", "modelID": "gpt-6-luna"})

    def test_plan_is_bound_to_source_and_revision(self):
        raw = valid_plan()
        plan = parse_motion_plan(
            raw, duration_ms=5000, source_job_id="parent123",
            fingerprint="sha256:" + ("b" * 64), revision=2,
        )
        self.assertEqual(plan.revision, 2)
        self.assertEqual(plan.source_fingerprint, "sha256:" + ("b" * 64))
        self.assertEqual(plan.scenes[0].scene_id, "s01")

    def test_output_schema_supports_broadcast_rational_fps_but_caps_actual_rate(self):
        raw = valid_plan()
        raw["output"]["fps_num"] = 30000
        raw["output"]["fps_den"] = 1001
        self.assertEqual(MotionPlan.model_validate(raw).output.fps_num, 30000)
        raw["output"]["fps_num"] = 300000
        with self.assertRaises(ValidationError):
            MotionPlan.model_validate(raw)

    def test_plan_rejects_code_numeric_hallucinations_and_unsafe_geometry(self):
        raw = valid_plan()
        raw["scenes"][0]["content"] = {"text": "x", "path": "/tmp/secret"}
        with self.assertRaises(ValidationError):
            MotionPlan.model_validate(raw)
        raw = valid_plan()
        raw["scenes"][0]["effect_type"] = "stat_card"
        raw["scenes"][0]["content"] = {"label": "Доход", "value": 1000}
        with self.assertRaises(ValidationError):
            MotionPlan.model_validate(raw)
        raw = valid_plan()
        raw["scenes"][0]["placement"]["x"] = 0.98
        with self.assertRaises(ValidationError):
            MotionPlan.model_validate(raw)

    def test_anchor_aware_placement_accepts_top_left_and_rejects_overflow(self):
        placements = [
            {"x": 0.1, "y": 0.08, "w": 0.7, "h": 0.7, "anchor": "top-left"},
            {"x": 0.9, "y": 0.08, "w": 0.7, "h": 0.7, "anchor": "top-right"},
            {"x": 0.1, "y": 0.92, "w": 0.7, "h": 0.7, "anchor": "bottom-left"},
            {"x": 0.9, "y": 0.92, "w": 0.7, "h": 0.7, "anchor": "bottom-right"},
            {"x": 0.5, "y": 0.5, "w": 0.7, "h": 0.7, "anchor": "center"},
        ]
        for placement in placements:
            with self.subTest(anchor=placement["anchor"]):
                raw = valid_plan()
                raw["scenes"][0]["placement"] = placement
                plan = MotionPlan.model_validate(raw)
                self.assertEqual(plan.scenes[0].placement.anchor, placement["anchor"])

        raw = valid_plan()
        raw["scenes"][0]["placement"] = {
            "x": 0.4, "y": 0.08, "w": 0.7, "h": 0.7, "anchor": "top-left",
        }
        with self.assertRaises(ValidationError):
            MotionPlan.model_validate(raw)

    def test_transcript_maps_word_timing_through_silence_edl(self):
        transcript = [{
            "text": "один два", "start": 0.0, "end": 1.0,
            "word_timestamps": [
                {"word": "один", "start": 0.0, "end": 0.4},
                {"word": "два", "start": 0.6, "end": 1.0},
            ],
        }]
        segments = [
            {"source_start_ms": 0, "source_end_ms": 500, "output_start_ms": 0, "output_end_ms": 500},
            {"source_start_ms": 700, "source_end_ms": 1500, "output_start_ms": 500, "output_end_ms": 1300},
        ]
        mapped = build_output_transcript(transcript, "", segments)
        self.assertEqual([(item["start_ms"], item["end_ms"]) for item in mapped["segments"]], [(0, 400), (500, 800)])
        self.assertFalse(mapped["exact_word_sync_available"])
        self.assertTrue(mapped["segments"][0]["word_aligned"])
        self.assertFalse(mapped["segments"][1]["word_aligned"])


class MotionStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_probe_video_uses_nominal_rate_when_average_rate_is_timestamp_artifact(self):
        payload = {
            "streams": [{
                "codec_type": "video", "r_frame_rate": "30/1",
                "avg_frame_rate": "338250000/11267329", "duration": "45.069316",
                "nb_frames": "1353", "width": 720, "height": 1280,
            }],
            "format": {"duration": "45.102"},
        }
        result = mock.Mock(returncode=0, stdout=json.dumps(payload))
        with mock.patch("motion.source_snapshot.subprocess.run", return_value=result), \
             mock.patch("motion.source_snapshot.probe_color_profile", return_value={"hdr": False}):
            metadata = probe_video(Path("source.mp4"))
        self.assertEqual((metadata["fps_num"], metadata["fps_den"]), (30, 1))

    def test_probe_video_preserves_supported_broadcast_rate(self):
        payload = {
            "streams": [{
                "codec_type": "video", "r_frame_rate": "30000/1001",
                "avg_frame_rate": "30000/1001", "duration": "1", "width": 720, "height": 1280,
            }],
            "format": {"duration": "1"},
        }
        result = mock.Mock(returncode=0, stdout=json.dumps(payload))
        with mock.patch("motion.source_snapshot.subprocess.run", return_value=result), \
             mock.patch("motion.source_snapshot.probe_color_profile", return_value={"hdr": False}):
            metadata = probe_video(Path("source.mp4"))
        self.assertEqual((metadata["fps_num"], metadata["fps_den"]), (30000, 1001))

    def test_snapshot_hardlink_survives_parent_retention_without_chmod(self):
        source = self.root / "master.mp4"
        source.write_bytes(b"immutable-master-data")
        original_mode = stat.S_IMODE(source.stat().st_mode)
        project_dir = self.root / ".motion" / "projects" / ("motion_" + "a" * 32)
        metadata = {
            "duration_ms": 5000, "duration_seconds": 5.0, "fps_num": 30, "fps_den": 1,
            "width": 1080, "height": 1920,
        }
        with mock.patch("motion.source_snapshot.probe_video", return_value=metadata):
            snapshot, provenance = create_source_snapshot(source, project_dir, "parent", "master")
            source.unlink()
            verified = verify_snapshot(snapshot, provenance["sha256"], provenance["size_bytes"])
        self.assertTrue(snapshot.is_file())
        self.assertEqual(provenance["snapshot_mode"], "hardlink")
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), original_mode)
        self.assertEqual(verified["duration_ms"], 5000)

    def test_repository_persists_project_revisions_and_operations(self):
        repo = MotionRepository(self.root / ".motion" / "motion.sqlite3")
        project = repo.create_project({
            "id": "motion_" + "b" * 32, "source_job_id": "parent", "source_fingerprint": "sha256:" + "a" * 64,
            "status": "created", "current_revision": 0, "manifest_note": "persisted",
        })
        plan = valid_plan()
        revision = repo.create_revision(project["id"], 1, plan, {}, "review")
        operation = repo.create_operation(project["id"], "design", "request-1")
        loaded = repo.get_project(project["id"])
        self.assertEqual(loaded["manifest_note"], "persisted")
        self.assertEqual(repo.get_revision(project["id"], 1)["plan"]["scenes"][0]["scene_id"], "s01")
        self.assertEqual(repo.get_operation(operation["id"])["status"], "queued")
        self.assertEqual(revision["status"], "review")

    def test_output_job_has_existing_publication_contract(self):
        project = {"id": "motion_" + "c" * 32, "source_job_id": "parent", "source_name": "video.mp4"}
        revision = {"revision": 2, "plan": valid_plan()}
        rendered = {
            "job_id": "mg_" + "d" * 32,
            "final_video": str(self.root / "final.mp4"),
            "master_video": str(self.root / "master.mp4"),
            "preview_video": str(self.root / "preview.mp4"),
            "duration_seconds": 5.0,
        }
        for key in ("final_video", "master_video", "preview_video"):
            Path(rendered[key]).write_bytes(b"video")
        job = build_output_job(project, revision, rendered)
        self.assertEqual(job["kind"], "montage")
        self.assertEqual(job["origin"], "motion_graphics_plus")
        self.assertEqual(job["motion_revision"], 2)
        self.assertEqual(job["result"]["result_video"], str(Path(rendered["final_video"]).resolve()))
        self.assertIn("publish_prep", job)

    def test_motion_output_registration_resolves_master_basename_inside_workdir(self):
        import app

        job_id = "mg_motion_test_register"
        workdir = self.root / job_id
        workdir.mkdir()
        for filename in ("master.mp4", "final.mp4", "preview.mp4"):
            (workdir / filename).write_bytes(b"video")
        job = {
            "id": job_id, "origin": "motion_graphics_plus",
            "result": {
                "master_video": "master.mp4",
                "result_video": str(workdir / "final.mp4"),
                "preview_video": str(workdir / "preview.mp4"),
            },
        }
        with mock.patch.dict(app.jobs, {}), \
             mock.patch.object(app, "_safe_montage_workdir", return_value=workdir), \
             mock.patch.object(app, "_save_montage_job") as save_job:
            self.assertEqual(app._register_motion_output_job(job), job_id)
            save_job.assert_called_once_with(job_id)


class MotionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_transcript_clears_unverifiable_quotes_without_discarding_plan(self):
        service = MotionService(
            work_root=self.root / "montage_work", project_root=Path(__file__).resolve().parent,
            source_resolver=lambda _job, _kind: {}, model_list=lambda: [], output_register=lambda _job: "",
        )
        raw = valid_plan()
        raw["scenes"][0]["spoken_evidence"] = "motion-design-label"
        sanitized = service._clear_unverifiable_spoken_evidence(
            raw, {"transcript": {"segments": []}},
        )
        self.assertEqual(raw["scenes"][0]["spoken_evidence"], "motion-design-label")
        self.assertEqual(sanitized["scenes"][0]["spoken_evidence"], "")
        plan = parse_motion_plan(
            sanitized, duration_ms=5000, source_job_id="parent123",
            fingerprint="sha256:" + ("a" * 64), revision=1,
        )
        service._validate_numeric_evidence(plan, {"transcript": {"segments": []}}, "")

    def test_design_prompt_tells_model_not_to_invent_quotes_when_transcript_missing(self):
        from types import SimpleNamespace

        service = MotionService(
            work_root=self.root / "montage_work", project_root=Path(__file__).resolve().parent,
            source_resolver=lambda _job, _kind: {}, model_list=lambda: [], output_register=lambda _job: "",
        )
        project = {
            "id": "motion_" + "2" * 32, "source_name": "video.mp4",
            "source_fingerprint": "sha256:" + ("a" * 64), "source_metadata": {
                "duration_ms": 5000, "fps_num": 30, "fps_den": 1, "width": 1080, "height": 1920,
            },
        }
        prompt = service._build_design_prompt(
            project, {"transcript": {"segments": []}},
            SimpleNamespace(instruction="Покажи, что я делаю"), None, 1, "",
        )
        self.assertIn("Транскрипт недоступен", prompt)
        self.assertIn("spoken_evidence пустым", prompt)

    def test_legacy_snapshot_with_unbounded_fps_is_reprobed_and_migrated(self):
        service = MotionService(
            work_root=self.root / "montage_work", project_root=Path(__file__).resolve().parent,
            source_resolver=lambda _job, _kind: {}, model_list=lambda: [], output_register=lambda _job: "",
        )
        project_id = "motion_" + "3" * 32
        snapshot = service._project_dir(project_id) / "source" / "input.mp4"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(b"pinned")
        old_metadata = {
            "duration_ms": 45102, "fps_num": 338250000, "fps_den": 11267329,
            "width": 720, "height": 1280,
        }
        project = service.repository.create_project({
            "id": project_id, "source_job_id": "parent", "source_fingerprint": "sha256:" + "a" * 64,
            "source_sha256": "a" * 64, "source_size_bytes": 6,
            "source_metadata": old_metadata, "status": "failed",
        })
        corrected = {
            "duration_ms": 45102, "fps_num": 30, "fps_den": 1, "fps": "30/1",
            "width": 720, "height": 1280,
        }
        with mock.patch("motion.service.verify_snapshot", return_value=corrected) as verify:
            updated = service._refresh_legacy_source_metadata(project)
        verify.assert_called_once_with(snapshot, "a" * 64, 6)
        self.assertEqual(updated["source_metadata"]["fps_num"], 30)
        manifest = json.loads((snapshot.parent.parent / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["source_metadata"]["fps"], "30/1")

    def test_safe_zone_check_uses_anchor_aware_placement_rect(self):
        service = MotionService(
            work_root=self.root / "montage_work", project_root=Path(__file__).resolve().parent,
            source_resolver=lambda _job, _kind: {}, model_list=lambda: [], output_register=lambda _job: "",
        )
        analysis = {
            "safe_zones": {"instagram_reels": {"x": 0.08, "y": 0.14, "w": 0.78, "h": 0.68}},
            "occupancy_map": [],
        }
        raw = valid_plan()
        raw["scenes"][0]["placement"] = {
            "x": 0.1, "y": 0.15, "w": 0.5, "h": 0.3, "anchor": "top-left",
        }
        service._validate_safe_zones(MotionPlan.model_validate(raw), analysis)
        raw["scenes"][0]["placement"]["y"] = 0.1
        with self.assertRaisesRegex(ValueError, "outside the social-platform safe zone"):
            service._validate_safe_zones(MotionPlan.model_validate(raw), analysis)

    def test_proposed_scene_is_nudged_clear_of_a_small_face_overlap(self):
        service = MotionService(
            work_root=self.root / "montage_work", project_root=Path(__file__).resolve().parent,
            source_resolver=lambda _job, _kind: {}, model_list=lambda: [], output_register=lambda _job: "",
        )
        raw = valid_plan()
        raw["duration_ms"] = 50_000
        raw["scenes"][0].update({"scene_id": "capability-subtitles", "start_ms": 14367, "end_ms": 16427})
        raw["scenes"][0]["placement"] = {
            "x": 0.5, "y": 0.22, "w": 0.58, "h": 0.07, "anchor": "center",
        }
        plan = MotionPlan.model_validate(raw)
        analysis = {
            "safe_zones": {"instagram_reels": {"x": 0.08, "y": 0.14, "w": 0.78, "h": 0.68}},
            "occupancy_map": [{
                "start_ms": 15301, "end_ms": 16801, "kind": "face",
                "box": {"x": 0.1953, "y": 0.246, "w": 0.6625, "h": 0.3726},
            }],
        }
        fitted = service._fit_plan_to_safe_zones(plan, analysis)
        placement = fitted.scenes[0].placement
        self.assertLess(placement.y, 0.22)
        service._validate_safe_zones(fitted, analysis)

    def test_create_project_pins_source_and_stores_analysis(self):
        from motion.models import CreateProjectInput
        source = self.root / "ready.mp4"
        source.write_bytes(b"ready montage")
        probe = {
            "duration_ms": 5000, "duration_seconds": 5.0, "fps_num": 30, "fps_den": 1,
            "fps": "30/1", "width": 1080, "height": 1920, "coded_width": 1080,
            "coded_height": 1920, "rotation": 0, "sample_aspect_ratio": "1:1",
            "display_aspect_ratio": "9:16", "video_codec": "h264", "audio_codec": "aac",
            "audio_present": True, "color": {"hdr": False},
        }
        service = MotionService(
            work_root=self.root / "montage_work",
            project_root=Path(__file__).resolve().parent,
            source_resolver=lambda _job, _kind: {
                "path": str(source), "source_kind": "master", "original_name": "ready.mp4",
                "srt_text": "00:00:00,000 --> 00:00:01,000\nПример", "transcript_segments": None,
                "params": {}, "result": {"timeline": {}},
            },
            model_list=lambda: [], output_register=lambda _job: "",
        )
        fake_analysis = {
            "schema_version": "motion-analysis.v1", "duration_ms": 5000,
            "fps": {"num": 30, "den": 1}, "frames": [], "scene_change_hints": [],
            "transcript": {"segments": [], "word_aligned": False, "timing_source": "srt"},
            "occupancy_map": [], "safe_zones": {},
        }
        try:
            with mock.patch("motion.source_snapshot.probe_video", return_value=probe), \
                 mock.patch("motion.service.analyze_source", return_value=fake_analysis):
                project = service.create_project(CreateProjectInput(source_job_id="parent"))
            snapshot = service.source_file(project["id"])
            self.assertTrue(snapshot.is_file())
            self.assertTrue(os.path.samefile(source, snapshot))
            self.assertEqual(project["status"], "created")
            self.assertEqual(project["source_kind"], "master")
        finally:
            asyncio.run(service.shutdown())

    def test_starts_fresh_session_instead_of_reloading_large_legacy_history(self):
        service = MotionService(
            work_root=self.root / "montage_work", project_root=Path(__file__).resolve().parent,
            source_resolver=lambda _job, _kind: {}, model_list=lambda: [], output_register=lambda _job: "",
        )
        model_id = "openai/gpt-6-luna"
        project = service.repository.create_project({
            "id": "motion_" + "1" * 32, "source_job_id": "parent",
            "source_fingerprint": "sha256:" + "a" * 64, "source_name": "video.mp4",
            "status": "failed", "session_id": "ses_legacy", "model_id": model_id,
            "model_history": [{"model_id": model_id, "at": "before"}],
        })
        with mock.patch.object(service.bridge, "messages") as get_messages, mock.patch.object(
            service.bridge, "create_session", return_value="ses_fresh",
        ) as create_session:
            updated = service._ensure_design_session(project, model_id)
        self.assertEqual(updated["session_id"], "ses_fresh")
        self.assertEqual(updated["session_history"][0]["session_id"], "ses_legacy")
        self.assertEqual(updated["session_history"][0]["reason"], "fresh_context_per_design_request")
        get_messages.assert_not_called()
        create_session.assert_called_once()

    def test_mirror_variant_renders_from_base_and_saves_on_existing_job(self):
        project_id = "motion_" + "e" * 32
        output_id = "mg_" + "f" * 32
        project_dir = self.root / "montage_work" / ".motion" / "projects" / project_id
        project_dir.mkdir(parents=True)
        job_dir = self.root / "montage_work" / output_id
        job_dir.mkdir(parents=True)
        final_video = job_dir / "final.mp4"
        final_video.write_bytes(b"original")
        saved_jobs = {}
        output_job = {
            "id": output_id, "origin": "motion_graphics_plus", "motion_project_id": project_id,
            "video_path": str(final_video), "variants": {},
        }
        saved_jobs[output_id] = output_job
        base_dir = self.root / "mirror_base"
        base_dir.mkdir()
        base_video = base_dir / "base.mp4"
        base_video.write_bytes(b"mirror base")
        probe = {"duration_ms": 5000, "duration_seconds": 5.0, "fps_num": 30, "fps_den": 1,
                 "width": 1080, "height": 1920, "color": {"hdr": False}}

        async def render_base(_project, _cancel, _progress):
            return {"video_path": str(base_video), "temporary_dir": str(base_dir)}

        def render_motion(_project_id, _revision, _source, _metadata, _plan, _preview,
                          _preview_duration, _cancel, _progress):
            render_dir = self.root / "rendered"
            render_dir.mkdir(exist_ok=True)
            rendered_video = render_dir / "motion.mp4"
            rendered_video.write_bytes(b"mirrored motion")
            return {"final_video": str(rendered_video), "output_dir": str(render_dir)}

        async def run():
            service = MotionService(
                work_root=self.root / "montage_work", project_root=Path(__file__).resolve().parent,
                source_resolver=lambda _job, _kind: {}, model_list=lambda: [], output_register=lambda _job: "",
                mirror_base_renderer=render_base,
                output_lookup=lambda job_id: saved_jobs.get(job_id),
                output_save=lambda job: saved_jobs.__setitem__(job["id"], job),
                mirror_signature=lambda _job: "signature-v1",
            )
            service.repository.create_project({
                "id": project_id, "status": "ready", "source_job_id": "parent",
                "source_fingerprint": "sha256:" + "a" * 64, "mirror_available": True,
                "current_revision": 1, "approved_revision": 1,
            })
            service.repository.create_revision(project_id, 1, valid_plan(), status="ready")
            service.repository.update_revision(project_id, 1, output_job_id=output_id)
            try:
                with mock.patch("motion.service.probe_video", return_value=probe), \
                     mock.patch.object(service.renderer, "render", side_effect=render_motion):
                    operation = await service.start_mirror(project_id, output_id, "mirror-request")
                    task = service._tasks[operation["id"]]
                    await task
                return service.repository.get_operation(operation["id"])
            finally:
                await service.shutdown()

        operation = asyncio.run(run())
        mirrored = saved_jobs[output_id]["variants"]["mirrored"]
        self.assertEqual(operation["status"], "completed")
        self.assertTrue(mirrored["ready"])
        self.assertEqual(mirrored["signature"], "signature-v1")
        self.assertTrue(Path(mirrored["path"]).is_file())
        self.assertEqual(Path(mirrored["path"]).read_bytes(), b"mirrored motion")


if __name__ == "__main__":
    unittest.main()
