"""Scenario schema, Pydantic v2 (BUILD_PLAN §2.5; spec §3.1 scenario, §3.2 overlay taxonomy, §4.2 tools).

Field names and types are the BUILD_PLAN contract. Optional fields and lists
default to None / empty so a YAML need not spell out every empty field.
Semantic checks (anchors, token lists, pause geometry) live in
`scenarios.loader.validate`.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

OverlayClass = Literal["Y-INT", "Y-CMD", "H-BC", "H-SIDE", "H-BG", "H-TIC", "P-PAUSE", "E-END"]
OVERLAY_CLASSES: tuple[str, ...] = ("Y-INT", "Y-CMD", "H-BC", "H-SIDE", "H-BG", "H-TIC",
                                    "P-PAUSE", "E-END")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CallerStep(_Model):
    """One scripted caller step (spec §3.1). `pause_ms` is a mid-turn pause (spec §3.2 P-PAUSE)."""
    id: str
    text: str
    clip: str | None = None
    speech_onset_ms: int | None = None
    speech_end_ms: int | None = None
    pause_ms: int | None = None
    resumption_text: str | None = None


class Overlay(_Model):
    """An overlay event, labelled by construction with its intent class (spec §3.2).

    `resumption_ms`: scripted silence before the caller resumes; for wait-class
    Y-CMD it is the 3-6 s resumption, for E-END the post-turn silence (>= 6 s).
    """
    id: str
    cls: OverlayClass
    anchor_step: str
    placement: Literal["during_agent", "in_pause"]
    offset_ms: int
    clip: str | None = None
    text: str
    level_db: float = 0.0
    response_anchors: list[str] = Field(default_factory=list)
    resumption_ms: int | None = None
    voice_id: str | None = None


class ToolDef(_Model):
    """Mock tool with injected latency and failure (spec §4.2)."""
    name: str
    kind: Literal["read", "write"]
    latency_ms: int = 0
    failure: Literal["none", "timeout", "error", "partial"] = "none"
    result_anchors: list[str] = Field(default_factory=list)
    anchor_step: str | None = None      # caller step carrying the arguments (metrics/task.py t_tool_offset)


class Scenario(_Model):
    """A versioned scenario (spec §3.1); split per spec §11."""
    id: str
    version: str
    split: Literal["public-dev", "private-test"]
    domain: str
    track: Literal["A", "C", "D"]
    source: Literal["scripted", "simulated", "recorded"]
    speaker_id: str | None = None
    caller_voice: str
    speaking_rate: float = 1.0
    persona: str | None = None
    goal: str | None = None
    facts: dict[str, Any] = Field(default_factory=dict)
    steps: list[CallerStep]
    overlays: list[Overlay] = Field(default_factory=list)
    tools: list[ToolDef] = Field(default_factory=list)
    expected_state: dict[str, Any] = Field(default_factory=dict)
    required_spoken: list[dict[str, Any]] = Field(default_factory=list)
    entities: list[dict[str, Any]] = Field(default_factory=list)
    policy: dict[str, Any] = Field(default_factory=dict)
    conditions: list[str] = Field(default_factory=list)
    trials: int = 1
    control_twin: bool = False
