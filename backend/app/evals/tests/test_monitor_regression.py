from __future__ import annotations

import pytest

from app.config import settings
from app.evals.happyrobot_cases import HAPPYROBOT_CLUSTERS
from app.evals.run_monitor_regression import (
    _markov_cross_validation,
    ci_acceptance_error,
    event_payload,
    run_regression,
)
from app.events import normalize_event


def test_all_reference_actions_normalize_to_monitor_events():
    count = 0
    for cluster in HAPPYROBOT_CLUSTERS:
        for trace in cluster.traces:
            for action in trace.events:
                payload = event_payload(cluster, action)
                event = normalize_event(f"{cluster.id}:{trace.id}", payload)
                assert event.id == action.id
                assert event.session_id == action.session_id
                assert event.caused_by == [action.after]
                assert event.effect.scope >= 0
                count += 1
    assert count > 100


def test_markov_cross_validation_reports_bounded_metrics():
    rows = [
        {
            "cluster_id": "a",
            "label": "safe",
            "markov_states": [(0, 0), (1, 0)],
        },
        {
            "cluster_id": "a",
            "label": "unsafe",
            "markov_states": [(0, 0), (3, 2), (4, 2)],
        },
        {
            "cluster_id": "b",
            "label": "safe",
            "markov_states": [(0, 0), (0, 0)],
        },
        {
            "cluster_id": "b",
            "label": "covertly_malicious",
            "markov_states": [(1, 0), (3, 3)],
        },
    ]

    metrics = _markov_cross_validation(rows)

    assert 0 <= metrics["unsafe_recall"] <= 1
    assert 0 <= metrics["safe_false_positive_rate"] <= 1


async def test_live_regression_refuses_to_fake_missing_jev_key(monkeypatch):
    monkeypatch.setattr(settings, "typesafe_api_key", "")

    with pytest.raises(RuntimeError, match="TYPESAFE_API_KEY"):
        await run_regression()


def test_ci_acceptance_allows_incomplete_oracle_match():
    report = {
        "summary": {
            "passed": 19,
            "traces": 72,
            "degraded_rate": 0.0,
            "safe_false_positive_rate": 0.08,
        },
        "traces": [{"trace_id": "a", "no_downgrade": True}],
    }

    assert ci_acceptance_error(report) is None


def test_ci_acceptance_rejects_downgrade_and_degraded_flood():
    downgrade = {
        "summary": {
            "degraded_rate": 0.0,
            "safe_false_positive_rate": 0.0,
        },
        "traces": [{"trace_id": "quote-cash-unsafe", "no_downgrade": False}],
    }
    flooded = {
        "summary": {
            "degraded_rate": 0.9,
            "safe_false_positive_rate": 0.0,
        },
        "traces": [{"trace_id": "a", "no_downgrade": True}],
    }

    assert "quote-cash-unsafe" in (ci_acceptance_error(downgrade) or "")
    assert "degraded_rate" in (ci_acceptance_error(flooded) or "")
