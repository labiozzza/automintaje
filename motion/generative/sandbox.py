"""Resource-limited, networkless Docker runner for generated Remotion scenes."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Optional

from .workspace import SceneWorkspace, SceneWorkspaceError


class SceneSandboxError(RuntimeError):
    pass


class DockerSceneSandbox:
    def __init__(self, project_root: Path, image: str = "automontage-motion-scene-runner:node22-remotion40539",
                 memory: str = "2g", cpus: str = "1.5", max_pids: int = 256,
                 timeout_seconds: int = 360):
        self.project_root = Path(project_root).resolve()
        self.remotion_root = self.project_root / "motion" / "assets" / "remotion"
        self.dockerfile = self.remotion_root / "Dockerfile.scene-runner"
        self.image = image
        self.memory = memory
        self.cpus = str(cpus)
        self.max_pids = max(64, min(384, int(max_pids)))
        self.timeout_seconds = max(60, min(900, int(timeout_seconds)))
        self.docker = shutil.which("docker")

    def capabilities(self) -> dict:
        if not self.docker or not self.dockerfile.is_file():
            return {"available": False, "reason": "docker_or_runner_image_definition_unavailable"}
        result = subprocess.run([self.docker, "image", "inspect", self.image], capture_output=True, timeout=15)
        return {
            "available": result.returncode == 0,
            "image": self.image,
            "network": "none",
            "memory": self.memory,
            "cpus": self.cpus,
            "max_pids": self.max_pids,
            "reason": "" if result.returncode == 0 else "runner_image_not_built",
        }

    def build_image(self, timeout_seconds: int = 900) -> dict:
        if not self.docker or not self.dockerfile.is_file():
            raise SceneSandboxError("Docker scene runner is unavailable")
        command = [
            self.docker, "build", "--pull", "-f", str(self.dockerfile), "-t", self.image,
            str(self.remotion_root),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=min(1800, int(timeout_seconds)))
        if result.returncode:
            raise SceneSandboxError("trusted runner image build failed: " + (result.stderr or result.stdout)[-2000:])
        inspect = subprocess.run([self.docker, "image", "inspect", self.image], capture_output=True, text=True, timeout=15)
        return {"image": self.image, "build_log_tail": (result.stdout or "")[-1500:], "inspect_ok": inspect.returncode == 0}

    def _entrypoint(self, width: int, height: int, fps_num: int, fps_den: int,
                    duration_frames: int) -> str:
        fps = float(fps_num) / float(fps_den)
        return """import React from 'react';
import {Composition, registerRoot} from 'remotion';
import {GeneratedScene} from './src/Scene';
const Root: React.FC = () => <Composition id=\"GeneratedScene\" component={GeneratedScene}
  durationInFrames={%(frames)d} fps={%(fps).10f} width={%(width)d} height={%(height)d}
  defaultProps={{width:%(width)d,height:%(height)d,fps:%(fps).10f,durationInFrames:%(frames)d}} />;
registerRoot(Root);
""" % {"frames": int(duration_frames), "fps": fps, "width": int(width), "height": int(height)}

    def render(self, workspace: SceneWorkspace, width: int, height: int,
               fps_num: int, fps_den: int, duration_ms: int) -> dict:
        if not self.docker or not self.capabilities().get("available"):
            raise SceneSandboxError("Docker scene runner image is not available")
        source = workspace.validate_generated_files()
        width, height, fps_num, fps_den = int(width), int(height), int(fps_num), int(fps_den)
        duration_frames = max(1, int(round(int(duration_ms) * fps_num / (1000.0 * fps_den))))
        entrypoint = workspace.root / "generated" / "entrypoint.tsx"
        entrypoint.write_text(self._entrypoint(width, height, fps_num, fps_den, duration_frames), encoding="utf-8")
        output = workspace.output_dir / "scene.mov"
        if output.exists():
            output.unlink()
        container_name = "motion-scene-" + re.sub(r"[^a-z0-9-]", "-", workspace.scene_id.lower())[:32] + "-" + str(os.getpid())
        project_mounts = [
            (workspace.generated_src, "/workspace/src", "ro"),
            (workspace.generated_assets, "/workspace/assets", "ro"),
            (entrypoint, "/workspace/entrypoint.tsx", "ro"),
            (workspace.output_dir, "/workspace/output", "rw"),
        ]
        for host_path, _container_path, _mode in project_mounts:
            if host_path.is_symlink() or not host_path.exists():
                raise SceneWorkspaceError("sandbox mount source is missing or symlinked")
        command = [
            self.docker, "run", "--rm", "--name", container_name,
            "--network", "none", "--memory", self.memory, "--cpus", self.cpus,
            "--pids-limit", str(self.max_pids), "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", "--shm-size", "256m",
            "--tmpfs", "/tmp:rw,nosuid,size=384m",
            "--user", "1000:1000", "--workdir", "/workspace",
            "--env", "HOME=/tmp/home", "--env", "TMPDIR=/tmp",
            "--env", "XDG_CACHE_HOME=/tmp/cache", "--env", "CHROME_PATH=/usr/bin/chromium",
        ]
        for host_path, container_path, mode in project_mounts:
            access = ",readonly" if mode == "ro" else ""
            command.extend(("--mount", "type=bind,src=%s,dst=%s%s" % (host_path.resolve(), container_path, access)))
        command.extend((
            self.image, "/bin/sh", "-lc",
            "mkdir -p /tmp/home /tmp/cache && "
            "/opt/remotion/node_modules/.bin/tsc --noEmit --skipLibCheck --strict --target ES2022 "
            "--module ESNext --moduleResolution bundler --jsx react-jsx --allowSyntheticDefaultImports "
            "/workspace/src/Scene.tsx /workspace/src/SceneSDK.tsx /workspace/entrypoint.tsx && "
             "/opt/remotion/node_modules/.bin/remotion render /workspace/entrypoint.tsx GeneratedScene "
             "/workspace/output/scene.mov --codec prores --prores-profile 4444 --pixel-format yuva444p10le "
             "--image-format png --browser-executable /usr/bin/chromium --concurrency 1 --log error --overwrite "
             "--no-bundle-cache --disable-ask-ai "
            "--width %d --height %d --fps %.10f --duration %d --timeout 120000" % (
                width, height, float(fps_num) / fps_den, duration_frames,
            ),
        ))
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            subprocess.run([self.docker, "kill", container_name], capture_output=True, timeout=10)
            subprocess.run([self.docker, "rm", "-f", container_name], capture_output=True, timeout=10)
            raise SceneSandboxError("generated scene sandbox timed out") from exc
        if result.returncode:
            raise SceneSandboxError("generated scene sandbox failed: " + (result.stderr or result.stdout)[-2500:])
        if not output.is_file() or output.stat().st_size <= 0:
            raise SceneSandboxError("generated scene render did not produce an output file")
        return {"overlay_path": str(output.resolve()), "duration_frames": duration_frames,
                "stdout_tail": (result.stdout or "")[-1000:], "stderr_tail": (result.stderr or "")[-1000:]}
