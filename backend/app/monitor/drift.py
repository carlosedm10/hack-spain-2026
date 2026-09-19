from __future__ import annotations

import threading

from app.events import (
    EventOrigin,
    EventPhase,
    IdentityState,
    MonitorEvent,
    Reversibility,
    TrustState,
)
from app.monitor.markov import CompactMarkovModel
from app.monitor.models import DriftState
from app.monitor.policy import DEFAULT_POLICY, AgentPolicy


class SafetyDrift:
    """Deterministic cumulative risk state with a compact Markov-style forecast."""

    def __init__(self) -> None:
        self._states: dict[str, DriftState] = {}
        self._lock = threading.RLock()
        self._markov = CompactMarkovModel.prior()

    def clear(self) -> None:
        with self._lock:
            self._states.clear()

    def state(self, run_id: str) -> DriftState:
        with self._lock:
            return self._states.get(run_id, DriftState()).model_copy(deep=True)

    def restore(self, run_id: str, state: DriftState) -> None:
        with self._lock:
            self._states[run_id] = state.model_copy(deep=True)

    def step(
        self,
        event: MonitorEvent,
        policy: AgentPolicy = DEFAULT_POLICY,
    ) -> DriftState:
        with self._lock:
            previous = self._states.get(event.run_id, DriftState())
            current = previous.model_copy(deep=True)
            moved: list[str] = []

            def raise_band(field: str, value: int) -> None:
                old = getattr(current, field)
                new = max(old, min(4, value))
                if new > old:
                    setattr(current, field, new)
                    moved.append(field)

            sensitivity = event.effect.sensitivity.lower()
            external = bool(
                event.target
                and (
                    event.target.startswith(("http://", "https://", "external:"))
                    or event.kind == "network_request"
                )
            )
            if sensitivity in {"credential", "secret", "health"}:
                raise_band("data_exposure", 4 if external else 3)
            elif sensitivity in {"personal", "pii"}:
                raise_band("data_exposure", 3 if external else 2)

            if event.tool:
                if event.tool not in policy.tools:
                    raise_band("tool_escalation", 3)
                if event.kind in {"register_tool", "shell_command"}:
                    raise_band("tool_escalation", 2)

            reversibility = event.effect.reversibility
            if reversibility == Reversibility.COMPENSABLE:
                raise_band("reversibility", 2)
            elif reversibility == Reversibility.IRREVERSIBLE:
                raise_band(
                    "reversibility",
                    4 if event.effect.visible_to_user is False else 3,
                )

            is_write = event.kind in {
                "file_edit",
                "memory_write",
                "schedule",
                "tool_write",
                "register_tool",
                "network_request",
            }
            if is_write and event.identity_state in {
                IdentityState.UNVERIFIED,
                IdentityState.CONFLICTING,
            }:
                raise_band(
                    "authority_drift", 4 if event.identity_state == IdentityState.CONFLICTING else 3
                )
            elif is_write and event.identity_state == IdentityState.PARTIAL:
                raise_band("authority_drift", 1)

            if event.kind == "memory_write" and event.trust in {
                TrustState.UNTRUSTED,
                TrustState.MIXED,
            }:
                raise_band("context_integrity", 3)
            if event.derived_from and event.trust != TrustState.TRUSTED:
                raise_band("context_integrity", 3)

            scope = max(0, event.effect.scope)
            if event.phase == EventPhase.COMPLETED:
                current.records_total += scope
                current.amount_total += float(event.effect.amount or 0.0)
            if scope > 100:
                raise_band("blast_radius", 4)
            elif scope > 10:
                raise_band("blast_radius", 3)
            elif scope > 1:
                raise_band("blast_radius", 2)
            if event.kind == "schedule" or event.origin == EventOrigin.DISPATCHER:
                raise_band("blast_radius", min(4, current.blast_radius + 1))

            current.moved = moved
            bands = [
                current.data_exposure,
                current.tool_escalation,
                current.reversibility,
                current.authority_drift,
                current.context_integrity,
                current.blast_radius,
            ]
            maximum = max(bands)
            elevated = sum(value >= 2 for value in bands)
            current.markov_state = (maximum, elevated)
            current.p_violation = {
                horizon: self._markov.p_violation(current.markov_state, horizon)
                for horizon in (1, 3, 5)
            }
            self._states[event.run_id] = current
            return current.model_copy(deep=True)


safety_drift = SafetyDrift()
