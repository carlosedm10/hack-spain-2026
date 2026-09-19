import io
import json
import os
import subprocess
from pathlib import Path
from urllib.error import URLError

import pytest
from scripts import collect

CONTAINER_ID = "a" * 64
CAPTURE_ID = "b" * 32
DIGEST = "d" * 64


def pending(**changes):
    return {
        "event": "tool_call_pending",
        "run_id": "demo",
        "capture_id": CAPTURE_ID,
        "tool_call_id": "call-1",
        "tool": "write_file",
        "args": {"path": "hello.txt", "content": "hola" * 300},
        "args_redacted": False,
        "digest": DIGEST,
        "status": "pending",
        "ts": 123.0,
        **changes,
    }


def line(event):
    return "__hs_event__" + json.dumps(event) + "\n"


@pytest.fixture
def delivered(monkeypatch, tmp_path):
    monkeypatch.setattr(collect, "LOG_ROOT", tmp_path / "logs")
    box = {
        "events": [],
        "verdict": {"level": 1, "degraded": False, "intent": "recon"},
        "root": tmp_path / "logs",
    }

    def send(api, run_id, event):
        box["events"].append((api, run_id, event))
        if isinstance(box["verdict"], Exception):
            raise box["verdict"]
        return box["verdict"]

    monkeypatch.setattr(collect, "forward", send)
    return box


def decision_path(box, event):
    return (
        box["root"] / "decisions" / event["capture_id"] / f"{event['tool_call_id']}.json"
    )


def test_every_valid_event_is_forwarded_and_archived(tmp_path, delivered):
    path = tmp_path / "capture" / "demo.jsonl"
    start = {"event": "run_start", "run_id": "demo", "capture_id": CAPTURE_ID, "ts": 122.0}
    blocked = {
        "event": "tool_call_blocked",
        "run_id": "demo",
        "capture_id": CAPTURE_ID,
        "tool_call_id": "call-1",
        "tool": "write_file",
        "level": 3,
        "reason": "denied",
        "ts": 124.0,
    }
    legacy = {"event": "file_read", "run_id": "demo", "path": "hello.txt", "ts": 125.0}
    records = [start, pending(), blocked, legacy]
    collect.collect(map(line, records), path, "demo", "http://localhost:8000")
    assert [json.loads(value) for value in path.read_text().splitlines()] == records
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert [event for api, run_id, event in delivered["events"]] == records


def test_pending_call_below_block_level_is_approved(tmp_path, delivered):
    path = tmp_path / "demo.jsonl"
    collect.collect([line(pending())], path, "demo", "http://localhost:8000")
    decision = json.loads(decision_path(delivered, pending()).read_text())
    assert decision == {
        "approved": True,
        "digest": DIGEST,
        "level": 1,
        "intent": "recon",
        "degraded": False,
    }


@pytest.mark.parametrize(
    "verdict",
    [
        {"level": 3, "degraded": False, "intent": "exfiltrate_secrets"},
        {"level": 5, "degraded": False, "intent": "lateral_movement"},
        {"level": 1, "degraded": True},
    ],
)
def test_pending_call_at_block_level_or_degraded_is_denied(tmp_path, delivered, verdict):
    delivered["verdict"] = verdict
    path = tmp_path / "demo.jsonl"
    collect.collect([line(pending())], path, "demo", "http://localhost:8000")
    decision = json.loads(decision_path(delivered, pending()).read_text())
    assert decision["approved"] is False
    assert decision["digest"] == DIGEST
    assert decision["level"] == verdict["level"]


def test_unreachable_api_writes_an_immediate_denial(tmp_path, delivered, capsys):
    delivered["verdict"] = URLError("refused")
    path = tmp_path / "demo.jsonl"
    collect.collect([line(pending())], path, "demo", "http://localhost:8000")
    decision = json.loads(decision_path(delivered, pending()).read_text())
    assert decision["approved"] is False
    assert decision["degraded"] is True
    assert decision["level"] is None
    assert "delivery failed" in capsys.readouterr().err
    assert len(path.read_text().splitlines()) == 1


def test_decision_artifacts_are_readable_by_the_sandbox_user(tmp_path, delivered):
    path = tmp_path / "demo.jsonl"
    collect.collect([line(pending())], path, "demo", "http://localhost:8000")
    decision = decision_path(delivered, pending())
    assert decision.stat().st_mode & 0o777 == 0o644
    assert decision.parent.stat().st_mode & 0o777 == 0o755


def test_replay_deduplicates_and_never_rewrites_a_decision(tmp_path, delivered):
    path = tmp_path / "demo.jsonl"
    collect.collect([line(pending())], path, "demo", "http://localhost:8000")
    decision = decision_path(delivered, pending())
    before = decision.read_bytes()
    records = [pending(ts=999.0), pending(capture_id="c" * 32), pending(tool_call_id="call-2")]
    collect.collect(map(line, records), path, "demo", "http://localhost:8000")
    collect.collect([line(pending())], path, "demo", "http://localhost:8000")
    assert len(path.read_text().splitlines()) == 3
    assert len(delivered["events"]) == 3
    assert decision.read_bytes() == before


def test_invalid_or_cross_run_events_are_rejected(tmp_path, delivered, capsys):
    path = tmp_path / "demo.jsonl"
    records = [
        "ordinary output\n",
        "prefix " + line(pending()),
        "__hs_event__{invalid json}\n",
        "__hs_event__[]\n",
        line(pending(run_id="another-run")),
        line(pending(capture_id="")),
        line(pending(tool_call_id="")),
        line(pending(tool_call_id="../escape")),
        line(pending(args="not an object")),
        line(pending(status="approved")),
        line(pending(capture_id=None)),
        line(pending(digest="")),
        line(pending(digest="not-hex")),
        line({k: v for k, v in pending().items() if k != "digest"}),
    ]
    collect.collect(records, path, "demo", "http://localhost:8000")
    assert path.read_text() == ""
    assert delivered["events"] == []
    assert "not an object" not in capsys.readouterr().err
    assert not (delivered["root"] / "decisions").exists()


def test_replay_does_not_forward_twice(tmp_path, delivered):
    path = tmp_path / "demo.jsonl"
    event = {"event": "file_read", "run_id": "demo", "path": "hello.txt", "ts": 123.0}
    collect.collect([line(event)], path, "demo", "http://localhost:8000")
    collect.collect([line(event)], path, "demo", "http://localhost:8000")
    assert delivered["events"] == [("http://localhost:8000", "demo", event)]


def test_collector_rejects_symlink_archive(tmp_path, delivered):
    target = tmp_path / "existing"
    target.write_text("unchanged")
    path = tmp_path / "demo.jsonl"
    path.symlink_to(target)
    with pytest.raises(OSError):
        collect.collect([line(pending())], path, "demo", "http://localhost:8000")
    assert target.read_text() == "unchanged"
    assert delivered["events"] == []


def test_main_collects_historical_logs_from_immutable_container_id(tmp_path, monkeypatch):
    commands = []

    def inspect(command, **kwargs):
        commands.append(command)
        return CONTAINER_ID + "\n"

    class Logs:
        stdout = io.StringIO(line(pending()))

        def __init__(self, command, **kwargs):
            commands.append(command)

        def wait(self):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(collect.subprocess, "check_output", inspect)
    monkeypatch.setattr(collect.subprocess, "Popen", Logs)
    monkeypatch.setattr(collect, "LOG_ROOT", tmp_path)
    monkeypatch.setattr(collect, "forward", lambda api, run_id, event: {"level": 0})
    collect.main("hackspain_agent", "demo", "http://localhost:8000")
    assert commands[1] == ["docker", "logs", "--follow", "--tail", "all", CONTAINER_ID]
    assert json.loads((tmp_path / CONTAINER_ID / "demo.jsonl").read_text()) == pending()


@pytest.mark.parametrize("run_id", ["../escape", ".", "", "demo/run"])
def test_invalid_run_id_fails_before_starting_docker(run_id, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Docker must not be called")

    monkeypatch.setattr(collect.subprocess, "check_output", unexpected)
    with pytest.raises(ValueError):
        collect.main("hackspain_agent", run_id, "http://localhost:8000")


def test_shell_entrypoint_passes_arguments_and_docker_failure(tmp_path):
    docker = tmp_path / "docker"
    docker.write_text("#!/usr/bin/env sh\nexit 23\n")
    docker.chmod(0o700)
    script = Path(__file__).resolve().parents[2] / "scripts" / "collect.sh"
    result = subprocess.run(
        ["bash", str(script), "missing", "demo"],
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
