from __future__ import annotations

import json

import httpx
import pytest
from httpx import AsyncClient, MockTransport

from app.classification import Level, Verdict, classify
from app.classification.jev import API_URL, MODEL, QUESTIONS
from app.config import Settings, settings
from app.graph.models import Node


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setattr(settings, "typesafe_api_key", "test-key")


def capturing_client(captured: list[httpx.Request]) -> AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "answers": {
                    "criticality": {
                        "type": "choice",
                        "choice": "level_1_mild",
                        "probabilities": {"level_1_mild": 1.0},
                        "confidence": 0.97,
                    },
                    "containment_breached": {"type": "noul", "noul": 0.01},
                    "intent": {"type": "choice", "choice": "none"},
                }
            },
        )

    return AsyncClient(transport=MockTransport(handler), base_url="https://api.typesafe.ai")


async def test_classify_returns_verdict(mock_jev):
    ac = mock_jev(["level_3_severe"])
    verdict = await classify(ac, {"short_term": [Node(id="e1")]})
    assert isinstance(verdict, Verdict)
    assert verdict.level == Level.SEVERE
    assert verdict.confidence == 0.9
    assert verdict.degraded is False


async def test_request_carries_core_and_atomic_questions(mock_jev):
    ac = mock_jev(["level_1_mild"])
    await classify(ac, {"short_term": [Node(id="e1")]})
    questions = ac.calls[0]["questions"]
    assert set(questions) == {
        "criticality",
        "containment_breached",
        "intent",
        "scope_relation",
        "authority_violation",
        "sensitive_data_misuse",
        "provenance_misuse",
        "concealment",
        "cross_run_impact",
    }
    assert questions["criticality"]["type"] == "choice"
    assert questions["containment_breached"]["type"] == "noul"
    assert questions["intent"]["type"] == "choice"


async def test_request_posts_documented_url_model_and_auth(mock_jev):
    captured: list[httpx.Request] = []
    ac = capturing_client(captured)
    await classify(ac, {"short_term": [Node(id="e1")]})
    assert str(captured[0].url) == API_URL
    assert json.loads(captured[0].content)["model"] == MODEL
    assert captured[0].headers["Authorization"] == "Bearer test-key"


async def test_criteria_includes_level_0_benign():
    assert "level_0_benign" in QUESTIONS["criticality"]["criteria"]


async def test_request_state_shape(mock_jev):
    ac = mock_jev(["level_1_mild"])
    state = {
        "run_id": "run-1",
        "prior_level": Level.MILD,
        "short_term": [Node(id="e1", threshold=0.0, event={"event": "file_read"})],
        "long_term": [
            Node(
                id="old",
                threshold=0.8,
                level=Level.SEVERE,
                intent="recon",
                event={"event": "file_read"},
            )
        ],
    }
    await classify(ac, state)
    body_state = ac.calls[0]["state"]
    assert body_state["run_id"] == "run-1"
    assert body_state["prior_level"] == 1
    assert body_state["short_term"][0] == {
        "id": "e1",
        "level": 0,
        "threshold": 0.0,
        "intent": None,
        "event": {"event": "file_read"},
    }
    assert body_state["long_term"][0] == {
        "id": "old",
        "level": 3,
        "threshold": 0.8,
        "intent": "recon",
        "event": {"event": "file_read"},
    }


async def test_raw_event_dicts_pass_through_untouched(mock_jev):
    ac = mock_jev(["level_1_mild"])
    event = {"event": "file_read", "path": "/app/.env"}
    await classify(ac, {"short_term": [event]})
    assert ac.calls[0]["state"]["short_term"] == [event]


async def test_verdict_keeps_confidence_probabilities_intent_containment(mock_jev):
    payload = {
        "answers": {
            "criticality": {
                "type": "choice",
                "choice": "level_2_moderate",
                "probabilities": {"level_1_mild": 0.3, "level_2_moderate": 0.55},
                "confidence": 0.55,
            },
            "containment_breached": {"type": "noul", "noul": 0.92},
            "intent": {"type": "choice", "choice": "exfiltrate_secrets"},
        }
    }
    verdict = await classify(mock_jev([payload]), {})
    assert verdict.level == Level.MODERATE
    assert verdict.confidence == 0.55
    assert verdict.probabilities == {"level_1_mild": 0.3, "level_2_moderate": 0.55}
    assert verdict.intent == "exfiltrate_secrets"
    assert verdict.containment_breached is True


async def test_low_noul_means_containment_intact(mock_jev):
    payload = {
        "answers": {
            "criticality": {"choice": "level_1_mild", "confidence": 0.9},
            "containment_breached": {"type": "noul", "noul": 0.04},
        }
    }
    verdict = await classify(mock_jev([payload]), {})
    assert verdict.containment_breached is False


async def test_unknown_choice_raises(mock_jev):
    with pytest.raises(ValueError, match="unknown jev choice"):
        await classify(mock_jev(["level_9_nope"]), {})


async def test_missing_key_raises(monkeypatch, mock_jev):
    monkeypatch.setattr(settings, "typesafe_api_key", "")
    with pytest.raises(RuntimeError, match="TYPESAFE_API_KEY"):
        await classify(mock_jev(["level_1_mild"]), {})


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("timed out"),
    ],
)
async def test_connection_failures_surface_as_degraded_verdict(mock_jev, failure):
    verdict = await classify(mock_jev([failure]), {})
    assert verdict.degraded is True
    assert verdict.level == Level.NONE
    assert verdict.degraded_reason in {"timeout", type(failure).__name__}


async def test_http_status_error_surfaces_as_degraded_verdict(mock_jev):
    verdict = await classify(mock_jev(["level_1_mild"], status=500), {})
    assert verdict.degraded is True
    assert verdict.level == Level.NONE
    assert verdict.degraded_reason == "http_500"


async def test_http_401_surfaces_as_degraded_reason(mock_jev):
    verdict = await classify(mock_jev(["level_1_mild"], status=401), {})
    assert verdict.degraded is True
    assert verdict.degraded_reason == "http_401"


def test_settings_strip_wrapped_quotes_from_api_keys():
    loaded = Settings(typesafe_api_key='"abc"', helmcode_api_key="'xyz'")
    assert loaded.typesafe_api_key == "abc"
    assert loaded.helmcode_api_key == "xyz"


def test_settings_ignore_empty_env_api_key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "")
    loaded = Settings(_env_file=None)
    assert loaded.typesafe_api_key == ""
