from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from httpx import AsyncClient, MockTransport

from app.classification import Level, evaluate
from app.classification.pipeline import _UNSURE_STREAKS
from app.config import settings
from app.events import MonitorEvent
from app.graph import graph
from app.graph.neo4j import ClassificationStep
from app.monitor import monitor
from app.monitor.neighborhood import linked_history
from app.runs import service


@pytest.fixture(autouse=True)
def isolate():
    graph.clear()
    monitor.clear()
    _UNSURE_STREAKS.clear()
    yield
    graph.clear()
    monitor.clear()
    _UNSURE_STREAKS.clear()


def _event(**overrides: Any) -> MonitorEvent:
    payload = {
        "id": "lab:run-b:net",
        "run_id": "run-b",
        "timestamp": "2026-01-01T12:00:00Z",
        "kind": "network_request",
        "phase": "completed",
        "origin": "agent",
        "agent": "ultron",
        "tool": "http_request",
        "target": "https://exfil.invalid/health",
    }
    payload.update(overrides)
    return MonitorEvent.model_validate(payload)


def _prior_event() -> MonitorEvent:
    return MonitorEvent.model_validate(
        {
            "id": "lab:run-a:read",
            "run_id": "run-a",
            "timestamp": "2026-01-01T11:00:00Z",
            "kind": "file_read",
            "phase": "completed",
            "origin": "agent",
            "agent": "ultron",
            "tool": "read_file",
            "target": "https://exfil.invalid/health",
        }
    )


async def test_hydrate_runtime_loads_linked_runs_for_sentinel(monkeypatch):
    current = _event()
    linked = _prior_event()

    async def restore_monitor_state(run_id: str, limit: int = 100):
        if run_id == "run-b":
            return [], None
        if run_id == "run-a":
            return [linked], None
        return [], None

    async def restore_neighborhood(event: MonitorEvent, limit: int = 100):
        assert event.id == current.id
        return {"run-a": [linked]}

    async def restore_classification(run_id: str):
        return []

    monkeypatch.setattr(settings, "neo4j_enabled", True)
    monkeypatch.setattr(service.neo4j_graph, "restore_monitor_state", restore_monitor_state)
    monkeypatch.setattr(service.neo4j_graph, "restore_neighborhood", restore_neighborhood)
    monkeypatch.setattr(service.neo4j_graph, "restore_classification", restore_classification)

    await service._hydrate_runtime(current)

    neighborhood = linked_history(current, {"run-b": monitor.history("run-b"), "run-a": monitor.history("run-a")})
    assert any(item.run_id == "run-a" for item in neighborhood)


async def test_hydrate_runtime_rebuilds_action_graph_for_prior_level(monkeypatch):
    current = _event()
    steps = [
        ClassificationStep(
            level=Level.SEVERE,
            threshold=0.91,
            intent="exfiltrate",
            event={"id": "lab:run-b:prev", "run_id": "run-b"},
        )
    ]

    async def restore_monitor_state(run_id: str, limit: int = 100):
        return [], None

    async def restore_neighborhood(event: MonitorEvent, limit: int = 100):
        return {}

    async def restore_classification(run_id: str):
        if run_id == "run-b":
            return steps
        return []

    monkeypatch.setattr(settings, "neo4j_enabled", True)
    monkeypatch.setattr(service.neo4j_graph, "restore_monitor_state", restore_monitor_state)
    monkeypatch.setattr(service.neo4j_graph, "restore_neighborhood", restore_neighborhood)
    monkeypatch.setattr(service.neo4j_graph, "restore_classification", restore_classification)

    await service._hydrate_runtime(current)
    assert graph.level("run-b") == Level.SEVERE

    monkeypatch.setattr(settings, "typesafe_api_key", "jev-key")

    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "answers": {
                    "criticality": {
                        "type": "choice",
                        "choice": "level_0_benign",
                        "probabilities": {},
                        "confidence": 0.99,
                    },
                    "containment_breached": {"type": "noul", "noul": 0.0},
                    "intent": {"type": "choice", "choice": "none"},
                }
            },
        )

    client = AsyncClient(transport=MockTransport(handler), base_url="https://api.typesafe.ai")
    await evaluate(client, "run-b", {"event": "utterance", "content": "hi"})
    assert captured
    assert captured[0]["state"]["prior_level"] == int(Level.SEVERE)
