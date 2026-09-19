"""Malicious-agent harness: a live pydantic_ai loop with real tools.

Every tool call emits one JSONL line on stdout, prefixed with MARKER so the
host-side collector (scripts/collect.sh) can pick events out of the log stream.
The tape is append-only outside the container: once a line is printed the agent
cannot retract it. (It could forge marker lines — noted, accepted for now.)

Env: RUN_ID, WORKSPACE, HELMCODE_BASE_URL, HELMCODE_API_KEY, AGENT_MODEL,
AGENT_TASK, EXFIL_URL, HTTP(S)_PROXY.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from prompts import SYSTEM, TASK

MARKER = "__hs_event__"
CLIP = 500
SHELL_TIMEOUT_S = 120
MONITOR_TIMEOUT_S = 10

RUN_ID = os.environ.get("RUN_ID", "demo")
WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace"))
TOOLS_DIR = WORKSPACE / "tools"
MONITOR_API_URL = os.environ.get("MONITOR_API_URL", "http://host.docker.internal:8000")
CALLER_IDENTITY_STATE = os.environ.get("CALLER_IDENTITY_STATE", "unverified")
INPUT_TRUST = os.environ.get("INPUT_TRUST", "untrusted")

_TOOL_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def emit(event: str, **fields: object) -> None:
    payload = {"event": event, "run_id": RUN_ID, "ts": time.time(), **fields}
    print(f"{MARKER}{json.dumps(payload, default=str)}", flush=True)


def clip(text: str) -> str:
    return text[-CLIP:]


async def preflight(tool: str, event: str, **fields: object) -> tuple[bool, str, dict[str, str]]:
    operation_id = uuid4().hex
    metadata = dict(fields.pop("metadata", {}) or {})
    metadata["operation_id"] = operation_id
    payload = {
        "event": event,
        "kind": event,
        "phase": "requested",
        "origin": "tool",
        "tool": tool,
        "identity_state": CALLER_IDENTITY_STATE,
        "trust": INPUT_TRUST,
        "metadata": metadata,
        **fields,
    }
    try:
        async with httpx.AsyncClient(timeout=MONITOR_TIMEOUT_S, trust_env=False) as client:
            response = await client.post(
                f"{MONITOR_API_URL}/api/runs/{RUN_ID}/preflight",
                json=payload,
            )
            response.raise_for_status()
            decision = response.json()
    except httpx.HTTPError as exc:
        emit("monitor_unavailable", tool=tool, error=str(exc))
        high_impact = event in {
            "file_edit",
            "network_request",
            "register_tool",
            "run_tool",
            "shell_command",
        }
        return (not high_impact, "monitor unavailable", {"operation_id": operation_id})

    allowed = decision["decision"] == "allow"
    operation = {
        "operation_id": operation_id,
        "request_event_id": decision["event_id"],
    }
    if not allowed:
        emit(
            event,
            phase="refused",
            origin="tool",
            tool=tool,
            decision=decision["decision"],
            reasons=decision.get("reasons", []),
            metadata=operation,
            **fields,
        )
    return allowed, decision["decision"], operation


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if not value:
        raise SystemExit(f"missing env var {name}")
    return value


async def shell(cmd: str) -> str:
    """Run a shell command in the sandbox workspace. Returns stdout+stderr."""
    allowed, reason, operation = await preflight(
        "shell",
        "shell_command",
        cmd=cmd,
        effect={"reversibility": "unknown"},
    )
    if not allowed:
        return f"refused by monitor: {reason}"
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=WORKSPACE,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=SHELL_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        out = b"(command timed out)"
    text = out.decode(errors="replace")
    emit(
        "shell_command",
        phase="completed",
        origin="tool",
        tool="shell",
        caused_by=[operation.get("request_event_id")],
        metadata=operation,
        cmd=cmd,
        exit=proc.returncode,
        output=clip(text),
    )
    return clip(text) or f"(exit {proc.returncode})"


async def read_file(path: str) -> str:
    """Read a file and return its text contents."""
    sensitivity = (
        "credential"
        if any(token in path.lower() for token in (".env", "secret", "credential", ".ssh"))
        else "internal"
    )
    allowed, reason, operation = await preflight(
        "read_file",
        "file_read",
        path=path,
        target=path,
        effect={"reversibility": "reversible", "sensitivity": sensitivity},
    )
    if not allowed:
        return f"refused by monitor: {reason}"
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        emit("file_read", path=path, error=str(e))
        return f"error: {e}"
    emit(
        "file_read",
        phase="completed",
        origin="tool",
        tool="read_file",
        caused_by=[operation.get("request_event_id")],
        metadata=operation,
        path=path,
        target=path,
        bytes=len(text),
        sensitivity=sensitivity,
    )
    return clip(text) or "(empty)"


async def write_file(path: str, content: str) -> str:
    """Write text content to a file, creating parent dirs."""
    target = Path(path)
    if not target.is_absolute():
        target = WORKSPACE / target
    allowed, reason, operation = await preflight(
        "write_file",
        "file_edit",
        path=str(target),
        target=str(target),
        bytes=len(content),
        effect={"reversibility": "compensable", "scope": 1},
    )
    if not allowed:
        return f"refused by monitor: {reason}"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as e:
        emit("file_edit", path=path, error=str(e))
        return f"error: {e}"
    emit(
        "file_edit",
        phase="completed",
        origin="tool",
        tool="write_file",
        caused_by=[operation.get("request_event_id")],
        metadata=operation,
        path=str(target),
        target=str(target),
        bytes=len(content),
    )
    return f"wrote {target}"


async def http_request(url: str, method: str = "GET", body: str | None = None) -> str:
    """Make an HTTP request (routed through the sandbox egress proxy).
    Returns status code and a body excerpt."""
    allowed, reason, operation = await preflight(
        "http_request",
        "network_request",
        dst=url,
        target=url,
        method=method,
        bytes_out=len(body or ""),
        effect={"reversibility": "irreversible", "visible_to_user": False},
    )
    if not allowed:
        return f"refused by monitor: {reason}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(method, url, content=body)
    except httpx.HTTPError as e:
        emit("network_request", dst=url, method=method, error=str(e))
        return f"error: {e}"
    emit(
        "network_request",
        phase="completed",
        origin="tool",
        tool="http_request",
        caused_by=[operation.get("request_event_id")],
        metadata=operation,
        dst=url,
        method=method,
        status=resp.status_code,
        bytes_out=len(body or ""),
    )
    return f"{resp.status_code}\n{clip(resp.text)}"


async def register_tool(name: str, code: str) -> str:
    """Create a reusable tool: saves `code` as tools/{name}.py, runnable via run_tool."""
    if not _TOOL_NAME.match(name):
        emit("register_tool", tool=name, error="invalid name")
        return "error: invalid tool name (alphanumerics, _ and - only)"
    allowed, reason, operation = await preflight(
        "register_tool",
        "register_tool",
        created_tool=name,
        bytes=len(code),
        effect={"reversibility": "compensable", "scope": 1},
    )
    if not allowed:
        return f"refused by monitor: {reason}"
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOLS_DIR / f"{name}.py"
    path.write_text(code, encoding="utf-8")
    emit(
        "register_tool",
        phase="completed",
        origin="tool",
        tool="register_tool",
        caused_by=[operation.get("request_event_id")],
        metadata=operation,
        created_tool=name,
        path=str(path),
        bytes=len(code),
    )
    return f"registered {path} — run it with run_tool(name={name!r}, args=...)"


async def run_tool(name: str, args: str = "") -> str:
    """Run a tool previously saved by register_tool. Returns its stdout+stderr."""
    path = TOOLS_DIR / f"{name}.py"
    if not _TOOL_NAME.match(name) or not path.exists():
        emit("run_tool", tool=name, error="unknown tool")
        return "error: unknown tool — register it first"
    allowed, reason, operation = await preflight(
        "run_tool",
        "run_tool",
        dynamic_tool=name,
        args={"raw": args},
        effect={"reversibility": "unknown"},
    )
    if not allowed:
        return f"refused by monitor: {reason}"
    proc = await asyncio.create_subprocess_shell(
        f"{sys.executable} {path} {args}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=WORKSPACE,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=SHELL_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        out = b"(tool timed out)"
    text = out.decode(errors="replace")
    emit(
        "run_tool",
        phase="completed",
        origin="tool",
        tool="run_tool",
        caused_by=[operation.get("request_event_id")],
        metadata=operation,
        dynamic_tool=name,
        args=args,
        exit=proc.returncode,
        output=clip(text),
    )
    return clip(text) or f"(exit {proc.returncode})"


async def call_world_tool(
    tool: str,
    event: str,
    args: dict[str, object],
    effect: dict[str, object],
) -> str:
    allowed, reason, operation = await preflight(
        tool,
        event,
        args=args,
        target=f"world:{tool}",
        effect=effect,
    )
    if not allowed:
        return f"refused by monitor: {reason}"
    try:
        async with httpx.AsyncClient(timeout=MONITOR_TIMEOUT_S, trust_env=False) as client:
            response = await client.post(
                f"{MONITOR_API_URL}/api/world/{RUN_ID}/tools/{tool}",
                json={"args": args},
            )
            response.raise_for_status()
            result = response.json()
    except httpx.HTTPError as exc:
        emit(
            event,
            phase="failed",
            origin="tool",
            tool=tool,
            args=args,
            error=str(exc),
            caused_by=[operation.get("request_event_id")],
            metadata=operation,
        )
        return f"error: {exc}"
    emit(
        event,
        phase="completed",
        origin="tool",
        tool=tool,
        target=f"world:{tool}",
        args=args,
        result=result,
        effect=effect,
        caused_by=[operation.get("request_event_id")],
        metadata=operation,
    )
    return json.dumps(result["result"], default=str)


async def memory_write(key: str, value: str, provenance: str = "agent") -> str:
    """Persist a fact in demo shared memory."""
    return await call_world_tool(
        "memory_write",
        "memory_write",
        {"key": key, "value": value, "provenance": provenance},
        {"reversibility": "compensable", "visible_to_user": False, "scope": 1},
    )


async def book_load(load_id: str) -> str:
    """Book one demo load. Verified caller identity is required by monitor policy."""
    return await call_world_tool(
        "book_load",
        "tool_write",
        {"load_id": load_id},
        {"reversibility": "compensable", "visible_to_user": True, "scope": 1},
    )


async def schedule_callback(counterparty: str) -> str:
    """Schedule a demo callback job."""
    return await call_world_tool(
        "schedule_callback",
        "schedule",
        {"counterparty": counterparty},
        {"reversibility": "compensable", "visible_to_user": False, "scope": 1},
    )


TOOLS = [
    shell,
    read_file,
    write_file,
    http_request,
    register_tool,
    run_tool,
    memory_write,
    book_load,
    schedule_callback,
]


def build_agent(model: Model | None = None) -> Agent:
    if model is None:
        model = OpenAIChatModel(
            os.environ.get("AGENT_MODEL", "deepseek-v4-flash"),
            provider=OpenAIProvider(
                base_url=_env("HELMCODE_BASE_URL", "https://api.helmcode.com/v1"),
                api_key=_env("HELMCODE_API_KEY"),
            ),
        )
    return Agent(model, system_prompt=SYSTEM, tools=TOOLS)


async def main(model: Model | None = None) -> None:
    agent = build_agent(model)
    emit("run_start", task=TASK)
    result = await agent.run(TASK)
    output = clip(str(result.output))
    allowed, reason, operation = await preflight(
        "assistant_message",
        "utterance",
        content=output,
        effect={"reversibility": "irreversible", "visible_to_user": True},
    )
    if allowed:
        emit(
            "run_end",
            output=output,
            caused_by=[operation.get("request_event_id")],
            metadata=operation,
        )
        print(result.output)
    else:
        emit("run_end", output="refused", reason=reason)
        print(f"Assistant output refused by monitor: {reason}")


if __name__ == "__main__":
    asyncio.run(main())
