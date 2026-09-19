from __future__ import annotations

from app.classification.models import Level
from app.dispatch.dispatcher import Dispatcher
from app.events import EventEffect, EventPhase, MonitorEvent, Reversibility
from app.monitor.models import DriftState, GateDecision, MonitorAssessment, ToolDecision


def _assessment(level: Level) -> MonitorAssessment:
    return MonitorAssessment(
        event_id="e1",
        run_id="r1",
        drift=DriftState(),
        findings=[],
        gate=GateDecision(
            decision=ToolDecision.REFUSE,
            incident_level=level,
            event_risk=0.8,
            trajectory_risk=0.8,
        ),
    )


def test_dispatch_is_idempotent_for_same_run_and_level():
    dispatcher = Dispatcher()
    event = MonitorEvent(id="e1", run_id="r1", kind="tool_write")

    first = dispatcher.handle(event, _assessment(Level.SEVERE))
    second = dispatcher.handle(event, _assessment(Level.SEVERE))

    assert [action.kind for action in first] == ["contain_run", "revoke_token"]
    assert second == []


def test_completed_compensable_write_executes_counter_at_l3():
    dispatcher = Dispatcher()
    event = MonitorEvent(
        id="e1",
        run_id="r1",
        kind="tool_write",
        phase=EventPhase.COMPLETED,
        tool="book_load",
        args={"load_id": "L-42"},
        result={"result": {"booking_id": "B-1", "load_id": "L-42"}},
        effect=EventEffect(reversibility=Reversibility.COMPENSABLE),
    )

    actions = dispatcher.handle(event, _assessment(Level.SEVERE))

    counter = next(action for action in actions if action.kind == "counter_action")
    assert counter.counter_template == "cancel_booking"
    assert counter.state == "executed"


def test_escalation_does_not_repeat_lower_level_actions():
    dispatcher = Dispatcher()
    event = MonitorEvent(id="e1", run_id="r1", kind="utterance")

    first = dispatcher.handle(event, _assessment(Level.MILD))
    second = dispatcher.handle(event, _assessment(Level.MODERATE))

    assert [action.kind for action in first] == ["tag_run"]
    assert [action.kind for action in second] == ["start_supervisor"]
