from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.classification.models import Level


class ToolDecision(StrEnum):
    ALLOW = "allow"
    HOLD = "hold"
    REFUSE = "refuse"


class DriftState(BaseModel):
    data_exposure: int = Field(default=0, ge=0, le=4)
    tool_escalation: int = Field(default=0, ge=0, le=4)
    reversibility: int = Field(default=0, ge=0, le=4)
    authority_drift: int = Field(default=0, ge=0, le=4)
    context_integrity: int = Field(default=0, ge=0, le=4)
    blast_radius: int = Field(default=0, ge=0, le=4)
    amount_total: float = 0.0
    records_total: int = 0
    moved: list[str] = Field(default_factory=list)
    markov_state: tuple[int, int] = (0, 0)
    p_violation: dict[int, float] = Field(default_factory=lambda: {1: 0.0, 3: 0.0, 5: 0.0})


class SentinelFinding(BaseModel):
    rule_id: str
    severity: int = Field(ge=0, le=5)
    evidence_event_ids: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    cross_run: bool = False
    detail: str


class GateDecision(BaseModel):
    decision: ToolDecision
    incident_level: Level
    event_risk: float = Field(ge=0.0, le=1.0)
    trajectory_risk: float = Field(ge=0.0, le=1.0)
    reasons: list[str] = Field(default_factory=list)


class MonitorAssessment(BaseModel):
    event_id: str
    run_id: str
    drift: DriftState
    findings: list[SentinelFinding]
    gate: GateDecision
    jev_answers: dict[str, Any] = Field(default_factory=dict)
    jev_model: str | None = None
    jev_latency_ms: float | None = None
    jev_degraded_reason: str | None = None
    dispatch_actions: list[dict[str, Any]] = Field(default_factory=list)
