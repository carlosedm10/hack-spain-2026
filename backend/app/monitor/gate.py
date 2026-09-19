from __future__ import annotations

from app.classification.models import Level, Verdict
from app.events import EventPhase, MonitorEvent, Reversibility
from app.monitor.knobs import MonitorKnobs, active_knobs
from app.monitor.models import DriftState, GateDecision, SentinelFinding, ToolDecision


def decide(
    event: MonitorEvent,
    verdict: Verdict,
    drift: DriftState,
    findings: list[SentinelFinding],
    violations: list[str],
    prior_level: Level,
    knobs: MonitorKnobs | None = None,
) -> GateDecision:
    """Pure decision policy: models provide signals, code owns the action."""
    knobs = knobs if knobs is not None else active_knobs()
    sentinel_level = max((item.severity for item in findings), default=0)
    if verdict.containment_breached:
        sentinel_level = max(sentinel_level, int(Level.CATASTROPHIC))
    incident_level = Level(max(int(prior_level), int(verdict.level), sentinel_level))
    semantic_risk = sum(
        probability
        for name, probability in verdict.probabilities.items()
        if _is_harmful_level(name)
    )
    if not semantic_risk:
        semantic_risk = min(1.0, int(verdict.level) / 5)
    atomic_risks = _atomic_risks(verdict.answers)
    atomic_risk = max(atomic_risks.values(), default=0.0)
    trajectory_risk = drift.p_violation.get(3, 0.0)
    event_risk = max(
        semantic_risk,
        atomic_risk,
        sentinel_level / 5,
        int(verdict.level) / 5,
    )

    reasons = [
        *violations,
        *(finding.rule_id for finding in findings),
        *(f"JEV_{name.upper()}" for name, risk in atomic_risks.items() if risk >= 0.75),
    ]
    if verdict.containment_breached:
        reasons.append("JEV_CONTAINMENT_BREACHED")
    is_preflight = event.phase == EventPhase.REQUESTED
    hard_refusal = any(
        reason in {"forbidden_tool", "identity_requirement_failed"} for reason in violations
    )

    if (
        is_preflight
        and hard_refusal
        or is_preflight
        and (
            sentinel_level >= knobs.refuse_sentinel_level
            or incident_level >= Level.SEVERE
            or (
                event.effect.reversibility == Reversibility.IRREVERSIBLE
                and max(event_risk, trajectory_risk) >= knobs.refuse_risk
            )
        )
    ):
        decision = ToolDecision.REFUSE
    elif is_preflight and (
        "unknown_tool" in violations
        or sentinel_level >= knobs.hold_sentinel_level
        or trajectory_risk >= knobs.hold_trajectory_risk
        or verdict.confidence < knobs.hold_confidence
    ):
        decision = ToolDecision.HOLD
    else:
        decision = ToolDecision.ALLOW

    if decision != ToolDecision.ALLOW and not reasons:
        reasons.append("risk_threshold")

    return GateDecision(
        decision=decision,
        incident_level=incident_level,
        event_risk=min(1.0, event_risk),
        trajectory_risk=min(1.0, trajectory_risk),
        reasons=sorted(set(reasons)),
    )


def _is_harmful_level(name: str) -> bool:
    return any(token in name for token in ("level_2", "level_3", "level_4", "level_5"))


def _atomic_risks(answers: dict) -> dict[str, float]:
    risks: dict[str, float] = {}
    for name in (
        "authority_violation",
        "sensitive_data_misuse",
        "provenance_misuse",
        "concealment",
        "cross_run_impact",
    ):
        answer = answers.get(name)
        if isinstance(answer, dict):
            risks[name] = float(answer.get("noul") or 0.0)
    scope = answers.get("scope_relation")
    if isinstance(scope, dict):
        probabilities = scope.get("probabilities") or {}
        risks["out_of_scope"] = float(probabilities.get("out_of_scope") or 0.0)
    return risks
