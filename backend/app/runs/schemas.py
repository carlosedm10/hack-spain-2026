from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EventIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    event: str


class NodeOut(BaseModel):
    id: str
    level: int
    threshold: float
    intent: str | None = None
    action_id: str | None = None


class IngestOut(BaseModel):
    level: int
    confidence: float
    intent: str | None = None
    escalated: bool
    degraded: bool
    node_id: str | None = None
    event_id: str | None = None
    decision: str = "allow"
    event_risk: float = 0.0
    trajectory_risk: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    graph_persisted: bool = False
    dispatch_actions: list[dict[str, Any]] = Field(default_factory=list)
    jev_latency_ms: float | None = None
    duplicate: bool = False
    drift: dict[str, Any] | None = None
    findings: list[dict[str, Any]] = Field(default_factory=list)


class GraphOut(BaseModel):
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


class RunOut(BaseModel):
    run_id: str
    level: int
    key_nodes: list[NodeOut]


class RunSummary(BaseModel):
    run_id: str
    level: int
    nodes: int
