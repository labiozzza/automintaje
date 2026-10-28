"""Build a compatible immutable montage job for an approved Motion render."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def build_output_job(project: dict, revision: dict, rendered: dict) -> dict:
    output_job_id = str(rendered["job_id"])
    final_path = Path(str(rendered["final_video"])).resolve()
    master_path = Path(str(rendered["master_video"])).resolve()
    preview_path = Path(str(rendered["preview_video"])).resolve()
    if not final_path.is_file() or not master_path.is_file() or not preview_path.is_file():
        raise ValueError("Motion render is missing a validated video artifact")
    plan = revision.get("plan") or {}
    created = datetime.now().isoformat()
    return {
        "id": output_job_id,
        "original_name": "%s · Motion v%d.mp4" % (
            str(project.get("source_name") or "video"), int(revision.get("revision") or 1),
        ),
        "video_path": str(final_path),
        "srt_text": str(project.get("srt_text") or ""),
        "transcript_job_id": None,
        "project_id": None,
        "kind": "montage",
        "origin": "motion_graphics_plus",
        "parent_job_id": str(project.get("source_job_id") or ""),
        "motion_project_id": str(project.get("id") or ""),
        "motion_revision": int(revision.get("revision") or 1),
        "status": "done",
        "phase": "ready",
        "warnings": [],
        "progress": 100,
        "stage": "done",
        "detail": "Motion-версия готова к публикации",
        "error": None,
        "error_info": None,
        "created_at": created,
        "result": {
            "job_id": output_job_id,
            "result_video": str(final_path),
            "preview_video": str(preview_path),
            "master_video": master_path.name,
            "master_mode": "copy",
            "duration": float(rendered.get("duration_seconds") or 0),
            "subtitle_mode": "motion",
            "camera_mode": "off",
            "timeline": {
                "motion_project_id": str(project.get("id") or ""),
                "motion_revision": int(revision.get("revision") or 1),
                "motion_plan": plan,
            },
        },
        "params": {
            "motion_graphics_plus": True,
            "motion_project_id": str(project.get("id") or ""),
            "motion_revision": int(revision.get("revision") or 1),
            "mirror_horizontal": False,
            "prepare_mirrored": False,
        },
        "publish": None,
        "publish_errors": {},
        "publish_unknown": {},
        "publish_status": {},
        "publish_progress": {},
        "publish_started": {},
        "last_publish_targets": [],
        "covers": [],
        "selected_cover_id": "",
        "cover_status": "idle",
        "cover_progress": 0,
        "cover_detail": "",
        "cover_error": None,
        "cover_title": "",
        "operation": None,
        "variants": {},
        "mirror_variant": None,
        "publish_prep": {},
        "prepare_operation": None,
        "prepare_status": "idle",
        "prepare_error": None,
        "cancel_requested": False,
    }
