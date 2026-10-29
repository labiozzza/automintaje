import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from motion.generative.sandbox import DockerSceneSandbox
from motion.generative.scene_renderer import GeneratedSceneRenderer
from motion.generative.workspace import SceneWorkspace, SceneWorkspaceError
from motion.source_snapshot import probe_video


class SceneWorkspaceTests(unittest.TestCase):
    def test_workspace_contains_only_brief_selected_skills_sdk_and_generated_area(self):
        project_root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as temp:
            workspace = SceneWorkspace.create(
                project_root, Path(temp), "motion_" + "a" * 32, "fixture-scene", 1,
                {"start_ms": 1000, "end_ms": 4500, "source": {"width": 720, "height": 1280}},
                ["video-graphics", "video-kinetic-typography", "video-data-motion"], [],
            )
            self.assertTrue((workspace.root / "scene-brief.json").is_file())
            self.assertTrue((workspace.generated_src / "SceneSDK.tsx").is_file())
            self.assertTrue((workspace.root / ".opencode" / "agents" / "motion-engineer.md").is_file())
            copied = sorted(p.name for p in (workspace.root / ".opencode" / "skills").iterdir())
            self.assertEqual(copied, ["video-data-motion", "video-graphics", "video-kinetic-typography"])
            self.assertFalse((workspace.root / "source" / "input.mp4").exists())

    def test_static_guard_rejects_network_and_dynamic_execution(self):
        project_root = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as temp:
            workspace = SceneWorkspace.create(
                project_root, Path(temp), "motion_" + "b" * 32, "fixture-scene", 1,
                {"start_ms": 1000, "end_ms": 4500}, ["video-graphics"], [],
            )
            source = workspace.generated_src / "Scene.tsx"
            source.write_text('export const GeneratedScene = () => fetch("https://example.com");', encoding="utf-8")
            with self.assertRaisesRegex(SceneWorkspaceError, "forbidden"):
                workspace.validate_generated_files()


@unittest.skipUnless(
    os.environ.get("AUTOMONTAGE_RUN_SCENE_SANDBOX") == "1"
    and shutil.which("docker"),
    "set AUTOMONTAGE_RUN_SCENE_SANDBOX=1 to run the isolated Docker scene proof",
)
class DockerScenePhase1SmokeTests(unittest.TestCase):
    def test_original_tsx_builds_in_networkless_runner_and_previews_on_source(self):
        project_root = Path(__file__).resolve().parent
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        with tempfile.TemporaryDirectory(dir=project_root / "montage_work") as temp:
            root = Path(temp)
            source = root / "master.mp4"
            subprocess.run(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                 "-f", "lavfi", "-i", "color=c=0x202830:s=360x640:r=24:d=2",
                 "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                 "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", str(source)],
                check=True, capture_output=True, timeout=90,
            )
            workspace = SceneWorkspace.create(
                project_root, root / "montage_work", "motion_" + "c" * 32, "fixture-scene", 1,
                {"start_ms": 0, "end_ms": 1500, "source": {"width": 360, "height": 640}},
                ["video-kinetic-typography"], [],
            )
            (workspace.generated_src / "Scene.tsx").write_text(
                """import React from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame} from 'remotion';
import {SceneCanvas} from './SceneSDK';
export const GeneratedScene: React.FC = () => {
  const frame = useCurrentFrame();
  const x = interpolate(frame, [0, 24, 60], [-80, 80, 220], {extrapolateRight: 'clamp'});
  const opacity = interpolate(frame, [0, 10, 30], [0, 1, 1], {extrapolateRight: 'clamp'});
  return <SceneCanvas><AbsoluteFill style={{alignItems:'center',justifyContent:'center'}}>
    <div style={{transform:`translateX(${x}px)`,opacity,color:'#f4c95d',fontSize:56,fontWeight:900,fontFamily:'Arial'}}>
      PROCESS → RESULT
    </div>
  </AbsoluteFill></SceneCanvas>;
};
""", encoding="utf-8",
            )
            source_info = probe_video(source)
            sandbox = DockerSceneSandbox(project_root)
            renderer = GeneratedSceneRenderer(project_root, sandbox)
            result = renderer.render(
                workspace, source, 200, 1700, 360, 640, 24, 1,
            )
            self.assertEqual(result["status"], "ready_for_review", repr(result["qa"]))
            self.assertTrue(Path(result["preview_path"]).is_file())
            self.assertEqual(result["qa"]["status"], "pass")


if __name__ == "__main__":
    unittest.main()
