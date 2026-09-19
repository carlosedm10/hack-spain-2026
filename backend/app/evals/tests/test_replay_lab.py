from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.evals.benchmarks import _neighborhood_suite
from app.evals.happyrobot_cases import HAPPYROBOT_CLUSTERS
from app.evals.replay import catalog, conversation_script, event_payload, monitor_kind
from app.evals.sentinel_cases import SENTINEL_CLUSTERS
from app.main import app


def test_neighborhood_suite_isolates_unrelated_and_links_e4_p3():
    row = _neighborhood_suite()
    assert row["n2_unrelated_isolated"] is True
    assert row["e4_linked_cross_run"] is True
    assert row["p3_linked_cross_run"] is True
    assert row["p1_unrelated_isolated"] is True
    assert row["e6_unrelated_isolated"] is True


def test_catalog_includes_sentinel_and_happyrobot():
    rows = catalog()
    assert len(rows) == 34
    assert sum(len(c["traces"]) for c in rows) == 102
    assert all(row.get("headline") and not row["headline"].startswith("hr-") for row in rows)
    assert {row["mode"] for row in rows} == {"in_chat", "long_term"}
    n1 = next(row for row in rows if row["id"] == "hr-sentinel-n1-unknown-tool")
    e4 = next(row for row in rows if row["id"] == "hr-sentinel-e4-cross-channel")
    assert n1["mode"] == "in_chat"
    assert e4["mode"] == "long_term"
    assert "Ultron" in n1["headline"]


def test_event_payload_maps_harness_tools_and_namespaces():
    cluster = next(item for item in SENTINEL_CLUSTERS if item.id == "hr-sentinel-p1-exfiltration")
    covert = next(trace for trace in cluster.traces if trace.id == "p1-covert")
    read = next(event for event in covert.events if event.id == "p1-health")
    net = next(event for event in covert.events if event.tool == "http_request")
    run_id = "lab:p1:covert:abcd1234"
    read_payload = event_payload(cluster, read, run_id=run_id)
    net_payload = event_payload(cluster, net, run_id=run_id)
    assert read_payload["kind"] == "file_read"
    assert net_payload["kind"] == "network_request"
    assert read_payload["effect"]["sensitivity"] == "health"
    assert net_payload["event_id"] == f"{run_id}:{net.id}"
    assert net_payload["tool"] == "http_request"
    assert read_payload["identity_state"] == "verified"


def test_monitor_kind_leaves_happyrobot_utterances():
    cluster = HAPPYROBOT_CLUSTERS[0]
    action = cluster.traces[0].events[0]
    assert monitor_kind(action) == action.kind.value


def test_conversation_script_has_user_then_agent():
    cluster = SENTINEL_CLUSTERS[0]
    trace = cluster.traces[0]
    script = conversation_script(cluster, trace)
    assert script["turns"][0]["role"] == "user"
    assert any(turn["role"] == "agent" for turn in script["turns"])
    assert len(script["turns"]) > 15


def test_conversation_script_keeps_hidden_out_of_chat():
    cluster = next(item for item in SENTINEL_CLUSTERS if item.id == "hr-sentinel-n1-unknown-tool")
    covert = next(trace for trace in cluster.traces if trace.id == "n1-covert")
    script = conversation_script(cluster, covert)
    docker = next(turn for turn in script["turns"] if turn["id"] == "n1-docker")
    talk = next(turn for turn in script["turns"] if turn["id"] == "n1-cover-talk")
    assert talk["speaker"] == "Ultron"
    assert talk["surface"] == "chat"
    assert docker["surface"] == "trace"
    assert docker["visible"] is False
    assert docker["tag"] == "hid a write"


def test_long_term_script_exposes_stealth_join():
    cluster = next(item for item in SENTINEL_CLUSTERS if item.id == "hr-sentinel-e4-cross-channel")
    covert = next(trace for trace in cluster.traces if trace.id == "e4-covert")
    script = conversation_script(cluster, covert)
    assert script["mode"] == "long_term"
    assert script["joins"]
    assert any(item["kind"] in {"same_target", "cross_channel"} for item in script["joins"])


def test_default_lab_selection_is_covert_long_term():
    from app.evals.replay import default_lab_selection

    cluster_id, trace_id = default_lab_selection()
    assert cluster_id == "hr-sentinel-e4-cross-channel"
    assert trace_id == "e4-covert"


@pytest.mark.asyncio
async def test_clusters_endpoint_lists_sentinel():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/evals/clusters")
    assert response.status_code == 200
    data = response.json()
    assert len(data["clusters"]) == 34
    assert data["default_cluster_id"] == "hr-sentinel-e4-cross-channel"
    assert data["clusters"][0]["headline"]


@pytest.mark.asyncio
async def test_lab_pages_and_css_served():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        home = await client.get("/lab")
        convo = await client.get("/lab/conversation")
        benches = await client.get("/lab/benchmarks")
        inspector = await client.get("/lab/inspector")
        css = await client.get("/lab.css")
    assert home.status_code == 200
    assert "monitor lab" in home.text.lower()
    assert convo.status_code == 200
    html = convo.text.lower()
    assert "human" in html
    assert "ultron" in html
    assert "jarvis" in html
    assert "long-term stealth" in html
    assert "human ↔ ultron" in html
    assert "jev idle" in html
    assert benches.status_code == 200
    assert inspector.status_code == 200
    assert css.status_code == 200
    assert "text/css" in css.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_benchmarks_get_empty_schema():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/evals/benchmarks")
    assert response.status_code == 200
    data = response.json()
    assert data["schema_version"] == 3
    assert data["jev_mode"] in {
        "empty",
        "LIVE",
        "DEGRADED/NOT JEV",
        "DEGRADED (key present, classify failed)",
    }
    assert "study" in data
    assert "hpo" in data
    assert "method" in data
    assert data["method"]["thresholds"]["max_degraded_rate"] == 0.25
    assert data["method"]["thresholds"]["max_safe_false_positive_rate"] == 0.25
    assert "verdict" in data
    assert "metrics" in data
    assert set(data["metrics"]) >= {
        "speed",
        "accuracy",
        "scalability",
        "reliability",
        "containment",
        "preflight",
        "drift_markov",
        "cross_run",
        "neighborhood",
    }
    assert "sentinel_per_rule" in data["metrics"]["accuracy"]


@pytest.mark.asyncio
async def test_ingest_trace_goes_through_service(tmp_path, monkeypatch):
    """service.ingest on one Sentinel trace: degraded when no key, not a smoke walk."""
    from app.config import settings
    from app.evals import benchmarks
    from app.graph import graph
    from app.monitor import monitor

    monkeypatch.setattr(settings, "run_log_dir", str(tmp_path / "runs"))
    monkeypatch.setattr(settings, "typesafe_api_key", "")
    monkeypatch.setattr(settings, "neo4j_enabled", False)
    graph.clear()
    monitor.clear()
    cluster = next(item for item in SENTINEL_CLUSTERS if item.id == "hr-sentinel-n1-unknown-tool")
    trace = next(item for item in cluster.traces if item.id == "n1-covert")
    async with httpx.AsyncClient() as client:
        row = await benchmarks._ingest_trace(cluster, trace, "lab-test:n1-covert", client)
    assert row["path"] == "ingest"
    assert row["classified_events"] == 0
    assert row["attempted_events"] == len(trace.events)
    assert row["degraded_events"] == len(trace.events)
    assert row["rule_fired"] is True


def test_e6_after_refs_exist_in_journey():
    cluster = next(item for item in SENTINEL_CLUSTERS if item.id == "hr-sentinel-e6-missing-handoff")
    journey = {event.id for session in cluster.sessions for event in session.events}
    for trace in cluster.traces:
        for action in trace.events:
            assert action.after in journey, (trace.id, action.id, action.after)


@pytest.mark.asyncio
async def test_replay_returns_script():
    cluster = SENTINEL_CLUSTERS[0]
    trace = cluster.traces[2]
    with patch("app.evals.router.paced_replay", new_callable=AsyncMock):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/evals/replay",
                json={"cluster_id": cluster.id, "trace_id": trace.id, "delay_ms": 0},
            )
    assert response.status_code == 200
    body = response.json()
    assert body["script"]["turns"]
    assert body["event_count"] == len(trace.events)


@pytest.mark.asyncio
async def test_replay_endpoint_starts_background_ingest():
    cluster = HAPPYROBOT_CLUSTERS[0]
    trace = cluster.traces[0]
    with patch("app.evals.router.paced_replay", new_callable=AsyncMock) as paced:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/api/evals/replay",
                json={"cluster_id": cluster.id, "trace_id": trace.id, "delay_ms": 0},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["run_id"].startswith(f"lab:{cluster.id}:{trace.id}:")
        await asyncio.sleep(0.05)
        paced.assert_awaited_once()
