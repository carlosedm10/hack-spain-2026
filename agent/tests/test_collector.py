import io
import json
import os
import subprocess
from pathlib import Path
from urllib.error import URLError

import pytest
from scripts import collect

CONTAINER_ID = "a" * 64


def sample(**changes):
    return {
        "event": "file_read",
        "run_id": "demo",
        "ts": 1.0,
        "phase": "completed",
        "tool": "read_file",
        "metadata": {"operation_id": "a" * 32},
        "caused_by": ["evt"],
        **changes,
    }


def line(event):
    return "__hs_event__" + json.dumps(event) + "\n"


@pytest.fixture
def delivered(monkeypatch, tmp_path):
    monkeypatch.setattr(collect, "LOG_ROOT", tmp_path / "logs")
    box = {"events": [], "error": None}

    def send(api, run_id, event):
        box["events"].append((api, run_id, event))
        if box["error"] is not None:
            raise box["error"]
        return {"level": 0}

    monkeypatch.setattr(collect, "forward", send)
    return box


def test_every_valid_event_is_forwarded_and_archived(tmp_path, delivered):
    path = tmp_path / "capture" / "demo.jsonl"
    records = [
        {"event": "run_start", "run_id": "demo", "ts": 122.0},
        sample(),
        {"event": "run_end", "run_id": "demo", "ts": 124.0, "output": "done"},
    ]
    collect.collect(map(line, records), path, "demo", "http://localhost:8000")
    assert [json.loads(value) for value in path.read_text().splitlines()] == records
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert [event for api, run_id, event in delivered["events"]] == records


def test_unreachable_api_retains_the_event(tmp_path, delivered, capsys):
    delivered["error"] = URLError("refused")
    path = tmp_path / "demo.jsonl"
    collect.collect([line(sample())], path, "demo", "http://localhost:8000")
    assert "delivery failed" in capsys.readouterr().err
    assert len(path.read_text().splitlines()) == 1


def test_replay_does_not_forward_twice(tmp_path, delivered):
    path = tmp_path / "demo.jsonl"
    event = sample()
    collect.collect([line(event)], path, "demo", "http://localhost:8000")
    collect.collect([line(event)], path, "demo", "http://localhost:8000")
    assert len(path.read_text().splitlines()) == 1
    assert delivered["events"] == [("http://localhost:8000", "demo", event)]


def test_invalid_or_cross_run_events_are_rejected(tmp_path, delivered, capsys):
    path = tmp_path / "demo.jsonl"
    records = [
        "ordinary output\n",
        "prefix " + line(sample()),
        "__hs_event__{invalid json}\n",
        "__hs_event__[]\n",
        line(sample(run_id="another-run")),
        line(sample(event="")),
        line({k: v for k, v in sample().items() if k != "event"}),
        line(sample(ts="not-a-number")),
        line(sample(ts=float("inf"))),
        line(sample(ts=float("nan"))),
    ]
    collect.collect(records, path, "demo", "http://localhost:8000")
    assert path.read_text() == ""
    assert delivered["events"] == []


def test_collector_rejects_symlink_archive(tmp_path, delivered):
    target = tmp_path / "existing"
    target.write_text("unchanged")
    path = tmp_path / "demo.jsonl"
    path.symlink_to(target)
    with pytest.raises(OSError):
        collect.collect([line(sample())], path, "demo", "http://localhost:8000")
    assert target.read_text() == "unchanged"
    assert delivered["events"] == []


def test_main_collects_historical_logs_from_immutable_container_id(tmp_path, monkeypatch):
    commands = []

    def inspect(command, **kwargs):
        commands.append(command)
        return CONTAINER_ID + "\n"

    class Logs:
        stdout = io.StringIO(line(sample()))

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
    assert json.loads((tmp_path / CONTAINER_ID / "demo.jsonl").read_text()) == sample()


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
