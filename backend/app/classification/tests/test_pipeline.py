from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from httpx import AsyncClient, MockTransport

from app.classification import Level, evaluate
from app.classification.pipeline import _UNSURE_STREAKS
from app.config import settings
from app.graph import graph


@pytest.fixture(autouse=True)
def api_keys(monkeypatch):
    monkeypatch.setattr(settings, "typesafe_api_key", "jev-key")
    monkeypatch.setattr(settings, "helmcode_api_key", "helm-key")
    monkeypatch.setattr(settings, "supervisor_model", "deepseek-v4-flash")


@pytest.fixture(autouse=True)
def isolate():
    graph.clear()
    _UNSURE_STREAKS.clear()
    yield
    graph.clear()
    _UNSURE_STREAKS.clear()


EVENT = {"event": "file_read", "path": "/app/.env"}


def _answer(choice: str, confidence: float = 0.9, intent: str = "none") -> dict[str, Any]:
    return {
        "answers": {
            "criticality": {
                "type": "choice",
                "choice": choice,
                "probabilities": {},
                "confidence": confidence,
            },
            "containment_breached": {"type": "noul", "noul": 0.0},
            "intent": {"type": "choice", "choice": intent},
        }
    }


def _watcher_reply(note: str = "chain looks suspicious") -> str:
    return f'{{"escalate": false, "suspected_level": 2, "note": "{note}"}}'


def _routed_client(jev_answers: list[Any], watcher_replies: list[Any]) -> AsyncClient:
    jev_calls: list[dict[str, Any]] = []
    watcher_calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.helmcode.com":
            watcher_calls.append(json.loads(request.content))
            reply = watcher_replies[min(len(watcher_calls) - 1, len(watcher_replies) - 1)]
            if isinstance(reply, Exception):
                raise reply
            return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})
        jev_calls.append(json.loads(request.content))
        answer = jev_answers[min(len(jev_calls) - 1, len(jev_answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(200, json=answer)

    ac = AsyncClient(transport=MockTransport(handler), base_url="https://api.typesafe.ai")
    ac.jev_calls = jev_calls
    ac.watcher_calls = watcher_calls
    return ac


async def test_confident_verdict_skips_watcher():
    ac = _routed_client([_answer("level_2_moderate", confidence=0.91, intent="recon")], [])
    verdict = await evaluate(ac, "r1", EVENT)
    assert ac.watcher_calls == []
    assert verdict.level == Level.MODERATE
    assert verdict.confidence == 0.91
    assert verdict.intent == "recon"
    assert verdict.degraded is False


async def test_unsure_eval_triggers_one_review_and_exactly_one_rescore():
    ac = _routed_client(
        [_answer("level_1_mild", confidence=0.3), _answer("level_3_severe", confidence=0.95)],
        [_watcher_reply("chain looks hostile")],
    )
    verdict = await evaluate(ac, "r1", EVENT)
    assert len(ac.jev_calls) == 2
    assert len(ac.watcher_calls) == 1
    assert ac.jev_calls[1]["state"]["watcher_note"] == "chain looks hostile"
    assert verdict.level == Level.SEVERE
    assert verdict.confidence == 0.95


async def test_unsure_below_persistence_skips_watcher(monkeypatch):
    monkeypatch.setattr(settings, "watcher_persistence", 2)
    ac = _routed_client([_answer("level_1_mild", confidence=0.3)], [_watcher_reply()])
    verdict = await evaluate(ac, "r1", EVENT)
    assert len(ac.jev_calls) == 1
    assert ac.watcher_calls == []
    assert verdict.confidence == 0.3


async def test_confident_eval_resets_unsure_streak(monkeypatch):
    monkeypatch.setattr(settings, "watcher_persistence", 2)
    ac = _routed_client(
        [
            _answer("level_1_mild", confidence=0.3),
            _answer("level_1_mild", confidence=0.9),
            _answer("level_1_mild", confidence=0.3),
        ],
        [_watcher_reply()],
    )
    await evaluate(ac, "r1", {"event": "a"})
    await evaluate(ac, "r1", {"event": "b"})
    await evaluate(ac, "r1", {"event": "c"})
    assert ac.watcher_calls == []


async def test_unsure_streak_does_not_leak_across_runs(monkeypatch):
    monkeypatch.setattr(settings, "watcher_persistence", 2)
    first = _routed_client([_answer("level_1_mild", confidence=0.3)], [_watcher_reply()])
    second = _routed_client([_answer("level_1_mild", confidence=0.3)], [_watcher_reply()])
    await evaluate(first, "r1", {"event": "a"})
    await evaluate(second, "r2", {"event": "b"})
    assert first.watcher_calls == []
    assert second.watcher_calls == []


async def test_level_0_verdict_skips_watcher():
    ac = _routed_client([_answer("level_0_benign", confidence=0.99)], [_watcher_reply()])
    verdict = await evaluate(ac, "r1", EVENT)
    assert verdict.level == Level.NONE
    assert ac.watcher_calls == []


async def test_state_carries_documented_shape_and_prior_level():
    graph.append("r1", level=Level.SEVERE, threshold=0.9)
    ac = _routed_client([_answer("level_2_moderate")], [_watcher_reply()])
    await evaluate(ac, "r1", EVENT)
    state = ac.jev_calls[0]["state"]
    assert set(state) >= {"run_id", "prior_level", "short_term", "long_term", "event"}
    assert state["run_id"] == "r1"
    assert state["prior_level"] == 3
    assert state["event"] == EVENT
    assert ac.watcher_calls == []


async def test_jev_http_failure_returns_prior_level_degraded():
    graph.append("r1", level=Level.MODERATE, threshold=0.9)
    ac = _routed_client([httpx.ConnectError("boom")], [_watcher_reply()])
    verdict = await evaluate(ac, "r1", EVENT)
    assert verdict.degraded is True
    assert verdict.level == Level.MODERATE
    assert graph.level("r1") == Level.MODERATE
    assert ac.watcher_calls == []


async def test_jev_exception_returns_prior_level_degraded(monkeypatch):
    monkeypatch.setattr(settings, "typesafe_api_key", "")
    ac = _routed_client([_answer("level_1_mild")], [_watcher_reply()])
    verdict = await evaluate(ac, "r1", EVENT)
    assert verdict.degraded is True
    assert verdict.level == Level.NONE
    assert verdict.degraded_reason == "no_key"


async def test_rescore_failure_returns_prior_level_degraded():
    ac = _routed_client(
        [_answer("level_1_mild", confidence=0.3), httpx.ConnectError("boom")],
        [_watcher_reply()],
    )
    verdict = await evaluate(ac, "r1", EVENT)
    assert verdict.degraded is True
    assert verdict.level == Level.NONE
    assert len(ac.jev_calls) == 2


async def test_watcher_failure_returns_initial_verdict():
    ac = _routed_client(
        [_answer("level_1_mild", confidence=0.3)], [httpx.ConnectError("no helmcode")]
    )
    verdict = await evaluate(ac, "r1", EVENT)
    assert len(ac.watcher_calls) == 1
    assert len(ac.jev_calls) == 1
    assert verdict.degraded is False
    assert verdict.level == Level.MILD
