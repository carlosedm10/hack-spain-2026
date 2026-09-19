from __future__ import annotations

import importlib
import json

import pytest
from fastapi import HTTPException

from app.classification.models import Level
from app.events import MonitorEvent
from app.monitor.models import DriftState, GateDecision, MonitorAssessment, ToolDecision
from app.realtime.broker import RealtimeBroker, build_envelopes
from app.realtime.router import realtime_stream, snapshot


def _assessment() -> MonitorAssessment:
    return MonitorAssessment(
        event_id="e1",
        run_id="r1",
        drift=DriftState(authority_drift=3, markov_state=(3, 1)),
        findings=[],
        gate=GateDecision(
            decision=ToolDecision.REFUSE,
            incident_level=Level.SEVERE,
            event_risk=0.8,
            trajectory_risk=0.7,
        ),
        jev_answers={"authority_violation": {"type": "noul", "noul": 0.9}},
        jev_model="jev-1.13.0",
    )


def test_envelopes_are_ordered_idempotent_upserts():
    event = MonitorEvent(
        id="e1",
        run_id="r1",
        sequence=2,
        kind="tool_write",
        agent="agent:demo",
        tool="book_load",
        target="res:TMS/load/L-42",
        caused_by=["stimulus-1"],
    )

    envelopes = build_envelopes(event, _assessment(), previous_event_id="e0")

    assert [item["position"] for item in envelopes] == list(range(len(envelopes)))
    assert len({item["stream_id"] for item in envelopes}) == len(envelopes)
    assert all(item["source_event_id"] == "e1" for item in envelopes)
    event_types = {item["type"] for item in envelopes}
    assert {
        "graph.node.upserted",
        "graph.edge.upserted",
        "drift.updated",
        "jev.assessed",
        "gate.decided",
        "run.updated",
    } <= event_types
    edge_types = {
        item["data"]["edge"]["type"] for item in envelopes if item["type"] == "graph.edge.upserted"
    }
    assert {"NEXT", "CAUSED_BY", "HAS_ASSESSMENT", "TOUCHES"} <= edge_types


async def test_broker_fans_out_to_current_run_only(monkeypatch):
    broker = RealtimeBroker()

    async def no_persist(_):
        return None

    broker_module = importlib.import_module("app.realtime.broker")
    monkeypatch.setattr(broker_module.neo4j_graph, "persist_stream", no_persist)
    r1 = broker.subscribe("r1")
    r2 = broker.subscribe("r2")
    envelope = {
        "stream_id": "e1:0",
        "run_id": "r1",
        "type": "run.updated",
    }

    await broker.publish([envelope])

    assert await r1.get() == envelope
    assert r2.empty()


async def test_snapshot_hydrates_graph_and_latest_monitor_state(monkeypatch):
    async def graph(_):
        return {
            "nodes": [{"id": "e1", "labels": ["Event"], "properties": {"id": "e1"}}],
            "edges": [],
        }

    async def timeline(_):
        return [
            {
                "event": {
                    "id": "e1",
                    "sequence": 1,
                    "timestamp": "2026-09-19T00:00:00+00:00",
                },
                "assessment": {
                    "id": "e1:assessment",
                    "drift_json": DriftState(authority_drift=3).model_dump_json(),
                    "findings_json": "[]",
                    "gate_json": _assessment().gate.model_dump_json(),
                    "jev_answers_json": "{}",
                    "dispatch_actions_json": "[]",
                },
            }
        ]

    async def cursor(_):
        return "e1:8"

    router_module = importlib.import_module("app.realtime.router")
    monkeypatch.setattr(router_module.neo4j_graph, "graph", graph)
    monkeypatch.setattr(router_module.neo4j_graph, "timeline", timeline)
    monkeypatch.setattr(router_module.neo4j_graph, "latest_cursor", cursor)

    payload = await snapshot("r1")

    assert payload["cursor"] == "e1:8"
    assert payload["graph"]["nodes"][0]["id"] == "e1"
    assert payload["monitor"]["drift"]["authority_drift"] == 3
    assert payload["monitor"]["latest_gate"]["gate"]["decision"] == "refuse"


async def test_stream_replays_after_cursor_as_named_sse(monkeypatch):
    envelope = {
        "schema_version": 1,
        "stream_id": "e2:0",
        "type": "run.updated",
        "run_id": "r1",
        "source_event_id": "e2",
        "data": {"run": {"run_id": "r1"}},
    }

    async def cursor_exists(_, __):
        return True

    async def stream_after(_, __):
        return [envelope]

    router_module = importlib.import_module("app.realtime.router")
    monkeypatch.setattr(router_module.neo4j_graph, "cursor_exists", cursor_exists)
    monkeypatch.setattr(router_module.neo4j_graph, "stream_after", stream_after)

    response = await realtime_stream("r1", after="e1:8", last_event_id=None)
    iterator = response.body_iterator
    try:
        chunk = await anext(iterator)
    finally:
        await iterator.aclose()
    if isinstance(chunk, bytes):
        chunk = chunk.decode()
    assert "id: e2:0" in chunk
    assert "event: run.updated" in chunk
    data = next(line for line in chunk.splitlines() if line.startswith("data: "))
    assert json.loads(data.removeprefix("data: ")) == envelope


async def test_unknown_realtime_cursor_requires_new_snapshot(monkeypatch):
    async def cursor_exists(_, __):
        return False

    router_module = importlib.import_module("app.realtime.router")
    monkeypatch.setattr(router_module.neo4j_graph, "cursor_exists", cursor_exists)

    with pytest.raises(HTTPException) as exc:
        await realtime_stream("r1", after="expired", last_event_id=None)
    assert exc.value.status_code == 409
