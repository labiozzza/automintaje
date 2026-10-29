"""Integrity-checked Belyak skill catalog; reference skills never imply executors."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


IMPLEMENTED: Dict[str, Dict[str, Any]] = {
    "video-kinetic-typography": {
        "status": "partial", "modes": ["overlay"],
        "executors": ["kinetic_sequence"],
        "limits": ["Fixed-timeline scenes only; no full-scene typography takeover yet."],
    },
    "video-graphics": {
        "status": "partial", "modes": ["overlay"],
        "executors": ["kinetic_sequence", "process_flow", "shape", "data_chart"],
        "limits": ["Data charts require transcript- or user-confirmed source values."],
    },
    "video-montage": {
        "status": "partial", "modes": ["overlay"],
        "executors": ["kinetic_sequence", "callout", "process_flow"],
        "limits": ["Uses Remotion deterministic frame time; source cuts remain immutable."],
    },
    "video-studio": {
        "status": "partial", "modes": ["overlay"],
        "executors": ["motion_director", "scene_graph_v2"],
        "limits": ["No generated voice or external B-roll in this release."],
    },
    "video-vox-explainer": {
        "status": "partial", "modes": ["overlay"],
        "executors": ["process_flow", "kinetic_sequence"],
        "limits": ["Map geometry and unsupported external sources are reference-only."],
    },
    "video-whiteboard": {
        "status": "partial", "modes": ["overlay"],
        "executors": ["kinetic_sequence", "line_draw"],
        "limits": ["Only supported vector primitives are rendered; no freehand image tracing."],
    },
    "video-data-motion": {
        "status": "partial", "modes": ["overlay"],
        "executors": ["data_chart", "counter"],
        "limits": ["Values must be evidenced; time-series choreography is not implemented."],
    },
}

ROUTES = {
    "kinetic": ["video-kinetic-typography", "video-graphics", "video-montage", "video-studio"],
    "process": ["video-vox-explainer", "video-whiteboard", "video-graphics", "video-studio"],
    "data": ["video-data-motion", "video-graphics", "video-montage", "video-studio"],
    "default": ["video-studio", "video-montage", "video-graphics"],
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _metadata(skill_path: Path) -> Dict[str, str]:
    text = skill_path.read_text(encoding="utf-8")
    match = re.match(r"(?s)^---\s*\n(.*?)\n---\s*\n", text)
    if not match:
        return {"name": skill_path.parent.name, "description": "", "frontmatter_valid": "false"}
    fields = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip().strip("\"'")
    return {
        "name": fields.get("name", ""),
        "description": fields.get("description", ""),
        "frontmatter_valid": str(bool(fields.get("name") and fields.get("description"))).lower(),
    }


def build_lock(project_root: Path, archive_path: Optional[Path] = None) -> dict:
    root = Path(project_root).resolve()
    skills_root = root / ".opencode" / "skills"
    skills = []
    for skill_path in sorted(skills_root.glob("*/SKILL.md")):
        skill_id = skill_path.parent.name
        metadata = _metadata(skill_path)
        implementation = IMPLEMENTED.get(skill_id, {
            "status": "reference_only", "modes": [], "executors": [],
            "limits": ["Methodology and storyboard reference only; no production executor registered."],
        })
        storyboard = skill_path.parent / "storyboard.png"
        scripts = sorted(str(path.relative_to(root)) for path in skill_path.parent.rglob("*") if path.is_file() and path != skill_path and path != storyboard)
        skills.append({
            "id": skill_id,
            "name": metadata["name"],
            "description": metadata["description"],
            "frontmatter_valid": metadata["frontmatter_valid"] == "true",
            "skill_file": str(skill_path.relative_to(root)),
            "skill_sha256": _sha256(skill_path),
            "storyboard_file": str(storyboard.relative_to(root)) if storyboard.is_file() else None,
            "storyboard_sha256": _sha256(storyboard) if storyboard.is_file() else None,
            "auxiliary_files": scripts,
            **implementation,
        })
    lock = {
        "schema_version": "motion-skills-lock.v1",
        "source_archive": Path(archive_path).name if archive_path else "Belyak_36_skills_OpenCode.zip",
        "source_archive_sha256": _sha256(Path(archive_path)) if archive_path and Path(archive_path).is_file() else None,
        "skill_count": len(skills),
        "storyboard_count": sum(1 for item in skills if item["storyboard_file"]),
        "skills": skills,
    }
    return lock


def load_registry(project_root: Path, verify_hashes: bool = True) -> dict:
    root = Path(project_root).resolve()
    lock_path = root / "motion" / "skills" / "skills-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    for skill in lock.get("skills") or []:
        skill_path = root / skill["skill_file"]
        valid = skill_path.is_file() and skill.get("frontmatter_valid", False)
        if valid and verify_hashes:
            valid = _sha256(skill_path) == skill.get("skill_sha256")
            storyboard = skill.get("storyboard_file")
            if storyboard:
                asset = root / storyboard
                valid = valid and asset.is_file() and _sha256(asset) == skill.get("storyboard_sha256")
        skill["integrity"] = "verified" if valid else "invalid"
        if not valid:
            skill["status"] = "unavailable"
            skill["executors"] = []
    return lock


def route_skills(project_root: Path, task: str = "default", limit: int = 4) -> List[dict]:
    """Return only skills with real executors; reference-only material is not selectable for production."""
    registry = load_registry(project_root)
    by_id = {item["id"]: item for item in registry.get("skills") or []}
    selected = []
    for skill_id in ROUTES.get(task, ROUTES["default"]):
        item = by_id.get(skill_id)
        if item and item.get("integrity") == "verified" and item.get("status") in {"partial", "implemented"} and item.get("executors"):
            selected.append(item)
        if len(selected) >= max(1, min(4, int(limit))):
            break
    return selected


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_root", nargs="?", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--archive")
    parser.add_argument("--write-lock", action="store_true")
    args = parser.parse_args()
    result = build_lock(Path(args.project_root), Path(args.archive) if args.archive else None)
    if args.write_lock:
        target = Path(args.project_root) / "motion" / "skills" / "skills-lock.json"
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"skills": result["skill_count"], "storyboards": result["storyboard_count"], "lock": str(Path(args.project_root) / "motion" / "skills" / "skills-lock.json")}, ensure_ascii=False))
