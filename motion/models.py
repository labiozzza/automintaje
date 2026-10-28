"""Versioned request and plan models for Motion Graphics Plus."""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "motion-plan.v1"
EFFECT_TYPES = {
    "none",
    "animated_text",
    "stat_card",
    "comparison_bars",
    "progress_arc",
    "timeline",
    "process_flow",
    "highlight_box",
    "counter",
    "data_chart",
    "callout",
    "shape",
    "mask_reveal",
}
PROFILES = {"minimal", "financial", "tech-ui", "editorial", "custom"}
INTENSITIES = {"minimal", "balanced", "expressive"}
DATA_ORIGINS = {"transcript", "user_confirmed", "derived"}
FORBIDDEN_CONTENT_KEYS = {
    "code", "command", "exec", "file", "filename", "html", "path", "python",
    "shell", "script", "tsx", "url",
}
SHAPE_TYPES = {"circle", "rounded-rect", "line", "arrow", "underline", "bracket"}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OutputSpec(StrictModel):
    width: int = Field(ge=2, le=7680)
    height: int = Field(ge=2, le=7680)
    fps_num: int = Field(ge=1, le=240_000)
    fps_den: int = Field(ge=1, le=1001)

    @model_validator(mode="after")
    def supported_fps(self):
        if self.fps_num < self.fps_den or self.fps_num > 240 * self.fps_den:
            raise ValueError("rational FPS must be between 1 and 240")
        return self


class Placement(StrictModel):
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)
    anchor: Literal["center", "top-left", "top-right", "bottom-left", "bottom-right"] = "center"

    def rect(self) -> tuple[float, float, float, float]:
        """Return normalized (left, top, width, height) using the declared anchor."""
        if self.anchor == "center":
            return self.x - self.w / 2, self.y - self.h / 2, self.w, self.h
        if self.anchor == "top-left":
            return self.x, self.y, self.w, self.h
        if self.anchor == "top-right":
            return self.x - self.w, self.y, self.w, self.h
        if self.anchor == "bottom-left":
            return self.x, self.y - self.h, self.w, self.h
        return self.x - self.w, self.y - self.h, self.w, self.h

    @model_validator(mode="after")
    def stays_on_canvas(self):
        left, top, width, height = self.rect()
        if left < -1e-9 or left + width > 1 + 1e-9:
            raise ValueError("placement must remain within the horizontal canvas")
        if top < -1e-9 or top + height > 1 + 1e-9:
            raise ValueError("placement must remain within the vertical canvas")
        return self


class AnimationSpec(StrictModel):
    preset: Literal[
        "fade", "slide-up", "slide-down", "grow", "draw", "grow-compare",
        "pulse", "wipe", "none",
    ] = "fade"
    enter_ms: int = Field(default=300, ge=0, le=5000)
    exit_ms: int = Field(default=200, ge=0, le=5000)
    delay_ms: int = Field(default=0, ge=0, le=30_000)


class MotionScene(StrictModel):
    scene_id: str = Field(min_length=1, max_length=64)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    review_status: Literal["proposed", "accepted", "rejected", "disabled"] = "proposed"
    spoken_evidence: str = Field(default="", max_length=800)
    semantic_goal: str = Field(min_length=1, max_length=300)
    effect_type: str
    content: Dict[str, Any] = Field(default_factory=dict)
    placement: Placement
    animation: AnimationSpec = Field(default_factory=AnimationSpec)
    evidence_time_ms: Optional[int] = Field(default=None, ge=0)
    explanation: str = Field(default="", max_length=600)
    asset_refs: List[str] = Field(default_factory=list, max_length=12)

    @field_validator("scene_id")
    @classmethod
    def safe_scene_id(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
            raise ValueError("scene_id contains unsupported characters")
        return value

    @field_validator("effect_type")
    @classmethod
    def known_effect(cls, value):
        if value not in EFFECT_TYPES:
            raise ValueError("unsupported effect_type")
        return value

    @field_validator("asset_refs")
    @classmethod
    def safe_asset_refs(cls, values):
        for value in values:
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value):
                raise ValueError("asset_refs must contain local asset ids, not paths or URLs")
        return values

    @model_validator(mode="after")
    def validate_interval_and_content(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("scene end_ms must be greater than start_ms")
        if self.end_ms - self.start_ms < 400 or self.end_ms - self.start_ms > 12_000:
            raise ValueError("scene duration must be between 400 and 12000 milliseconds")
        numeric_values = _validate_content(self.content)
        if numeric_values and self.content.get("data_origin") not in DATA_ORIGINS:
            raise ValueError("numeric content requires a transcript, user_confirmed, or derived source")
        if self.effect_type in {"animated_text", "callout", "mask_reveal"}:
            if not any(str(self.content.get(key) or "").strip() for key in ("text", "title", "label")):
                raise ValueError("text effects require text, title, or label content")
        if self.effect_type in {"stat_card", "counter", "progress_arc"}:
            if self.content.get("value") is None or not any(self.content.get(key) for key in ("label", "title")):
                raise ValueError("stat effects require value and label")
        if self.effect_type in {"comparison_bars", "data_chart"}:
            required = ("left_label", "left_value", "right_label", "right_value")
            if any(self.content.get(key) is None for key in required):
                raise ValueError("comparison effects require two labeled values")
        if self.effect_type in {"process_flow", "timeline"}:
            steps = self.content.get("steps")
            if not isinstance(steps, list) or not 2 <= len(steps) <= 6 or any(not isinstance(step, str) for step in steps):
                raise ValueError("process flow requires between 2 and 6 text steps")
        if self.effect_type == "shape" and self.content.get("shape") not in SHAPE_TYPES:
            raise ValueError("shape effect requires a supported primitive shape")
        return self


class MotionPlan(StrictModel):
    schema_version: Literal["motion-plan.v1"] = SCHEMA_VERSION
    source_job_id: str = Field(min_length=1, max_length=100)
    source_fingerprint: str = Field(min_length=20, max_length=128)
    revision: int = Field(ge=1)
    profile: Literal["minimal", "financial", "tech-ui", "editorial", "custom"]
    output: OutputSpec
    duration_ms: int = Field(gt=0)
    scenes: List[MotionScene] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def validate_scenes(self):
        seen = set()
        for scene in self.scenes:
            if scene.scene_id in seen:
                raise ValueError("scene_id values must be unique")
            seen.add(scene.scene_id)
            if scene.end_ms > self.duration_ms:
                raise ValueError("scene interval exceeds the source duration")
            if scene.evidence_time_ms is not None and scene.evidence_time_ms > self.duration_ms:
                raise ValueError("evidence_time_ms exceeds the source duration")
        return self


class CreateProjectInput(StrictModel):
    source_job_id: str = Field(min_length=1, max_length=100)
    source_kind: Literal["prefer_master", "master", "delivery"] = "prefer_master"
    user_intent: str = Field(default="", max_length=2000)
    style: Literal["minimal", "financial", "tech-ui", "editorial", "custom"] = "minimal"
    intensity: Literal["minimal", "balanced", "expressive"] = "balanced"
    request_id: str = Field(default="", max_length=100)


class DesignInput(StrictModel):
    model: str = Field(min_length=3, max_length=100)
    instruction: str = Field(default="", max_length=4000)
    request_id: str = Field(min_length=1, max_length=100)
    max_agent_calls: int = Field(default=3, ge=1, le=6)
    user_intent: Optional[str] = Field(default=None, max_length=2000)
    style: Optional[Literal["minimal", "financial", "tech-ui", "editorial", "custom"]] = None
    intensity: Optional[Literal["minimal", "balanced", "expressive"]] = None


class MessageInput(StrictModel):
    model: str = Field(min_length=3, max_length=100)
    instruction: str = Field(min_length=1, max_length=4000)
    expected_revision: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=100)
    user_intent: Optional[str] = Field(default=None, max_length=2000)
    style: Optional[Literal["minimal", "financial", "tech-ui", "editorial", "custom"]] = None
    intensity: Optional[Literal["minimal", "balanced", "expressive"]] = None


class ScenePatchInput(StrictModel):
    action: Literal["accept", "reject", "disable", "revise"]
    expected_revision: int = Field(ge=1)
    feedback: str = Field(default="", max_length=2000)
    scene: Optional[MotionScene] = None

    @model_validator(mode="after")
    def action_payload(self):
        if self.action == "revise" and not self.feedback and self.scene is None:
            raise ValueError("revise requires feedback or a replacement scene")
        return self


class ApproveInput(StrictModel):
    expected_revision: int = Field(ge=1)


class PreviewInput(StrictModel):
    expected_revision: int = Field(ge=1)
    max_duration_ms: int = Field(default=15_000, ge=1000, le=60_000)


class RenderInput(StrictModel):
    expected_revision: int = Field(ge=1)
    request_id: str = Field(min_length=1, max_length=100)


class MirrorInput(StrictModel):
    request_id: str = Field(min_length=1, max_length=100)


def _validate_content(value: Any, depth: int = 0) -> List[float]:
    if depth > 8:
        raise ValueError("scene content is nested too deeply")
    if isinstance(value, dict):
        if len(value) > 64:
            raise ValueError("scene content contains too many fields")
        numbers = []
        for key, child in value.items():
            key = str(key).lower()
            if key in FORBIDDEN_CONTENT_KEYS:
                raise ValueError("executable code, paths, and URLs are not valid scene content")
            numbers.extend(_validate_content(child, depth + 1))
        return numbers
    if isinstance(value, list):
        if len(value) > 128:
            raise ValueError("scene content arrays exceed the supported limit")
        numbers = []
        for child in value:
            numbers.extend(_validate_content(child, depth + 1))
        return numbers
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("scene content must use finite numbers")
        return [number]
    if isinstance(value, str):
        if len(value) > 4000:
            raise ValueError("a scene content string is too long")
        return []
    raise ValueError("unsupported scene content value")


def parse_motion_plan(raw: Any, *, duration_ms: int, source_job_id: str,
                      fingerprint: str, revision: int) -> MotionPlan:
    """Validate the model response and bind it to the immutable source/revision."""
    if isinstance(raw, dict) and "plan" in raw and isinstance(raw["plan"], dict):
        raw = raw["plan"]
    if not isinstance(raw, dict):
        raise ValueError("Motion Director response must be a JSON object")
    payload = dict(raw)
    payload["source_job_id"] = source_job_id
    payload["source_fingerprint"] = fingerprint
    payload["revision"] = revision
    payload["duration_ms"] = duration_ms
    plan = MotionPlan.model_validate(payload)
    return plan
