import asyncio
import json
import uuid
from functools import wraps
from pathlib import Path

import pytest
from pydantic_ai import DeferredToolRequests, DeferredToolResults
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

import harness

CALLS = [
    ToolCallPart("shell", {"cmd": "printf hello"}, "call-shell"),
    ToolCallPart("read_file", {"path": "/workspace/example.txt"}, "call-read"),
    ToolCallPart(
        "write_file", {"path": "/workspace/example.txt", "content": "hola 世界" * 300}, "call-write"
    ),
    ToolCallPart("http_request", {"url": "https://example.invalid"}, "call-http"),
    ToolCallPart("register_tool", {"name": "hello", "code": "print('hello')"}, "call-register"),
    ToolCallPart("run_tool", {"name": "hello", "args": ""}, "call-run"),
]

CAPTURE_ID = "c" * 32


@pytest.fixture
def executed(monkeypatch):
    calls = []

    def spy(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            calls.append((function.__name__, args, kwargs))
            return "ok"

        return wrapped

    for call in CALLS:
        monkeypatch.setattr(harness, call.tool_name, spy(getattr(harness, call.tool_name)))
    return calls


@pytest.fixture
def decisions(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "uuid4", lambda: uuid.UUID(CAPTURE_ID))
    monkeypatch.setattr(harness, "DECISIONS_DIR", str(tmp_path))
    return tmp_path / CAPTURE_ID


def seed_decision(directory: Path, call: ToolCallPart, *, approved: bool, level=1, digest=None):
    audited = harness.audit_value(call.args_as_dict())
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "approved": approved,
        "digest": digest or harness.decision_digest(call.tool_name, audited),
        "level": level,
        "intent": "recon",
        "degraded": False,
    }
    (directory / f"{call.tool_call_id}.json").write_text(json.dumps(payload))


def model_for(*calls):
    return FunctionModel(lambda messages, info: ModelResponse(parts=list(calls)))


def scripted(*steps):
    responses = iter(steps)
    return FunctionModel(lambda messages, info: ModelResponse(parts=next(responses)))


def events(capsys):
    lines = capsys.readouterr().out.splitlines()
    assert all(line.startswith(harness.MARKER) for line in lines)
    return [json.loads(line.removeprefix(harness.MARKER)) for line in lines]


def test_every_tool_is_deferred_before_its_body_runs(executed):
    agent = harness.build_agent(model_for(*CALLS))
    result = asyncio.run(agent.run("test"))

    assert isinstance(result.output, DeferredToolRequests)
    assert {call.tool_call_id for call in result.output.approvals} == {
        call.tool_call_id for call in CALLS
    }
    assert result.output.calls == []
    assert executed == []


def test_approved_calls_run_and_their_arguments_are_not_clipped(executed, capsys, decisions):
    for call in CALLS[:3]:
        seed_decision(decisions, call, approved=True)
    result = asyncio.run(harness.main(scripted(CALLS[:3], [TextPart("done")])))

    captured = events(capsys)
    pending = [event for event in captured if event["event"] == "tool_call_pending"]
    assert [event["event"] for event in captured] == [
        "run_start",
        *["tool_call_pending"] * 3,
        "run_end",
    ]
    assert len({event["capture_id"] for event in captured}) == 1
    assert all(event["run_id"] == harness.RUN_ID for event in captured)
    for event, call in zip(pending, CALLS[:3], strict=True):
        assert event["tool_call_id"] == call.tool_call_id
        assert event["tool"] == call.tool_name
        assert event["args"] == call.args_as_dict()
        assert event["status"] == "pending"
        assert event["args_redacted"] is False
        assert event["digest"] == harness.decision_digest(call.tool_name, call.args_as_dict())
    assert pending[2]["args"]["content"] == "hola 世界" * 300
    assert [name for name, args, kwargs in executed] == ["shell", "read_file", "write_file"]
    assert result.output == "done"
    assert captured[-1]["output"] == "done"


def test_denied_call_is_blocked_and_reported_to_the_model(executed, capsys, decisions):
    seed_decision(decisions, CALLS[0], approved=False, level=3)
    asyncio.run(harness.main(scripted([CALLS[0]], [TextPart("stopped")])))

    captured = events(capsys)
    blocked = [event for event in captured if event["event"] == "tool_call_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["tool_call_id"] == CALLS[0].tool_call_id
    assert blocked[0]["tool"] == "shell"
    assert blocked[0]["reason"] == "denied"
    assert blocked[0]["level"] == 3
    assert captured[-1]["event"] == "run_end"
    assert captured[-1]["output"] == "stopped"
    assert executed == []


def test_missing_decision_times_out_and_blocks(executed, capsys, decisions, monkeypatch):
    monkeypatch.setattr(harness, "DECISION_TIMEOUT_S", 0.3)
    asyncio.run(harness.main(scripted([CALLS[0]], [TextPart("stopped")])))

    captured = events(capsys)
    blocked = next(event for event in captured if event["event"] == "tool_call_blocked")
    assert blocked["reason"] == "no decision"
    assert blocked["level"] is None
    assert captured[-1]["event"] == "run_end"
    assert executed == []


def test_decision_with_a_wrong_digest_blocks(executed, capsys, decisions):
    seed_decision(decisions, CALLS[0], approved=True, digest="0" * 64)
    asyncio.run(harness.main(scripted([CALLS[0]], [TextPart("stopped")])))

    captured = events(capsys)
    blocked = next(event for event in captured if event["event"] == "tool_call_blocked")
    assert blocked["reason"] == "no decision"
    assert executed == []


def test_pending_digest_binds_the_audited_arguments(executed, capsys, decisions, monkeypatch):
    monkeypatch.setenv("HELMCODE_API_KEY", "synthetic-provider-value")
    call = ToolCallPart(
        "write_file", {"path": "example.txt", "content": "synthetic-provider-value"}, "private-call"
    )
    audited = harness.audit_value(call.args_as_dict())
    seed_decision(decisions, call, approved=False)
    asyncio.run(harness.main(scripted([call], [TextPart("stopped")])))

    captured = events(capsys)
    pending = next(event for event in captured if event["event"] == "tool_call_pending")
    assert pending["args"]["content"] == "[REDACTED]"
    assert pending["args_redacted"] is True
    assert pending["digest"] == harness.decision_digest("write_file", audited)
    assert "synthetic-provider-value" not in json.dumps(captured)
    assert call.args_as_dict()["content"] == "synthetic-provider-value"
    assert executed == []


def test_invalid_arguments_cannot_execute_a_tool(executed):
    responses = iter(
        [[ToolCallPart("write_file", {"path": "example.txt"}, "invalid")], [TextPart("No write")]]
    )
    model = FunctionModel(lambda messages, info: ModelResponse(parts=next(responses)))
    result = asyncio.run(harness.build_agent(model).run("test"))
    assert result.output == "No write"
    assert executed == []


def test_capture_ids_differ_between_invocations(executed, capsys, monkeypatch):
    monkeypatch.setattr(harness, "DECISION_TIMEOUT_S", 0.05)
    asyncio.run(harness.main(scripted([CALLS[0]], [TextPart("done")])))
    first = events(capsys)
    asyncio.run(harness.main(scripted([CALLS[0]], [TextPart("done")])))
    second = events(capsys)
    assert first[0]["capture_id"] != second[0]["capture_id"]


@pytest.mark.parametrize(
    "value",
    [
        {"Authorization": "synthetic-auth-value"},
        {"nested": [{"api_key": "synthetic-key-value"}]},
        {"cmd": "curl -H 'Authorization: Bearer synthetic-auth-value' https://example.invalid"},
        {"content": 'password = "synthetic-password-value"'},
        {"url": "http://demo:synthetic-proxy-value@example.invalid"},
        {"content": "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic-key-value"},
    ],
)
def test_audit_redacts_structured_and_embedded_credentials(value):
    redacted = harness.audit_value(value)
    assert "synthetic-" not in json.dumps(redacted)
    assert "[REDACTED]" in json.dumps(redacted)
    assert "synthetic-" in json.dumps(value)


def test_known_proxy_password_is_redacted_even_without_a_label(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://demo:synthetic-proxy-value@example.invalid")
    assert harness.audit_value({"body": "synthetic-proxy-value"}) == {"body": "[REDACTED]"}


def test_tool_output_is_audited_before_emission(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "WORKSPACE", tmp_path)
    asyncio.run(harness.shell("printf 'password = synthetic-value'"))
    emitted = events(capsys)
    assert emitted[-1]["event"] == "shell_command"
    assert emitted[-1]["output"] == "[REDACTED]"
    assert "synthetic-value" not in emitted[-1]["output"]


def test_only_explicitly_approved_tool_runs_and_next_call_is_pending(executed):
    responses = iter([CALLS[:2], [CALLS[2]]])
    model = FunctionModel(lambda messages, info: ModelResponse(parts=next(responses)))
    agent = harness.build_agent(model)

    async def scenario():
        first = await agent.run("test")
        assert executed == []
        return await agent.run(
            message_history=first.all_messages(),
            deferred_tool_results=DeferredToolResults(
                approvals={CALLS[0].tool_call_id: True, CALLS[1].tool_call_id: False}
            ),
        )

    result = asyncio.run(scenario())
    assert [name for name, args, kwargs in executed] == ["shell"]
    assert isinstance(result.output, DeferredToolRequests)
    assert [call.tool_call_id for call in result.output.approvals] == [CALLS[2].tool_call_id]


def test_text_response_completes_without_tools(executed, capsys):
    model = FunctionModel(lambda messages, info: ModelResponse(parts=[TextPart("Hecho")]))
    result = asyncio.run(harness.main(model))
    captured = events(capsys)
    assert result.output == "Hecho"
    assert [event["event"] for event in captured] == ["run_start", "run_end"]
    assert captured[-1]["output"] == "Hecho"
    assert executed == []


@pytest.mark.parametrize("error", [RuntimeError("model failed"), asyncio.CancelledError()])
def test_model_failure_never_executes_tools_or_reports_success(executed, capsys, error):
    async def fail(messages, info):
        raise error

    with pytest.raises(type(error)):
        asyncio.run(harness.main(FunctionModel(fail)))
    captured = events(capsys)
    assert not any(event["event"] == "run_end" for event in captured)
    assert executed == []
