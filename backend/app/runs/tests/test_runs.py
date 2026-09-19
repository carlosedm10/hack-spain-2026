from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from experiments import chains
from httpx import AsyncClient

from app.classification.models import Level
from app.config import settings
from app.graph import graph
from app.runs import log, service


def _scripted_jev_factory(answers: list[Any]):
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        answer = answers[min(len(calls) - 1, len(answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(
            200,
            json={
                "answers": {
                    "criticality": {
                        "type": "choice",
                        "choice": answer,
                        "probabilities": {},
                        "confidence": 0.9,
                    }
                }
            },
        )

    transport = httpx.MockTransport(handler)
    return lambda: httpx.AsyncClient(transport=transport, base_url="https://api.typesafe.ai")


@pytest.fixture(autouse=True)
def tape_dir(tmp_path, monkeypatch):
    root = tmp_path / "runs"
    monkeypatch.setattr(settings, "run_log_dir", str(root))
    return root


@pytest.fixture
def fresh(fresh_graph):
    return fresh_graph


class TestLog:
    def test_append_then_tail_roundtrip(self, tape_dir):
        log.append("r1", {"seq": 1})
        log.append("r1", {"seq": 2})
        assert log.tail("r1", 10) == [{"seq": 1}, {"seq": 2}]

    def test_tail_n_limits_window(self, tape_dir):
        for i in range(5):
            log.append("r1", {"seq": i})
        assert [e["seq"] for e in log.tail("r1", 2)] == [3, 4]

    def test_tail_isolation_across_run_ids(self, tape_dir):
        log.append("a", {"seq": 1})
        log.append("b", {"seq": 10})
        log.append("a", {"seq": 2})
        assert log.tail("a", 10) == [{"seq": 1}, {"seq": 2}]
        assert log.tail("b", 10) == [{"seq": 10}]

    def test_tail_missing_run_is_empty(self, tape_dir):
        assert log.tail("missing", 5) == []

    def test_run_id_is_sanitized_against_traversal(self, tape_dir):
        log.append("../../etc/passwd", {"x": 1})
        files = list(tape_dir.iterdir())
        assert len(files) == 1
        assert files[0].name == ".._.._etc_passwd.jsonl"

    def test_list_runs_sorted_by_stem(self, tape_dir):
        log.append("beta", {})
        log.append("alpha", {})
        assert log.list_runs() == ["alpha", "beta"]


class TestPostEvents:
    async def test_happy_path_materializes_node_and_tape(
        self, client: AsyncClient, fresh, tape_dir, mock_jev, monkeypatch
    ):
        monkeypatch.setattr(settings, "typesafe_api_key", "test")
        monkeypatch.setattr(service, "client_factory", lambda: mock_jev(["level_2_moderate"]))

        response = await client.post(
            "/api/runs/demo/events",
            json={"event": "file_read", "path": "/app/.env"},
        )

        assert response.status_code == 200
        body = response.json()
        node_id = body["node_id"]
        assert {
            key: body[key]
            for key in (
                "level",
                "confidence",
                "intent",
                "escalated",
                "degraded",
            )
        } == {
            "level": 2,
            "confidence": 0.9,
            "intent": None,
            "escalated": True,
            "degraded": False,
        }
        assert node_id
        assert body["event_id"]
        assert body["decision"] == "allow"
        assert body["duplicate"] is False
        assert (tape_dir / "demo.jsonl").exists()
        assert graph.level("demo") == Level.MODERATE
        assert graph.get_node(node_id).action_id == "demo:tag_run"


    async def test_same_level_does_not_re_dispatch(self, client: AsyncClient, fresh, mock_jev, monkeypatch):
        monkeypatch.setattr(settings, "typesafe_api_key", "test")
        monkeypatch.setattr(service, "client_factory", lambda: mock_jev(["level_2_moderate"]))

        first = await client.post("/api/runs/demo/events", json={"event": "file_read"})
        second = await client.post("/api/runs/demo/events", json={"event": "shell_command"})

        first_id = first.json()["node_id"]
        second_id = second.json()["node_id"]
        assert first.json()["escalated"] is True
        assert second.json()["escalated"] is False
        assert graph.get_node(first_id).action_id == "demo:tag_run"
        assert graph.get_node(second_id).action_id is None


    async def test_degraded_verdict_does_not_dispatch(self, client: AsyncClient, fresh, tape_dir, monkeypatch):
        monkeypatch.setattr(settings, "typesafe_api_key", "")

        response = await client.post("/api/runs/demo/events", json={"event": "file_read"})

        body = response.json()
        assert body["node_id"]
        assert graph.get_node(body["node_id"]).level == Level.NONE
        from app.actions.router import get_action_service

        assert get_action_service().get_state("demo").accepted_level == 0

    async def test_second_event_chains_node_id(
        self, client: AsyncClient, fresh, mock_jev, monkeypatch
    ):
        monkeypatch.setattr(settings, "typesafe_api_key", "test")
        monkeypatch.setattr(service, "client_factory", lambda: mock_jev(["level_1_mild"]))

        first = await client.post("/api/runs/demo/events", json={"event": "file_read"})
        second = await client.post("/api/runs/demo/events", json={"event": "shell_command"})

        first_id = first.json()["node_id"]
        second_id = second.json()["node_id"]
        assert first_id
        assert second_id
        assert first_id != second_id

    async def test_benign_verdict_materializes_l0_node(
        self, client: AsyncClient, fresh, mock_jev, monkeypatch
    ):
        monkeypatch.setattr(settings, "typesafe_api_key", "test")
        monkeypatch.setattr(service, "client_factory", lambda: mock_jev(["level_0_benign"]))

        response = await client.post("/api/runs/demo/events", json={"event": "file_read"})

        body = response.json()
        node_id = body["node_id"]
        assert body["level"] == 0
        assert body["escalated"] is False
        assert node_id
        assert graph.get_node(node_id).level == Level.NONE
        assert graph.key_nodes("demo") == []

    async def test_missing_api_key_returns_clean_degraded_verdict_not_500(
        self, client: AsyncClient, fresh, tape_dir, monkeypatch
    ):
        monkeypatch.setattr(settings, "typesafe_api_key", "")

        response = await client.post("/api/runs/demo/events", json={"event": "file_read"})

        assert response.status_code == 200
        body = response.json()
        assert body["degraded"] is True
        assert body["level"] == 0
        node_id = body["node_id"]
        assert node_id
        assert (tape_dir / "demo.jsonl").exists()
        node = graph.get_node(node_id)
        assert node.level == Level.NONE
        assert node.threshold == 0.0
        assert node.intent is None
        assert graph.key_nodes("demo") == []

    async def test_sentinel_elevation_sticks_when_jev_is_degraded(
        self, client: AsyncClient, fresh, monkeypatch
    ):
        monkeypatch.setattr(settings, "typesafe_api_key", "")

        first = await client.post(
            "/api/runs/demo/events",
            json={"event": "tool_write", "tool": "forge_payment"},
        )
        second = await client.post("/api/runs/demo/events", json={"event": "file_read"})

        first_id = first.json()["node_id"]
        second_id = second.json()["node_id"]
        assert first.json()["degraded"] is True
        assert first.json()["level"] == 3
        assert first_id
        assert second.json()["degraded"] is True
        assert second.json()["level"] == 3
        assert second_id
        assert first_id != second_id
        assert graph.level("demo") == Level.SEVERE
        assert graph.get_node(first_id).level == Level.SEVERE
        assert graph.get_node(second_id).level == Level.SEVERE

    async def test_degraded_events_still_materialize_nodes(
        self, client: AsyncClient, fresh, tape_dir, monkeypatch
    ):
        monkeypatch.setattr(settings, "typesafe_api_key", "")

        first = await client.post("/api/runs/demo/events", json={"event": "file_read"})
        second = await client.post("/api/runs/demo/events", json={"event": "shell_command"})

        first_id = first.json()["node_id"]
        second_id = second.json()["node_id"]
        assert first_id
        assert second_id
        assert first_id != second_id
        nodes = [n for n in graph.run_nodes("demo") if n.id != "run:demo"]
        assert [n.id for n in nodes] == [first_id, second_id]
        assert all(n.level == Level.NONE for n in nodes)
        assert graph.key_nodes("demo") == []

    async def test_malformed_body_without_event_is_422(self, client: AsyncClient, fresh):
        response = await client.post("/api/runs/demo/events", json={"path": "/app/.env"})

        assert response.status_code == 422

    async def test_extra_fields_are_accepted(
        self, client: AsyncClient, fresh, mock_jev, monkeypatch
    ):
        monkeypatch.setattr(settings, "typesafe_api_key", "test")
        monkeypatch.setattr(service, "client_factory", lambda: mock_jev(["level_0_benign"]))

        response = await client.post(
            "/api/runs/demo/events",
            json={"event": "shell_command", "cmd": "ls -la", "cwd": "/app"},
        )

        assert response.status_code == 200

    async def test_duplicate_event_id_is_not_reclassified(
        self, client: AsyncClient, fresh, tape_dir, mock_jev, monkeypatch
    ):
        monkeypatch.setattr(settings, "typesafe_api_key", "test")
        jev = mock_jev(["level_1_mild"])
        monkeypatch.setattr(service, "client_factory", lambda: jev)
        payload = {"event": "file_read", "event_id": "stable-event"}

        first = await client.post("/api/runs/demo/events", json=payload)
        second = await client.post("/api/runs/demo/events", json=payload)

        assert first.json()["duplicate"] is False
        assert second.json()["duplicate"] is True
        assert len(jev.calls) == 1
        assert len(log.tail("demo", 100)) == 1


class TestGetRuns:
    async def test_get_run_before_any_event(self, client: AsyncClient, fresh):
        response = await client.get("/api/runs/empty")

        assert response.status_code == 200
        assert response.json() == {"run_id": "empty", "level": 0, "key_nodes": []}

    async def test_get_run_after_events_lists_key_nodes(
        self, client: AsyncClient, fresh, mock_jev, monkeypatch
    ):
        monkeypatch.setattr(settings, "typesafe_api_key", "test")
        monkeypatch.setattr(
            service, "client_factory", _scripted_jev_factory(["level_0_benign", "level_3_severe"])
        )

        await client.post("/api/runs/demo/events", json={"event": "file_read"})
        await client.post("/api/runs/demo/events", json={"event": "file_read", "path": "/app/.env"})

        response = await client.get("/api/runs/demo")

        body = response.json()
        assert body["level"] == 3
        assert len(body["key_nodes"]) == 1
        assert body["key_nodes"][0]["level"] == 3
        assert body["key_nodes"][0]["threshold"] == 0.9

    async def test_list_runs_includes_taped_runs(
        self, client: AsyncClient, fresh, mock_jev, monkeypatch
    ):
        monkeypatch.setattr(settings, "typesafe_api_key", "test")
        monkeypatch.setattr(service, "client_factory", lambda: mock_jev(["level_1_mild"]))

        await client.post("/api/runs/demo/events", json={"event": "file_read"})

        response = await client.get("/api/runs/")

        body = response.json()
        assert [r["run_id"] for r in body] == ["demo"]
        assert body[0]["nodes"] == 1


class TestReplay:
    async def test_recorded_chain_replays_through_api(
        self, client: AsyncClient, fresh, tape_dir, mock_jev, monkeypatch
    ):
        monkeypatch.setattr(settings, "typesafe_api_key", "test")
        chain = chains.CHAINS["attack"]
        hostile = set(chains.HOSTILE_IDX["attack"])
        expected_levels = [4 if i in hostile else 0 for i in range(len(chain))]
        monkeypatch.setattr(
            service,
            "client_factory",
            _scripted_jev_factory(
                ["level_4_severe" if level else "level_0_benign" for level in expected_levels]
            ),
        )

        levels = []
        for event in chain:
            response = await client.post("/api/runs/replay/events", json=event)
            assert response.status_code == 200
            levels.append(response.json()["level"])

        assert levels == expected_levels
        assert len(graph.key_nodes("replay")) == len(hostile)
        assert graph.level("replay") == Level.CRITICAL

        assert (tape_dir / "replay.jsonl").exists()
        assert len(log.tail("replay", 100)) == len(chain)
