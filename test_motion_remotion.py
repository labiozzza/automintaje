import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from motion.renderer import MotionRenderer
from motion.scene_graph_v2 import GraphLayer, GraphRect, MotionPlanV2, MotionTrack, SceneGraph
from motion.source_snapshot import probe_video


@unittest.skipUnless(
    os.environ.get("AUTOMONTAGE_RUN_REMOTION_SMOKE") == "1"
    and (Path(__file__).parent / "motion" / "assets" / "remotion" / "node_modules" / ".bin" / "remotion").is_file(),
    "set AUTOMONTAGE_RUN_REMOTION_SMOKE=1 after installing the Remotion renderer",
)
class RemotionAlphaSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_transparent_overlay_composites_to_a_publishable_video(self):
        source = self.root / "master.mp4"
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "color=c=0x202830:s=360x640:r=24:d=1.5",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.5",
             "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709",
             "-colorspace", "bt709", "-color_range", "tv", "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", str(source)],
            capture_output=True, text=True, timeout=90, check=True,
        )
        source_info = probe_video(source)
        plan = {
            "profile": "minimal",
            "scenes": [{
                "scene_id": "smoke01", "start_ms": 150, "end_ms": min(1300, source_info["duration_ms"] - 1),
                "review_status": "accepted", "spoken_evidence": "Motion smoke",
                "semantic_goal": "Visible layer with transparency", "effect_type": "animated_text",
                "content": {"text": "Motion smoke", "profile": "minimal"},
                "placement": {"x": 0.5, "y": 0.34, "w": 0.72, "h": 0.22, "anchor": "center"},
                "animation": {"preset": "fade", "enter_ms": 120, "exit_ms": 100, "delay_ms": 0},
            }],
        }
        renderer = MotionRenderer(Path(__file__).parent, self.root / "montage_work")
        project_id = "motion_" + uuid.uuid4().hex
        result = renderer.render(
            project_id, 1, source, source_info, plan, False, None,
            threading.Event(), lambda _progress, _detail: None,
        )
        self.assertTrue(Path(result["final_video"]).is_file())
        self.assertTrue(Path(result["master_video"]).is_file())
        self.assertTrue(Path(result["preview_video"]).is_file())
        self.assertTrue(result["quality"]["verified"])
        final_info = probe_video(Path(result["final_video"]))
        self.assertTrue(final_info["audio_present"])
        self.assertEqual((final_info["width"], final_info["height"]), (360, 640))
        preview = renderer.render(
            project_id, 1, source, source_info, plan, True, 1000,
            threading.Event(), lambda _progress, _detail: None,
        )
        self.assertTrue(Path(preview["preview_video"]).is_file())

    def test_v2_kinetic_graph_renders_internal_text_and_path_motion(self):
        source = self.root / "master-v2.mp4"
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "color=c=0x202830:s=360x640:r=24:d=1.5",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.5",
             "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709",
             "-colorspace", "bt709", "-color_range", "tv", "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", str(source)],
            capture_output=True, text=True, timeout=90, check=True,
        )
        source_info = probe_video(source)
        graph = MotionPlanV2(
            source_job_id="v2-smoke", source_fingerprint="sha256:" + "a" * 64,
            revision=1, duration_ms=1500,
            output={"width": 360, "height": 640, "fps_num": 24, "fps_den": 1},
            scenes=[SceneGraph(
                scene_id="kinetic-smoke", start_ms=100, end_ms=1350,
                semantic_goal="Show internal state changes over time.",
                spoken_evidence="motion graph smoke",
                placement={"x": 0.5, "y": 0.35, "w": 0.78, "h": 0.22, "anchor": "center"},
                skills_used=["video-kinetic-typography"],
                layers=[
                    GraphLayer(id="title", kind="text", text="MOTION", color="ink", font_size_px=60,
                               geometry=GraphRect(x=0.1, y=0.2, w=0.8, h=0.45)),
                    GraphLayer(id="underline", kind="line", shape="underline", color="accent",
                               geometry=GraphRect(x=0.1, y=0.78, w=0.8, h=0.1)),
                ],
                tracks=[
                    MotionTrack(layer_id="title", property="opacity", keyframes=[{"at_ms": 0, "value": 0}, {"at_ms": 320, "value": 1}]),
                    MotionTrack(layer_id="underline", property="draw_progress", keyframes=[{"at_ms": 120, "value": 0}, {"at_ms": 680, "value": 1}]),
                ],
            )],
        )
        plan = graph.compile_legacy_plan().model_dump(mode="json")
        renderer = MotionRenderer(Path(__file__).parent, self.root / "montage_work")
        result = renderer.render(
            "motion_v2_" + uuid.uuid4().hex, 1, source, source_info, plan,
            False, None, threading.Event(), lambda _progress, _detail: None,
        )
        self.assertTrue(result["quality"]["verified"])
        self.assertTrue(Path(result["final_video"]).is_file())
        self.assertTrue(probe_video(Path(result["final_video"]))["audio_present"])


if __name__ == "__main__":
    unittest.main()
