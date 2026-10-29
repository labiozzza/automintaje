"""Bounded end-to-end Motion Engineer proof: original TSX, sandbox, preview and QA."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .engineer import MotionEngineer
from .sandbox import DockerSceneSandbox
from .scene_renderer import GeneratedSceneRenderer
from .visual_qa import MotionVisualQA
from .workspace import SceneWorkspace


async def generate_reviewable_scene(project_root: Path, work_root: Path,
                                    project_id: str, scene_id: str, scene_revision: int,
                                    model_id: str, brief: Dict[str, Any],
                                    source_video: Path, reference_frames: Iterable[Path],
                                    cancel_event: Optional[threading.Event] = None,
                                    progress_cb=None, max_attempts: int = 3) -> dict:
    """Generate one scene. The returned status is never auto-approved."""
    cancel_event = cancel_event or threading.Event()
    root = Path(project_root).resolve()
    work = Path(work_root).resolve()
    selected_skills = list(brief.get("selected_skills") or [])[:3]
    workspace = SceneWorkspace.create(
        root, work, project_id, scene_id, scene_revision, brief, selected_skills, reference_frames,
    )
    sandbox = DockerSceneSandbox(root)
    if not sandbox.capabilities().get("available"):
        if progress_cb:
            progress_cb("runner_build", 3, "Сборка закреплённого sandbox runner по Remotion lockfile")
        sandbox.build_image()
    engineer = MotionEngineer(root, workspace.root / ".opencode-state")
    renderer = GeneratedSceneRenderer(root, sandbox)
    visual_qa = MotionVisualQA()
    source_meta = brief.get("source") or {}
    attempts = []
    feedback = ""
    last = None
    for attempt in range(max(1, min(3, int(max_attempts)))):
        if cancel_event.is_set():
            raise RuntimeError("scene generation cancelled")
        codegen = await engineer.generate(
            workspace, model_id, cancel_event,
            progress_cb=lambda stage, progress, detail: progress_cb(stage, progress, detail) if progress_cb else None,
            comment=feedback,
        )
        if progress_cb:
            progress_cb("scene_build", 35, "TypeScript build и isolated Remotion render")
        render = renderer.render(
            workspace, Path(str(source_video)),
            int(brief["start_ms"]), int(brief["end_ms"]),
            int(source_meta["width"]), int(source_meta["height"]),
            int(source_meta["fps_num"]), int(source_meta["fps_den"]),
        )
        machine_qa = render["qa"]
        if machine_qa.get("status") != "pass":
            feedback = "Machine QA blocking issues: " + json.dumps(machine_qa.get("blocking_issues"), ensure_ascii=False)[:2000]
            attempts.append({"attempt": attempt + 1, "code_sha256": codegen["source_sha256"], "machine_qa": machine_qa, "status": "revise"})
            if attempt + 1 < max(1, min(3, int(max_attempts))):
                continue
            last = {**render, "engineer": codegen, "status": "needs_manual_review"}
            break

        visual = await visual_qa.review(
            workspace, model_id, machine_qa, comment=feedback,
        )
        attempts.append({
            "attempt": attempt + 1,
            "code_sha256": codegen["source_sha256"],
            "machine_qa": machine_qa,
            "visual_qa": visual,
            "status": visual.get("status"),
        })
        feedback = json.dumps({
            "blocking_issues": visual.get("blocking_issues") or [],
            "creative_notes": visual.get("creative_notes") or [],
        }, ensure_ascii=False)
        last = {**render, "engineer": codegen, "visual_qa": visual,
                "status": "ready_for_review" if visual.get("status") == "pass" else "needs_manual_review"}
        if visual.get("status") in {"pass", "blocked"}:
            break

    if last is None:
        raise RuntimeError("scene generation ended without QA result")
    manifest_path = workspace.root / "scene-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({
        "status": last["status"],
        "review_status": "proposed",
        "engineer_model": model_id,
        "source_video": str(Path(source_video).resolve()),
        "attempts": attempts,
        "visual_qa": last.get("visual_qa"),
        "approved_at": None,
    })
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if progress_cb:
        progress_cb("ready_for_review", 99, "Реальный scene preview готов к согласованию")
    return {**last, "workspace": str(workspace.root), "manifest_path": str(manifest_path), "manifest": manifest}
