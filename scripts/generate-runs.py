#!/usr/bin/env python3
"""Populate a large, random AngryRobot graph through the live backend API.

Posts multi-step runs to ``/api/runs/{run_id}/events``. Each run contains a
random cover story (4-6 events) followed by a trigger that should escalate
to L3+. Runs are linked to previous runs by reusing agents/targets/tools/
channels and by ``derived_from`` references.

The backend must already be running (``make up``). If ``TYPESAFE_API_KEY`` is
configured in the backend's ``.env``, Jev classifies live; otherwise the
generator grows the graph but actions are not dispatched automatically.

Examples
--------
    # Initial burst of 8 runs, fast
    python scripts/generate-runs.py --runs 8 --delay 0

    # Add one run slowly and watch it expand in the dashboard
    python scripts/generate-runs.py --runs 1 --delay 0.5

    # Big graph: 30 runs, 5-8 cover events each
    python scripts/generate-runs.py --runs 30 --min-cover 5 --max-cover 8 --delay 0.05
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_BACKEND = os.environ.get("BACKEND_URL", "http://localhost:8000")


# Load the shared generator without importing the full backend package.
_trace_generator_path = Path(__file__).resolve().parent.parent / "backend" / "app" / "evals" / "trace_generator.py"
_spec = importlib.util.spec_from_file_location("trace_generator", _trace_generator_path)
_trace_generator = importlib.util.module_from_spec(_spec)
sys.modules["trace_generator"] = _trace_generator
_spec.loader.exec_module(_trace_generator)

build_run = _trace_generator.build_run
normalize_event_payload = _trace_generator.normalize_event_payload


def _post(url: str, payload: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["X-Dispatch-Token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            detail = json.loads(body)
        except json.JSONDecodeError:
            detail = body
        raise RuntimeError(f"{exc.code} {exc.reason}: {detail}") from exc


def _get(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode()
        return json.loads(body) if body else {}


def populate(
    backend: str,
    runs: int,
    min_cover: int,
    max_cover: int,
    delay: float,
    dispatch_token: str | None,
    scenario: str | None,
    verbose: bool,
) -> dict[str, Any]:
    health = _get(f"{backend}/health")
    if health.get("status") != "ok":
        raise RuntimeError(f"backend is not healthy: {health}")

    derived_pool: list[str] = []
    agent_pool: list[str] = []
    channel_pool: list[str] = []
    target_pool: list[str] = []
    tool_pool: list[str] = []

    created: list[str] = []
    event_count = 0
    dispatched = 0

    for i in range(runs):
        run_id = f"run-{i:04d}-{random.randint(1000, 9999)}"
        events, agent, channel, _session_id = build_run(
            run_id,
            agent_pool,
            channel_pool,
            target_pool,
            tool_pool,
            derived_pool,
            min_cover,
            max_cover,
            scenario=scenario,
        )

        final_level = 0
        final_intent: str | None = None
        node_id: str | None = None
        for event in events:
            normalize_event_payload(event)
            try:
                result = _post(f"{backend}/api/runs/{run_id}/events", event)
            except RuntimeError as exc:
                print(f"  error posting {event['event_id']}: {exc}", file=sys.stderr)
                continue
            final_level = max(final_level, result.get("level", 0))
            final_intent = result.get("intent") or final_intent
            node_id = result.get("node_id") or node_id
            event_count += 1
            derived_pool.append(event["event_id"])
            if event.get("agent"):
                agent_pool.append(event["agent"])
            if event.get("channel"):
                channel_pool.append(event["channel"])
            if event.get("target"):
                target_pool.append(event["target"])
            if event.get("tool"):
                tool_pool.append(event["tool"])
            if delay:
                time.sleep(delay)

        # If the backend has no live Jev, try to drive the action ladder
        # manually so the side panel still shows protective actions.
        if dispatch_token and final_level >= 3 and node_id:
            try:
                _post(
                    f"{backend}/api/demo/incidents/{run_id}/dispatch",
                    {"level": final_level, "intent": final_intent or "critical agent activity"},
                    token=dispatch_token,
                )
                dispatched += 1
            except RuntimeError as exc:
                if verbose:
                    print(f"  dispatch failed for {run_id}: {exc}", file=sys.stderr)

        created.append(run_id)
        if verbose:
            print(f"created {run_id}: {len(events)} events -> L{final_level}")

    return {
        "runs": created,
        "events": event_count,
        "dispatched": dispatched,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5, help="number of runs to create")
    parser.add_argument("--min-cover", type=int, default=4, help="minimum cover events before trigger")
    parser.add_argument("--max-cover", type=int, default=6, help="maximum cover events before trigger")
    parser.add_argument("--delay", type=float, default=0.05, help="seconds between events")
    parser.add_argument("--backend", default=DEFAULT_BACKEND, help="backend base URL")
    parser.add_argument("--token", default=os.environ.get("ACTION_DISPATCH_TOKEN"), help="dispatch token for manual action ladder")
    parser.add_argument("--scenario", choices=["exfil", "lateral", "forge", "memory_poison"], default=None, help="force a scenario")
    parser.add_argument("--seed", type=int, default=None, help="random seed")
    parser.add_argument("-v", "--verbose", action="store_true", help="print per-run progress")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    report = populate(
        backend=args.backend,
        runs=args.runs,
        min_cover=args.min_cover,
        max_cover=args.max_cover,
        delay=args.delay,
        dispatch_token=args.token,
        scenario=args.scenario,
        verbose=args.verbose,
    )

    print(json.dumps(report, indent=2))
    print(f"\nOpen Neo4j browser at http://localhost:7474 and run:")
    print(f"  MATCH (r:Run) RETURN count(r) AS runs")
    print(f"  MATCH (e:Event) RETURN count(e) AS events")


if __name__ == "__main__":
    main()
