from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class EventPhase(StrEnum):
    OBSERVED = "observed"
    REQUESTED = "requested"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUSED = "refused"


class EventOrigin(StrEnum):
    USER = "user"
    AGENT = "agent"
    TOOL = "tool"
    HARNESS = "harness"
    SYSTEM = "system"
    NETWORK = "network"
    MONITOR = "monitor"
    DISPATCHER = "dispatcher"
    REPLAY = "replay"


class IdentityState(StrEnum):
    VERIFIED = "verified"
    PARTIAL = "partial"
    UNVERIFIED = "unverified"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


class TrustState(StrEnum):
    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class Reversibility(StrEnum):
    REVERSIBLE = "reversible"
    COMPENSABLE = "compensable"
    IRREVERSIBLE = "irreversible"
    UNKNOWN = "unknown"


class EventEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reversibility: Reversibility = Reversibility.UNKNOWN
    visible_to_user: bool | None = None
    sensitivity: str = "unknown"
    scope: int = Field(default=0, ge=0)
    amount: float | None = None


class MonitorEvent(BaseModel):
    """Canonical append-only event shared by the harness, monitor and eval runner."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str = Field(min_length=1)
    session_id: str | None = None
    sequence: int | None = Field(default=None, ge=0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    kind: str = Field(min_length=1)
    phase: EventPhase = EventPhase.OBSERVED
    origin: EventOrigin = EventOrigin.HARNESS
    agent: str | None = None
    tool: str | None = None
    target: str | None = None
    channel: str | None = None

    identity_state: IdentityState = IdentityState.UNKNOWN
    trust: TrustState = TrustState.UNKNOWN
    content: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any | None = None
    effect: EventEffect = Field(default_factory=EventEffect)

    caused_by: list[str] = Field(default_factory=list)
    derived_from: list[str] = Field(default_factory=list)
    policy_version: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)

    def graph_properties(self) -> dict[str, Any]:
        """Return Neo4j-safe scalar properties; nested data stays as JSON."""
        import json

        return {
            "id": self.id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "timestamp": self.timestamp.isoformat(),
            "kind": self.kind,
            "phase": self.phase.value,
            "origin": self.origin.value,
            "agent": self.agent,
            "tool": self.tool,
            "target": self.target,
            "channel": self.channel,
            "identity_state": self.identity_state.value,
            "trust": self.trust.value,
            "content": self.content,
            "args_json": json.dumps(self.args, default=str, sort_keys=True),
            "result_json": json.dumps(self.result, default=str, sort_keys=True),
            "effect_json": self.effect.model_dump_json(),
            "caused_by_json": json.dumps(self.caused_by),
            "derived_from_json": json.dumps(self.derived_from),
            "policy_version": self.policy_version,
            "metadata_json": json.dumps(self.metadata, default=str, sort_keys=True),
            "raw_json": json.dumps(self.raw, default=str, sort_keys=True),
        }
