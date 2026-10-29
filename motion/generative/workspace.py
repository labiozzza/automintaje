"""Per-scene immutable workspace construction with strict path containment."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List

from motion.skills.registry import load_registry


class SceneWorkspaceError(RuntimeError):
    pass


@dataclass(frozen=True)
class SceneWorkspace:
    root: Path
    project_id: str
    scene_id: str
    revision: int

    @property
    def brief_path(self) -> Path:
        return self.root / "scene-brief.json"

    @property
    def generated_src(self) -> Path:
        return self.root / "generated" / "src"

    @property
    def generated_assets(self) -> Path:
        return self.root / "generated" / "assets"

    @property
    def output_dir(self) -> Path:
        return self.root / "output"

    @property
    def qa_dir(self) -> Path:
        return self.root / "qa"

    @classmethod
    def create(cls, project_root: Path, work_root: Path, project_id: str,
               scene_id: str, revision: int, brief: Dict[str, Any],
               skill_ids: Iterable[str], reference_frames: Iterable[Path]) -> "SceneWorkspace":
        if not re.fullmatch(r"motion_[a-f0-9]{32}", str(project_id)):
            raise SceneWorkspaceError("invalid motion project id")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(scene_id)):
            raise SceneWorkspaceError("invalid scene id")
        if int(revision) < 1:
            raise SceneWorkspaceError("invalid scene revision")

        root_dir = Path(work_root).resolve() / ".motion" / "projects" / str(project_id)
        scene_dir = root_dir / "scenes" / str(scene_id) / ("%04d" % int(revision))
        try:
            root_dir.resolve().relative_to((Path(work_root).resolve() / ".motion" / "projects").resolve())
        except ValueError as exc:
            raise SceneWorkspaceError("scene workspace escaped project storage") from exc
        if root_dir.is_symlink() or scene_dir.is_symlink() or scene_dir.exists():
            raise SceneWorkspaceError("scene revision workspace already exists or is a symlink")
        scene_dir.mkdir(parents=True, exist_ok=False)
        workspace = cls(scene_dir.resolve(), str(project_id), str(scene_id), int(revision))
        workspace.generated_src.mkdir(parents=True)
        workspace.generated_assets.mkdir(parents=True)
        workspace.output_dir.mkdir(parents=True)
        workspace.qa_dir.mkdir(parents=True)
        workspace.output_dir.chmod(0o777)  # Docker runner uses an unprivileged UID.

        registry = load_registry(Path(project_root))
        skills_by_id = {item["id"]: item for item in registry.get("skills") or []}
        selected = []
        for skill_id in list(skill_ids)[:4]:
            item = skills_by_id.get(str(skill_id))
            if not item or item.get("integrity") != "verified" or item.get("status") not in {"implemented", "partial"}:
                continue
            skill_root = scene_dir / ".opencode" / "skills" / item["id"]
            skill_root.mkdir(parents=True, exist_ok=True)
            source = Path(project_root) / item["skill_file"]
            destination = skill_root / "SKILL.md"
            shutil.copyfile(source, destination)
            storyboard = item.get("storyboard_file")
            if storyboard:
                source_image = Path(project_root) / storyboard
                if source_image.is_file() and not source_image.is_symlink():
                    shutil.copyfile(source_image, skill_root / "storyboard.png")
            selected.append(item)

        refs_dir = scene_dir / "references"
        refs_dir.mkdir()
        copied_frames: List[str] = []
        for index, frame in enumerate(list(reference_frames)[:4]):
            frame_path = Path(frame)
            if not frame_path.is_file() or frame_path.is_symlink() or frame_path.stat().st_size > 2 * 1024 * 1024:
                continue
            name = "frame_%02d%s" % (index + 1, frame_path.suffix.lower())
            shutil.copyfile(frame_path, refs_dir / name)
            copied_frames.append("references/" + name)

        safe_brief = dict(brief)
        safe_brief.update({
            "project_id": project_id,
            "scene_id": scene_id,
            "revision": int(revision),
            "selected_skills": [item["id"] for item in selected],
            "reference_frames": copied_frames,
            "workspace_mode": "isolated-scene-only",
        })
        workspace.brief_path.write_text(json.dumps(safe_brief, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        background_rule = (
            "For scene_takeover, an authored full-frame background may be opaque; keep visual titles in platform-safe bounds.\n"
            if safe_brief.get("mode") == "scene_takeover" else
            "For overlay, keep background pixels transparent (alpha 0) and avoid protected source regions.\n"
        )
        (scene_dir / "sdk-api.md").write_text(
            "# Generated Remotion Scene SDK v1\n\n"
            "Export `GeneratedScene` from `generated/src/Scene.tsx`. It is mounted at the scene's local frame 0.\n"
            "Use only `react`, `remotion`, and `./SceneSDK`; no network, filesystem, dynamic imports, or runtime randomness.\n"
            "Available: `SceneSDKProps`, `SceneCanvas`, `StaggeredWords`, `useSceneClock`, `progress`, `AbsoluteFill`, `interpolate`, `Easing`, `spring`, `useCurrentFrame`, `useVideoConfig`.\n"
            + background_rule
            + "Keep all content inside the provided normalized safe layout.\n",
            encoding="utf-8",
        )
        sdk_source = Path(project_root) / "motion" / "generative" / "sdk" / "SceneSDK.tsx"
        if not sdk_source.is_file() or sdk_source.is_symlink():
            raise SceneWorkspaceError("trusted scene SDK is unavailable")
        shutil.copyfile(sdk_source, workspace.generated_src / "SceneSDK.tsx")
        agent_source = Path(project_root) / ".opencode" / "agents" / "motion-engineer.md"
        qa_agent_source = Path(project_root) / ".opencode" / "agents" / "motion-visual-qa.md"
        if not agent_source.is_file() or agent_source.is_symlink():
            raise SceneWorkspaceError("Motion Engineer agent definition is unavailable")
        if not qa_agent_source.is_file() or qa_agent_source.is_symlink():
            raise SceneWorkspaceError("Motion Visual QA agent definition is unavailable")
        agent_dir = scene_dir / ".opencode" / "agents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(agent_source, agent_dir / "motion-engineer.md")
        shutil.copyfile(qa_agent_source, agent_dir / "motion-visual-qa.md")
        return workspace

    def validate_generated_files(self) -> Path:
        allowed_root = (self.root / "generated").resolve()
        source = self.generated_src / "Scene.tsx"
        if source.is_symlink() or not source.is_file():
            raise SceneWorkspaceError("Motion Engineer did not write generated/src/Scene.tsx")
        if source.stat().st_size > 96 * 1024:
            raise SceneWorkspaceError("generated scene source exceeds the 96 KiB limit")
        trusted_opencode_modules = (self.root / ".opencode" / "node_modules").resolve()
        for path in self.root.rglob("*"):
            if path.is_symlink():
                relative = path.relative_to(self.root)
                resolved = path.resolve()
                if relative.parts[:2] == (".opencode", "node_modules"):
                    try:
                        resolved.relative_to(trusted_opencode_modules)
                        continue
                    except ValueError:
                        pass
                raise SceneWorkspaceError("symlink outside OpenCode's confined dependency cache is forbidden")
        try:
            source.resolve().relative_to(allowed_root)
        except ValueError as exc:
            raise SceneWorkspaceError("generated scene escaped its workspace") from exc
        text = source.read_text(encoding="utf-8")
        if not re.search(r"export\s+(?:const|function|class)\s+GeneratedScene\b", text):
            raise SceneWorkspaceError("generated scene must export GeneratedScene")
        code_only = re.sub(r"/\*[\s\S]*?\*/|//[^\n]*", "", text)
        code_only = re.sub(r"`(?:\\.|[^`])*`|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'", "", code_only)
        if re.search(r"(?i)\b(eval\s*\(|new\s+Function|require\s*\(|process\s*\.|child_process|fetch\s*\(|XMLHttpRequest|WebSocket|import\s*\(|node:)", code_only):
            raise SceneWorkspaceError("generated scene contains a forbidden runtime or network API")
        imports = re.findall(r"(?m)^\s*import\s+(?:[^;]+?\s+from\s+)?[\"']([^\"']+)[\"']", text)
        if any(item not in {"react", "remotion", "./SceneSDK"} and not item.startswith(("./assets/", "../assets/")) for item in imports):
            raise SceneWorkspaceError("generated scene imports an unapproved package")
        for asset in self.generated_assets.rglob("*"):
            if asset.is_file() and (asset.is_symlink() or asset.stat().st_size > 12 * 1024 * 1024):
                raise SceneWorkspaceError("generated asset is linked or exceeds 12 MiB")
        return source
