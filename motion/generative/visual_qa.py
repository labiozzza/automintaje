"""Bounded, read-only OpenCode visual review of rendered scene samples."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict

from motion.opencode_bridge import OpenCodeBridge, assistant_text

from .workspace import SceneWorkspace


class VisualQAError(RuntimeError):
    pass


class MotionVisualQA:
    async def review(self, workspace: SceneWorkspace, model_id: str,
                     machine_report: Dict[str, Any], comment: str = "") -> dict:
        bridge = OpenCodeBridge(workspace.root, workspace.root / ".opencode-state-qa")
        await asyncio.to_thread(bridge.start)
        try:
            session_id = await asyncio.to_thread(
                bridge.create_session,
                "Motion Visual QA · %s · r%d" % (workspace.scene_id, workspace.revision),
                model_id, "motion-visual-qa", workspace.root,
            )
            before = await asyncio.to_thread(bridge.messages, session_id, 2, workspace.root)
            before_ids = {str((item.get("info") or {}).get("id") or "") for item in before}
            contact = workspace.qa_dir / "contact-sheet.jpg"
            if not contact.is_file():
                raise VisualQAError("visual QA contact sheet is missing")
            prompt = (
                "Review the actual scene render, not the TSX. Return only JSON with keys "
                "status (pass|revise|blocked), blocking_issues (list of {code,frame,summary}), "
                "creative_notes (list of strings), next_action (accept|revise_scene|manual_review).\n\n"
                "Scene brief:\n%s\n\nMachine QA:\n%s\n\nUser note:\n%s"
                % (
                    workspace.brief_path.read_text(encoding="utf-8")[:12000],
                    json.dumps(machine_report, ensure_ascii=False)[:12000],
                    str(comment or "")[:1500],
                )
            )
            parts = [
                {"type": "text", "text": prompt + "\nQA media: contact sheet plus five source-composite samples in timeline order; machine report includes temporal frame-difference metrics."},
                {"type": "file", "mime": "image/jpeg", "filename": "source-composite-contact-sheet.jpg", "url": contact.resolve().as_uri()},
            ]
            for frame in sorted((workspace.qa_dir / "composite-frames").glob("frame_*.png")):
                if frame.is_file() and not frame.is_symlink() and frame.stat().st_size <= 3 * 1024 * 1024:
                    parts.extend((
                        {"type": "text", "text": "Source-composite sample frame: " + frame.name},
                        {"type": "file", "mime": "image/png", "filename": frame.name, "url": frame.resolve().as_uri()},
                    ))
            message_id = "msg_" + uuid.uuid4().hex
            await asyncio.to_thread(
                bridge.prompt_async, session_id, model_id, parts,
                message_id=message_id, agent="motion-visual-qa", directory=workspace.root,
            )
            deadline = time.monotonic() + 240
            empty_idle_polls = 0
            while time.monotonic() < deadline:
                status = await asyncio.to_thread(bridge.session_status, session_id)
                if str((status or {}).get("type") or "idle") != "idle":
                    await asyncio.sleep(0.8)
                    continue
                messages = await asyncio.to_thread(bridge.messages, session_id, 4, workspace.root)
                for message in reversed(messages):
                    info = message.get("info") or {}
                    if info.get("role") != "assistant" or str(info.get("id") or "") in before_ids:
                        continue
                    text = assistant_text(message).strip()
                    if not text:
                        empty_idle_polls += 1
                        break
                    try:
                        value = json.loads(text)
                    except json.JSONDecodeError:
                        match = re.search(r"\{[\s\S]*\}", text)
                        try:
                            value = json.loads(match.group(0)) if match else None
                        except Exception:
                            value = None
                    if not isinstance(value, dict):
                        raise VisualQAError("Visual QA returned invalid JSON")
                    if value.get("status") not in {"pass", "revise", "blocked"}:
                        raise VisualQAError("Visual QA returned an unknown status")
                    return value
                else:
                    empty_idle_polls = 0
                if empty_idle_polls >= 4:
                    raise VisualQAError("Visual QA completed without a JSON report")
                await asyncio.sleep(0.8)
            await asyncio.to_thread(bridge.abort, session_id)
            raise VisualQAError("Visual QA exceeded its 4-minute limit")
        finally:
            await asyncio.to_thread(bridge.stop)
