import asyncio
import functools
import json

import httpx
import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

import harness


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "WORKSPACE", tmp_path)
    monkeypatch.setattr(harness, "TOOLS_DIR", tmp_path / "tools")
    return tmp_path


@pytest.fixture
def monitor(monkeypatch):
    def make(decision="allow", *, fail=False):
        box = {"requests": [], "paths": []}

        def handler(request: httpx.Request) -> httpx.Response:
            box["paths"].append(request.url.path)
            box["requests"].append(json.loads(request.content))
            if fail:
                raise httpx.ConnectError("down")
            return httpx.Response(
                200,
                json={
                    "decision": decision,
                    "event_id": "evt-1",
                    "reasons": ["risk_threshold"],
                },
            )

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            harness.httpx,
            "AsyncClient",
            functools.partial(httpx.AsyncClient, transport=transport),
        )
        return box

    return make


def events(capsys):
    lines = capsys.readouterr().out.splitlines()
    return [
        json.loads(line.removeprefix(harness.MARKER))
        for line in lines
        if line.startswith(harness.MARKER)
    ]


def run(coro):
    return asyncio.run(coro)


def test_allowed_shell_runs_and_emits_completed_linked_to_preflight(
    workspace, monitor, capsys
):
    box = monitor("allow")

    assert run(harness.shell("printf hello")) == "hello"

    assert len(box["requests"]) == 1
    request = box["requests"][0]
    assert request["phase"] == "requested"
    assert request["tool"] == "shell"
    assert request["event"] == "shell_command"
    assert "identity_state" in request
    assert "trust" in request
    operation_id = request["metadata"]["operation_id"]
    assert isinstance(operation_id, str) and len(operation_id) == 32
    int(operation_id, 16)

    emitted = events(capsys)
    assert len(emitted) == 1
    event = emitted[0]
    assert event["event"] == "shell_command"
    assert event["phase"] == "completed"
    assert event["caused_by"] == ["evt-1"]
    assert event["metadata"]["operation_id"] == operation_id


def test_refused_write_does_not_touch_disk(workspace, monitor, capsys, tmp_path):
    box = monitor("refuse")
    target = tmp_path / "x.txt"

    assert run(harness.write_file(str(target), "hi")) == "refused by monitor: refuse"
    assert not target.exists()

    emitted = events(capsys)
    assert len(emitted) == 1
    event = emitted[0]
    assert event["phase"] == "refused"
    assert event["decision"] == "refuse"
    assert event["reasons"] == ["risk_threshold"]
    assert event["metadata"]["operation_id"] == box["requests"][0]["metadata"]["operation_id"]


def test_hold_blocks_http_request(workspace, monitor, capsys):
    monitor("hold")

    assert (
        run(harness.http_request("https://example.invalid"))
        == "refused by monitor: hold"
    )
    assert [event["phase"] for event in events(capsys)] == ["refused"]


@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(lambda: harness.shell("printf no"), id="shell"),
        pytest.param(lambda: harness.write_file("w.txt", "x"), id="write_file"),
        pytest.param(
            lambda: harness.http_request("https://example.invalid"), id="http_request"
        ),
        pytest.param(
            lambda: harness.register_tool("bad", "print(1)"), id="register_tool"
        ),
    ],
)
def test_monitor_unavailable_fails_open_for_reads_and_closed_for_side_effects(
    workspace, monitor, capsys, tmp_path, invoke
):
    monitor(fail=True)
    readable = tmp_path / "r.txt"
    readable.write_text("data")

    assert run(harness.read_file(str(readable))) == "data"
    emitted = events(capsys)
    assert [event["event"] for event in emitted] == ["monitor_unavailable", "file_read"]
    assert emitted[1]["phase"] == "completed"
    assert emitted[1]["caused_by"] == [None]

    capsys.readouterr()
    assert run(invoke()) == "refused by monitor: monitor unavailable"
    emitted = events(capsys)
    assert [event["event"] for event in emitted] == ["monitor_unavailable"]


def test_operation_ids_differ_between_invocations(workspace, monitor, tmp_path):
    box = monitor("allow")
    readable = tmp_path / "r.txt"
    readable.write_text("data")

    run(harness.read_file(str(readable)))
    run(harness.read_file(str(readable)))

    ids = [request["metadata"]["operation_id"] for request in box["requests"]]
    assert len(ids) == 2
    assert ids[0] != ids[1]


def test_invalid_tool_names_never_reach_the_monitor(workspace, monitor, capsys):
    box = monitor("allow")

    assert run(harness.register_tool("../x", "print(1)")).startswith("error")
    assert run(harness.run_tool("nope")).startswith("error")

    assert box["requests"] == []
    emitted = events(capsys)
    assert emitted[0]["error"] == "invalid name"
    assert emitted[1]["error"] == "unknown tool"


def test_register_then_run_tool_round_trip(workspace, monitor):
    box = monitor("allow")

    run(harness.register_tool("hello", "print('hi')"))
    assert (workspace / "tools" / "hello.py").exists()
    assert run(harness.run_tool("hello")).strip() == "hi"

    assert [request["tool"] for request in box["requests"]] == [
        "register_tool",
        "run_tool",
    ]
    assert box["requests"][1]["dynamic_tool"] == "hello"


def test_build_agent_requires_helmcode_key(monkeypatch):
    monkeypatch.delenv("HELMCODE_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        harness.build_agent()
    agent = harness.build_agent(
        FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("ok")]))
    )
    assert agent is not None


def test_main_emits_run_start_and_run_end_with_allowed_output(workspace, monitor, capsys):
    box = monitor("allow")
    model = FunctionModel(
        lambda messages, info: ModelResponse(parts=[TextPart("done")])
    )

    run(harness.main(model))

    emitted = events(capsys)
    assert [event["event"] for event in emitted] == ["run_start", "run_end"]
    assert emitted[1]["output"] == "done"
    assert emitted[1]["caused_by"] == ["evt-1"]
    assert len(box["requests"]) == 1
    request = box["requests"][0]
    assert request["event"] == "utterance"
    assert request["tool"] == "assistant_message"
    assert request["content"] == "done"


def test_main_refused_output_is_not_printed(workspace, monitor, capsys):
    monitor("refuse")
    model = FunctionModel(
        lambda messages, info: ModelResponse(parts=[TextPart("done")])
    )

    run(harness.main(model))

    raw = capsys.readouterr().out
    emitted = [
        json.loads(line.removeprefix(harness.MARKER))
        for line in raw.splitlines()
        if line.startswith(harness.MARKER)
    ]
    assert [event["event"] for event in emitted] == [
        "run_start",
        "utterance",
        "run_end",
    ]
    assert emitted[2]["output"] == "refused"
    plain = [line for line in raw.splitlines() if not line.startswith(harness.MARKER)]
    assert any("refused by monitor" in line for line in plain)
