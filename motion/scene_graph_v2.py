"""Typed, deterministic scene graph v2 and a compatibility compiler to plan v1."""

from __future__ import annotations

import math
import re
from typing import Dict, List, Literal, Optional

from pydantic import Field, model_validator

from .models import AnimationSpec, MotionPlan, MotionScene, OutputSpec, Placement, StrictModel


class GraphRect(StrictModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0.01, le=1)
    h: float = Field(gt=0.01, le=1)

    @model_validator(mode="after")
    def inside_layer_box(self):
        if self.x + self.w > 1 + 1e-9 or self.y + self.h > 1 + 1e-9:
            raise ValueError("graph layer geometry must remain inside its scene box")
        return self


class GraphLayer(StrictModel):
    id: str = Field(min_length=1, max_length=48)
    kind: Literal["text", "shape", "line"]
    geometry: GraphRect
    text: Optional[str] = Field(default=None, max_length=100)
    shape: Optional[Literal["circle", "rounded_rect", "arrow", "underline", "bracket"]] = None
    color: Literal["accent", "ink", "muted", "white"] = "accent"
    font_size_px: int = Field(default=48, ge=16, le=120)
    font_weight: Literal[400, 500, 600, 700, 800, 900] = 700
    align: Literal["left", "center", "right"] = "center"

    @model_validator(mode="before")
    @classmethod
    def default_safe_primitive(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if data.get("kind") == "line" and not data.get("shape"):
            data["shape"] = "underline"
        if data.get("kind") == "shape" and not data.get("shape"):
            data["shape"] = "rounded_rect"
        return data

    @model_validator(mode="after")
    def valid_primitive(self):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", self.id):
            raise ValueError("graph layer id is invalid")
        if self.kind == "text" and not str(self.text or "").strip():
            raise ValueError("text graph layers require text")
        if self.kind == "shape" and not self.shape:
            raise ValueError("shape graph layers require a supported shape")
        if self.kind == "line" and self.shape not in {"arrow", "underline", "bracket"}:
            raise ValueError("line graph layers require a line primitive")
        return self


class Keyframe(StrictModel):
    at_ms: int = Field(ge=0, le=12_000)
    value: float = Field(ge=-2, le=2)


class MotionTrack(StrictModel):
    layer_id: str = Field(min_length=1, max_length=48)
    property: Literal["opacity", "x", "y", "scale", "draw_progress"]
    easing: Literal["linear", "ease_out", "ease_in_out"] = "ease_out"
    keyframes: List[Keyframe] = Field(min_length=2, max_length=16)

    @model_validator(mode="after")
    def ordered_keyframes(self):
        times = [item.at_ms for item in self.keyframes]
        if times != sorted(times) or len(times) != len(set(times)):
            raise ValueError("motion keyframes must be strictly ordered")
        if self.property == "opacity" and any(not 0 <= key.value <= 1 for key in self.keyframes):
            raise ValueError("opacity keyframes must be in [0, 1]")
        if self.property == "scale" and any(not 0.1 <= key.value <= 3 for key in self.keyframes):
            raise ValueError("scale keyframes must be in [0.1, 3]")
        if self.property == "draw_progress" and any(not 0 <= key.value <= 1 for key in self.keyframes):
            raise ValueError("draw progress keyframes must be in [0, 1]")
        return self


class StyleBible(StrictModel):
    style_id: Literal["minimal", "editorial-kinetic", "tech-ui"] = "editorial-kinetic"
    accent: str = Field(default="#F4C95D", pattern=r"^#[0-9A-Fa-f]{6}$")
    ink: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")
    muted: str = Field(default="#C5CAD3", pattern=r"^#[0-9A-Fa-f]{6}$")
    font_family: Literal["Arial", "Helvetica", "system-sans"] = "Arial"
    pace: Literal["restrained", "balanced", "expressive"] = "balanced"


class SceneGraph(StrictModel):
    scene_id: str = Field(min_length=1, max_length=64)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    mode: Literal["overlay", "scene_takeover", "hybrid"] = "overlay"
    semantic_goal: str = Field(min_length=1, max_length=300)
    spoken_evidence: str = Field(default="", max_length=800)
    skills_used: List[str] = Field(default_factory=list, max_length=4)
    placement: Placement
    layers: List[GraphLayer] = Field(min_length=1, max_length=12)
    tracks: List[MotionTrack] = Field(default_factory=list, max_length=32)
    review_status: Literal["proposed", "accepted", "rejected", "disabled"] = "proposed"

    @model_validator(mode="after")
    def valid_graph(self):
        duration = self.end_ms - self.start_ms
        if duration < 400 or duration > 12_000:
            raise ValueError("scene graph interval must be between 400 and 12000 ms")
        ids = {layer.id for layer in self.layers}
        if len(ids) != len(self.layers):
            raise ValueError("scene graph layer ids must be unique")
        if any(not re.fullmatch(r"(?:video|edit)-[a-z0-9-]{2,48}", skill) for skill in self.skills_used):
            raise ValueError("scene graph contains an invalid skill id")
        for track in self.tracks:
            if track.layer_id not in ids:
                raise ValueError("motion track refers to an unknown layer")
            if track.keyframes[-1].at_ms > duration:
                raise ValueError("motion track exceeds its scene interval")
            for keyframe in track.keyframes:
                # The renderer clips all graph layers to the scene container;
                # limited overshoot enables masked slide-ins without leaving the safe zone.
                if track.property in {"x", "y"} and not -0.25 <= keyframe.value <= 1.25:
                    raise ValueError("tracked position exceeds the masked scene viewport")
        return self


class MotionPlanV2(StrictModel):
    schema_version: Literal["motion-plan.v2"] = "motion-plan.v2"
    source_job_id: str = Field(min_length=1, max_length=100)
    source_fingerprint: str = Field(min_length=20, max_length=128)
    revision: int = Field(ge=1)
    profile: Literal["minimal", "financial", "tech-ui", "editorial", "custom"] = "custom"
    duration_ms: int = Field(gt=0)
    output: OutputSpec
    style_bible: StyleBible = Field(default_factory=StyleBible)
    scenes: List[SceneGraph] = Field(min_length=1, max_length=24)

    @model_validator(mode="before")
    @classmethod
    def normalize_generated_geometry(cls, value):
        """Clamp model-authored bounds/keyframes without altering semantic content."""
        if not isinstance(value, dict):
            return value
        data = dict(value)

        def finite_number(item, default):
            try:
                number = float(item)
                return number if math.isfinite(number) else default
            except (TypeError, ValueError):
                return default

        for scene in data.get("scenes") or []:
            if not isinstance(scene, dict):
                continue
            placement = scene.get("placement")
            if isinstance(placement, dict):
                box = dict(placement)
                width = min(1.0, max(0.05, finite_number(box.get("w"), 0.5)))
                height = min(1.0, max(0.05, finite_number(box.get("h"), 0.15)))
                x = finite_number(box.get("x"), 0.5)
                y = finite_number(box.get("y"), 0.5)
                anchor = box.get("anchor", "center")
                if anchor == "center":
                    x = min(1 - width / 2, max(width / 2, x))
                    y = min(1 - height / 2, max(height / 2, y))
                elif anchor == "top-left":
                    x, y = min(1 - width, max(0, x)), min(1 - height, max(0, y))
                elif anchor == "top-right":
                    x, y = min(1, max(width, x)), min(1 - height, max(0, y))
                elif anchor == "bottom-left":
                    x, y = min(1 - width, max(0, x)), min(1, max(height, y))
                elif anchor == "bottom-right":
                    x, y = min(1, max(width, x)), min(1, max(height, y))
                box.update({"x": x, "y": y, "w": width, "h": height})
                scene["placement"] = box

            layer_boxes = {}
            for layer in scene.get("layers") or []:
                if not isinstance(layer, dict) or not isinstance(layer.get("geometry"), dict):
                    continue
                rect = dict(layer["geometry"])
                width = min(1.0, max(0.02, finite_number(rect.get("w"), 0.4)))
                height = min(1.0, max(0.02, finite_number(rect.get("h"), 0.2)))
                rect["x"] = min(1 - width, max(0, finite_number(rect.get("x"), 0.1)))
                rect["y"] = min(1 - height, max(0, finite_number(rect.get("y"), 0.1)))
                rect.update({"w": width, "h": height})
                layer["geometry"] = rect
                layer_boxes[layer.get("id")] = rect

            for track in scene.get("tracks") or []:
                if not isinstance(track, dict):
                    continue
                rect = layer_boxes.get(track.get("layer_id"))
                if not rect:
                    continue
                prop = track.get("property")
                for keyframe in track.get("keyframes") or []:
                    if not isinstance(keyframe, dict):
                        continue
                    current = finite_number(keyframe.get("value"), 0)
                    if prop == "x":
                        current = min(1.25, max(-0.25, current))
                    elif prop == "y":
                        current = min(1.25, max(-0.25, current))
                    elif prop == "scale":
                        current = min(2, max(0.1, current))
                    keyframe["value"] = current
        return data

    @model_validator(mode="after")
    def scenes_fit_source(self):
        ids = set()
        for scene in self.scenes:
            if scene.scene_id in ids:
                raise ValueError("scene ids must be unique")
            ids.add(scene.scene_id)
            if scene.end_ms > self.duration_ms:
                raise ValueError("scene exceeds fixed source duration")
        return self

    def compile_legacy_plan(self) -> MotionPlan:
        """Compile typed v2 graphs to the existing immutable renderer contract."""
        scenes = []
        for scene in self.scenes:
            if scene.mode != "overlay":
                raise ValueError("scene mode %s has no registered executor yet" % scene.mode)
            graph = {
                "version": 2,
                "style_bible": self.style_bible.model_dump(mode="json"),
                "layers": [layer.model_dump(mode="json") for layer in scene.layers],
                "tracks": [track.model_dump(mode="json") for track in scene.tracks],
                "skills_used": list(scene.skills_used),
            }
            scenes.append(MotionScene(
                scene_id=scene.scene_id,
                start_ms=scene.start_ms,
                end_ms=scene.end_ms,
                review_status=scene.review_status,
                spoken_evidence=scene.spoken_evidence,
                semantic_goal=scene.semantic_goal,
                effect_type="kinetic_sequence",
                content={"data_origin": "derived", "motion_graph": graph},
                placement=scene.placement,
                animation=AnimationSpec(preset="none", enter_ms=0, exit_ms=0),
                evidence_time_ms=scene.start_ms,
                explanation="Compiled from typed motion-plan.v2 scene graph.",
            ))
        return MotionPlan(
            schema_version="motion-plan.v1",
            source_job_id=self.source_job_id,
            source_fingerprint=self.source_fingerprint,
            revision=self.revision,
            profile=self.profile,
            duration_ms=self.duration_ms,
            output=self.output,
            scenes=scenes,
        )
