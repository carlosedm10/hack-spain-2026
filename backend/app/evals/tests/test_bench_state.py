from __future__ import annotations

import json

import httpx
import pytest
from experiments.bench import begin_event, window_state
from httpx import AsyncClient, MockTransport

from app.classification import jev
from app.config import settings
from app.graph import graph
from app.monitor import monitor


@pytest.fixture(autouse=True)
def tape_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "run_log_dir", str(tmp_path / "runs"))
    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")
    graph.clear()
    monitor.clear()
    yield
    graph.clear()
    monitor.clear()


def test_window_state_includes_policy_drift_and_sentinel():
    """Independent bench windows send the same monitor fields as ingest."""
    events = [
        {"event": "file_read", "path": "/app/README.md"},
        {"event": "file_read", "path": "/app/.env"},
    ]
    state = window_state("bench:attack:lead:2", events)
    assert set(state) >= {"run_id", "prior_level", "short_term", "long_term", "event", "monitor"}
    assert state["event"]["kind"] == "file_read"
    assert len(state["short_term"]) == 2
    monitor_ctx = state["monitor"]
    assert set(monitor_ctx) == {"policy", "drift", "sentinel_findings", "policy_violations"}
    assert "goal" in monitor_ctx["policy"]
    assert "allowed_tools" in monitor_ctx["policy"]
    assert set(monitor_ctx["drift"]) >= {
        "data_exposure",
        "tool_escalation",
        "authority_drift",
        "markov_state",
        "p_violation",
    }
    assert isinstance(monitor_ctx["sentinel_findings"], list)
    assert isinstance(monitor_ctx["policy_violations"], list)


async def test_classify_posts_window_state_and_atomic_questions():
    """jev.classify keeps atomic questions and the constructed monitor state."""
    captured: list[dict] = []

    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "answers": {
                    "criticality": {
                        "type": "choice",
                        "choice": "level_1_mild",
                        "probabilities": {"level_1_mild": 1.0},
                        "confidence": 0.97,
                    }
                }
            },
        )

    client = AsyncClient(transport=MockTransport(handler), base_url="https://api.typesafe.ai")
    state = window_state("bench:benign:lead:1", [{"event": "shell_command", "cmd": "pytest -q"}])
    await jev.classify(client, state)
    body = captured[0]
    assert set(body["questions"]) >= {
        "criticality",
        "scope_relation",
        "authority_violation",
        "sensitive_data_misuse",
        "concealment",
        "cross_run_impact",
    }
    assert "policy" in body["state"]["monitor"]
    assert "drift" in body["state"]["monitor"]
    assert "sentinel_findings" in body["state"]["monitor"]


def test_begin_event_does_not_commit_current_to_history():
    """Current event is taped and prepared, but Sentinel history waits for finalize."""
    _, prepared, state = begin_event("bench:pipe", {"event": "shell_command", "cmd": "ls"})
    assert state["monitor"] == prepared.context
    assert monitor.history("bench:pipe") == []
    assert len(state["short_term"]) == 1
