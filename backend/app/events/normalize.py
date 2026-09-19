from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.events.models import (
    EventEffect,
    EventOrigin,
    EventPhase,
    IdentityState,
    MonitorEvent,
    Reversibility,
    TrustState,
)

_TOOL_EVENTS = {
    "file_read",
    "file_edit",
    "network_request",
    "register_tool",
    "run_tool",
    "shell_command",
    "tool_read",
    "tool_write",
    "memory_read",
    "memory_write",
    "schedule",
}

_TOOL_NAMES = {
    "file_read": "read_file",
    "file_edit": "write_file",
    "network_request": "http_request",
    "shell_command": "shell",
}


def _enum_or_default(enum_type, value: Any, default):
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return default


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(UTC)


def normalize_event(
    run_id: str, payload: dict[str, Any], *, sequence: int | None = None
) -> MonitorEvent:
    """Normalize legacy harness/eval payloads without dropping their original fields."""
    kind = str(payload.get("kind") or payload.get("event") or "unknown")
    phase = _enum_or_default(EventPhase, payload.get("phase"), EventPhase.OBSERVED)
    if "phase" not in payload and kind in _TOOL_EVENTS:
        phase = EventPhase.COMPLETED

    origin_default = EventOrigin.TOOL if kind in _TOOL_EVENTS else EventOrigin.HARNESS
    origin = _enum_or_default(EventOrigin, payload.get("origin"), origin_default)
    identity = _enum_or_default(IdentityState, payload.get("identity_state"), IdentityState.UNKNOWN)
    trust = _enum_or_default(TrustState, payload.get("trust"), TrustState.UNKNOWN)

    reversibility = payload.get("reversibility")
    if isinstance(reversibility, bool):
        reversibility = "compensable" if reversibility else "irreversible"
    effect_payload = payload.get("effect") if isinstance(payload.get("effect"), dict) else {}
    effect = EventEffect(
        reversibility=_enum_or_default(
            Reversibility,
            effect_payload.get("reversibility", reversibility),
            Reversibility.UNKNOWN,
        ),
        visible_to_user=effect_payload.get("visible_to_user", payload.get("visible_to_user")),
        sensitivity=str(effect_payload.get("sensitivity", payload.get("sensitivity", "unknown"))),
        scope=int(effect_payload.get("scope", payload.get("scope", 0)) or 0),
        amount=effect_payload.get("amount", payload.get("amount")),
    )

    reserved = {
        "id",
        "event_id",
        "run_id",
        "session_id",
        "seq",
        "sequence",
        "ts",
        "timestamp",
        "kind",
        "event",
        "phase",
        "origin",
        "agent",
        "tool",
        "target",
        "channel",
        "identity_state",
        "trust",
        "content",
        "args",
        "result",
        "effect",
        "caused_by",
        "after",
        "derived_from",
        "policy_version",
        "metadata",
    }
    args = payload.get("args")
    if not isinstance(args, dict):
        args = {key: value for key, value in payload.items() if key not in reserved}

    caused_by = payload.get("caused_by", payload.get("after", []))
    if isinstance(caused_by, str):
        caused_by = [caused_by]
    derived_from = payload.get("derived_from", [])
    if isinstance(derived_from, str):
        derived_from = [derived_from]

    return MonitorEvent(
        id=str(payload.get("event_id") or payload.get("id") or f"{run_id}:{sequence}")
        if sequence is not None or payload.get("event_id") or payload.get("id")
        else MonitorEvent(run_id=run_id, kind=kind).id,
        run_id=run_id,
        session_id=payload.get("session_id"),
        sequence=payload.get("sequence", payload.get("seq", sequence)),
        timestamp=_timestamp(payload.get("timestamp", payload.get("ts"))),
        kind=kind,
        phase=phase,
        origin=origin,
        agent=payload.get("agent"),
        tool=payload.get("tool") or _TOOL_NAMES.get(kind),
        target=payload.get("target") or payload.get("dst") or payload.get("path"),
        channel=payload.get("channel"),
        identity_state=identity,
        trust=trust,
        content=payload.get("content") or payload.get("output"),
        args=args,
        result=payload.get("result"),
        effect=effect,
        caused_by=list(caused_by or []),
        derived_from=list(derived_from or []),
        policy_version=payload.get("policy_version"),
        metadata=dict(payload.get("metadata") or {}),
        raw=dict(payload),
    )
