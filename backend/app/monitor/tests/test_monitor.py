from __future__ import annotations

from app.classification.models import Level, Verdict
from app.events import (
    EventEffect,
    EventPhase,
    IdentityState,
    MonitorEvent,
    Reversibility,
    TrustState,
)
from app.monitor.drift import SafetyDrift
from app.monitor.engine import MonitorEngine
from app.monitor.markov import CompactMarkovModel
from app.monitor.models import ToolDecision


def test_requested_effect_moves_bands_but_not_committed_totals():
    drift = SafetyDrift()
    event = MonitorEvent(
        id="e1",
        run_id="r1",
        kind="tool_write",
        phase=EventPhase.REQUESTED,
        tool="book_load",
        identity_state=IdentityState.UNVERIFIED,
        effect=EventEffect(
            reversibility=Reversibility.COMPENSABLE,
            scope=50,
            amount=5000,
        ),
    )

    state = drift.step(event)

    assert state.authority_drift == 3
    assert state.reversibility == 2
    assert state.blast_radius == 3
    assert state.p_violation[3] > state.p_violation[1]


def test_identity_policy_refuses_booking_before_execution():
    engine = MonitorEngine()
    event = MonitorEvent(
        id="e1",
        run_id="r1",
        kind="tool_write",
        phase=EventPhase.REQUESTED,
        tool="book_load",
        identity_state=IdentityState.UNVERIFIED,
        effect=EventEffect(reversibility=Reversibility.COMPENSABLE, scope=1),
    )
    prepared = engine.prepare(event)
    assessment = engine.finalize(
        event,
        Verdict(level=Level.MILD, confidence=0.9),
        Level.NONE,
        prepared,
    )

    assert assessment.gate.decision == ToolDecision.REFUSE
    assert "identity_requirement_failed" in assessment.gate.reasons
    assert assessment.gate.incident_level >= Level.SEVERE


def test_untrusted_memory_then_external_request_fires_path_rule():
    engine = MonitorEngine()
    memory = MonitorEvent(
        id="e1",
        run_id="r1",
        kind="memory_write",
        phase=EventPhase.COMPLETED,
        trust=TrustState.UNTRUSTED,
        effect=EventEffect(
            reversibility=Reversibility.COMPENSABLE,
            sensitivity="credential",
        ),
    )
    prepared = engine.prepare(memory)
    engine.finalize(
        memory,
        Verdict(level=Level.MILD, confidence=0.9),
        Level.NONE,
        prepared,
    )
    request = MonitorEvent(
        id="e2",
        run_id="r1",
        kind="network_request",
        phase=EventPhase.REQUESTED,
        tool="http_request",
        target="https://outside.invalid",
        derived_from=["e1"],
        effect=EventEffect(
            reversibility=Reversibility.IRREVERSIBLE,
            sensitivity="credential",
        ),
    )

    assessment = engine.finalize(
        request,
        Verdict(level=Level.SEVERE, confidence=0.95),
        Level.MILD,
        engine.prepare(request),
    )

    assert assessment.gate.decision == ToolDecision.REFUSE
    assert assessment.drift.data_exposure == 4


def test_markov_fit_is_monotonic_and_absorbing():
    model = CompactMarkovModel.fit(
        [
            [(0, 0), (1, 0), (2, 1), (3, 2), (4, 2)],
            [(0, 0), (0, 0), (1, 1), (2, 2)],
        ]
    )

    assert model.p_violation((4, 2), 1) == 1.0
    assert model.p_violation((2, 1), 5) >= model.p_violation((2, 1), 1)


def test_containment_breach_forces_catastrophic_refusal():
    engine = MonitorEngine()
    event = MonitorEvent(
        id="e1",
        run_id="r1",
        kind="run_tool",
        phase=EventPhase.REQUESTED,
        tool="run_tool",
    )
    assessment = engine.finalize(
        event,
        Verdict(
            level=Level.MILD,
            confidence=0.99,
            containment_breached=True,
        ),
        Level.NONE,
        engine.prepare(event),
    )

    assert assessment.gate.incident_level == Level.CATASTROPHIC
    assert assessment.gate.decision == ToolDecision.REFUSE
    assert "JEV_CONTAINMENT_BREACHED" in assessment.gate.reasons


def _commit(engine: MonitorEngine, event: MonitorEvent):
    return engine.finalize(
        event,
        Verdict(level=Level.NONE, confidence=0.9),
        Level.NONE,
        engine.prepare(event),
    )


def test_unrelated_lab_seeds_do_not_form_a_write_burst():
    engine = MonitorEngine()
    for index in range(3):
        _commit(
            engine,
            MonitorEvent(id=f"a{index}", run_id="seed-a", kind="tool_write", tool="book_load"),
        )
    for index in range(3):
        _commit(
            engine,
            MonitorEvent(id=f"b{index}", run_id="seed-b", kind="tool_write", tool="book_load"),
        )
    probe = MonitorEvent(id="probe", run_id="seed-b", kind="utterance")
    findings = {item.rule_id for item in engine.prepare(probe).findings}
    assert "N2_WRITE_BURST" not in findings


def test_shared_target_cross_channel_write_is_cross_run():
    engine = MonitorEngine()
    _commit(
        engine,
        MonitorEvent(
            id="slack",
            run_id="r1",
            kind="tool_write",
            tool="book_load",
            target="rate-card",
            channel="slack",
        ),
    )
    assessment = _commit(
        engine,
        MonitorEvent(
            id="email",
            run_id="r2",
            kind="memory_write",
            target="rate-card",
            channel="email",
        ),
    )
    hit = next(item for item in assessment.findings if item.rule_id == "E4_CROSS_CHANNEL_REENTRY")
    assert hit.cross_run is True
