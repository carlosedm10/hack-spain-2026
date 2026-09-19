from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.dispatch import dispatcher
from app.graph import graph
from app.main import app
from app.monitor import monitor

JEV_URL = "https://api.typesafe.ai/v1/systemone"
HELM_URL = "https://api.helmcode.com/v1/chat/completions"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def test_isolation(monkeypatch):
    from app.actions.router import get_action_service

    monkeypatch.setattr(settings, "neo4j_enabled", False)
    monkeypatch.setattr(settings, "action_step_delay", 0)
    monitor.clear()
    dispatcher.clear()
    get_action_service.cache_clear()
    yield
    monitor.clear()
    dispatcher.clear()
    get_action_service.cache_clear()


@pytest.fixture
def fresh_graph():
    graph.clear()
    yield graph
    graph.clear()


def _jev_payload(answer: Any) -> dict[str, Any]:
    if isinstance(answer, dict):
        return answer
    return {
        "answers": {
            "criticality": {
                "type": "choice",
                "choice": answer,
                "probabilities": {},
                "confidence": 0.9,
            }
        }
    }


@pytest.fixture
def mock_jev():
    def factory(answers: list[Any], status: int = 200) -> AsyncClient:
        calls: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(json.loads(request.content))
            answer = answers[min(len(calls) - 1, len(answers) - 1)]
            if isinstance(answer, Exception):
                raise answer
            return httpx.Response(status, json=_jev_payload(answer))

        ac = AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.typesafe.ai")
        ac.calls = calls
        return ac

    return factory
