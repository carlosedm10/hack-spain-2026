from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

MARKER = "__hs_event__"
LOG_ROOT = Path(__file__).resolve().parents[1] / ".local" / "harness"


def valid_event(event: object, run_id: str) -> bool:
    if not isinstance(event, dict) or event.get("run_id") != run_id:
        return False
    if not isinstance(event.get("event"), str) or not event["event"]:
        return False
    ts = event.get("ts")
    return isinstance(ts, (int, float)) and math.isfinite(ts)


def identity(event: dict) -> str:
    return hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()


def forward(api: str, run_id: str, event: dict) -> dict:
    request = Request(
        f"{api.rstrip('/')}/api/runs/{quote(run_id, safe='')}/events",
        data=json.dumps(event).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def collect(lines: Iterable[str], path: Path, run_id: str, api: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a+", encoding="utf-8") as tape:
        os.fchmod(tape.fileno(), 0o600)
        fcntl.flock(tape, fcntl.LOCK_EX | fcntl.LOCK_NB)
        tape.seek(0)
        seen = {identity(json.loads(line)) for line in tape if line.strip()}
        for line in lines:
            if not line.startswith(MARKER):
                continue
            try:
                event = json.loads(line[len(MARKER) :])
            except json.JSONDecodeError:
                print("collector: rejected invalid JSON", file=sys.stderr)
                continue
            if not valid_event(event, run_id):
                print("collector: rejected invalid or mismatched event", file=sys.stderr)
                continue
            key = identity(event)
            if key in seen:
                continue
            tape.write(json.dumps(event, ensure_ascii=False) + "\n")
            tape.flush()
            os.fsync(tape.fileno())
            seen.add(key)
            try:
                forward(api, run_id, event)
            except (URLError, TimeoutError, ValueError):
                print(
                    "collector: event delivery failed; event retained", file=sys.stderr
                )


def main(container: str, run_id: str, api: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        raise ValueError("invalid run_id")
    container_id = subprocess.check_output(
        ["docker", "inspect", "--format", "{{.Id}}", "--", container], text=True
    ).strip()
    if not re.fullmatch(r"[a-f0-9]{64}", container_id):
        raise ValueError("invalid container identity")
    process = subprocess.Popen(
        ["docker", "logs", "--follow", "--tail", "all", container_id],
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        if process.stdout is None:
            raise RuntimeError("missing Docker log stream")
        with process.stdout:
            collect(process.stdout, LOG_ROOT / container_id / f"{run_id}.jsonl", run_id, api)
        if process.wait() != 0:
            raise RuntimeError("Docker log capture failed")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()


if __name__ == "__main__":
    main(*sys.argv[1:])
