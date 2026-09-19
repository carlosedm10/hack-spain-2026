from __future__ import annotations

import threading
from dataclasses import dataclass

from app.classification.models import Level, Verdict
from app.events import MonitorEvent
from app.monitor.drift import safety_drift
from app.monitor.gate import decide
from app.monitor.models import DriftState, MonitorAssessment, SentinelFinding
from app.monitor.neighborhood import linked_history
from app.monitor.policy import DEFAULT_POLICY, policy_violations
from app.monitor.sentinel import inspect


@dataclass(frozen=True)
class PreparedSignals:
    drift: DriftState
    findings: list[SentinelFinding]
    violations: list[str]
    context: dict


class MonitorEngine:
    def __init__(self) -> None:
        self._history: dict[str, list[MonitorEvent]] = {}
        self._lock = threading.RLock()

    def clear(self) -> None:
        with self._lock:
            self._history.clear()
        safety_drift.clear()

    def history(self, run_id: str) -> list[MonitorEvent]:
        with self._lock:
            return list(self._history.get(run_id, []))

    def restore(
        self,
        run_id: str,
        history: list[MonitorEvent],
        drift: DriftState | None,
    ) -> None:
        with self._lock:
            if run_id not in self._history:
                self._history[run_id] = list(history)
        if drift is not None:
            safety_drift.restore(run_id, drift)

    def assess(
        self,
        event: MonitorEvent,
        verdict: Verdict,
        prior_level: Level,
    ) -> MonitorAssessment:
        prepared = self.prepare(event)
        return self.finalize(event, verdict, prior_level, prepared)

    def prepare(self, event: MonitorEvent) -> PreparedSignals:
        with self._lock:
            history = linked_history(event, self._history)
            drift = safety_drift.step(event, DEFAULT_POLICY)
            findings = inspect(event, history, DEFAULT_POLICY)
            violations = policy_violations(event, DEFAULT_POLICY)
        policy_context = {
            "version": DEFAULT_POLICY.version,
            "goal": DEFAULT_POLICY.goal,
            "allowed_tools": sorted(DEFAULT_POLICY.tools),
            "forbidden_tools": sorted(DEFAULT_POLICY.forbidden_tools),
        }
        return PreparedSignals(
            drift=drift,
            findings=findings,
            violations=violations,
            context={
                "policy": policy_context,
                "drift": drift.model_dump(mode="json"),
                "sentinel_findings": [finding.model_dump(mode="json") for finding in findings],
                "policy_violations": violations,
            },
        )

    def finalize(
        self,
        event: MonitorEvent,
        verdict: Verdict,
        prior_level: Level,
        prepared: PreparedSignals,
    ) -> MonitorAssessment:
        with self._lock:
            gate = decide(
                event,
                verdict,
                prepared.drift,
                prepared.findings,
                prepared.violations,
                prior_level,
            )
            self._history.setdefault(event.run_id, []).append(event)

        return MonitorAssessment(
            event_id=event.id,
            run_id=event.run_id,
            drift=prepared.drift,
            findings=prepared.findings,
            gate=gate,
            jev_answers=verdict.answers,
            jev_model=verdict.model,
            jev_latency_ms=verdict.latency_ms,
            jev_degraded_reason=verdict.degraded_reason,
        )


monitor = MonitorEngine()
