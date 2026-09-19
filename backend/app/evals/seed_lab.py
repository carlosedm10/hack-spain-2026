"""Bulk-replay eval traces into the live JSONL + Neo4j monitor.

Does not call graph.clear(). Unique run_ids avoid duplicate-event skips.
Jev stays degraded unless --live-jev is passed (thousands of live calls).
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from typing import Any
from uuid import uuid4

import httpx

from app.config import settings
from app.evals.happyrobot_cases import HAPPYROBOT_CLUSTERS
from app.evals.replay import event_payload
from app.evals.sentinel_cases import SENTINEL_CLUSTERS
from app.runs import service


def _run_id(prefix: str, cluster_id: str, trace_id: str) -> str:
    return f"{prefix}:{cluster_id}:{trace_id}:{uuid4().hex[:8]}"


async def _ingest_with_counts(
    client: httpx.AsyncClient,
    cluster,
    trace,
    run_id: str,
) -> dict[str, Any]:
    findings: Counter[str] = Counter()
    events = 0
    for action in trace.events:
        result = await service.ingest(
            run_id,
            event_payload(cluster, action, run_id=run_id),
            client,
        )
        events += 1
        for finding in result.get("findings") or []:
            rule = finding.get("rule_id")
            if rule:
                findings[rule] += 1
    return {"run_id": run_id, "events": events, "findings": findings}


async def _cross_run_pairs(client: httpx.AsyncClient) -> dict[str, Any]:
    """E4/P3 cross_run=true: two run_ids linked by target or derived_from."""
    e4 = next(c for c in SENTINEL_CLUSTERS if c.id == "hr-sentinel-e4-cross-channel")
    p3 = next(c for c in SENTINEL_CLUSTERS if c.id == "hr-sentinel-p3-memory-prop")
    extra: Counter[str] = Counter()
    events = 0

    source_run = _run_id("seedx", e4.id, "channel-a")
    dest_run = _run_id("seedx", e4.id, "channel-b")
    slack_write = next(
        event
        for event in next(t for t in e4.traces if t.id == "e4-covert").events
        if event.id == "e4-slack-mem"
    )
    email_write = next(
        event
        for event in next(t for t in e4.traces if t.id == "e4-covert").events
        if event.id == "e4-email-mem"
    )
    for action, run in ((slack_write, source_run), (email_write, dest_run)):
        result = await service.ingest(run, event_payload(e4, action, run_id=run), client)
        events += 1
        for finding in result.get("findings") or []:
            extra[finding.get("rule_id") or "unknown"] += 1

    p3_covert = next(t for t in p3.traces if t.id == "p3-covert")
    mem = next(event for event in p3_covert.events if event.id == "p3-mread")
    sched = next(event for event in p3_covert.events if event.id == "p3-sched")
    mem_run = _run_id("seedx", p3.id, "memory")
    effect_run = _run_id("seedx", p3.id, "effect")
    mem_result = await service.ingest(mem_run, event_payload(p3, mem, run_id=mem_run), client)
    events += 1
    payload = event_payload(p3, sched, run_id=effect_run)
    payload["derived_from"] = [f"{mem_run}:{mem.id}"]
    effect_result = await service.ingest(effect_run, payload, client)
    events += 1
    for result in (mem_result, effect_result):
        for finding in result.get("findings") or []:
            extra[finding.get("rule_id") or "unknown"] += 1

    return {
        "runs": 4,
        "events": events,
        "findings": extra,
        "note": "E4/P3 cross_run depends on Sentinel's linked neighborhood (shared target or derived_from), not process-global history",
    }


async def seed(*, repeats: int, include_happyrobot: bool, live_jev: bool) -> dict[str, Any]:
    original_key = settings.typesafe_api_key
    if not live_jev:
        settings.typesafe_api_key = ""
    try:
        clusters = list(SENTINEL_CLUSTERS)
        if include_happyrobot:
            clusters.extend(HAPPYROBOT_CLUSTERS)

        totals = Counter()
        findings: Counter[str] = Counter()
        runs = 0
        async with httpx.AsyncClient() as client:
            for round_id in range(repeats):
                for cluster in clusters:
                    for trace in cluster.traces:
                        run_id = _run_id(f"seed{round_id}", cluster.id, trace.id)
                        result = await _ingest_with_counts(client, cluster, trace, run_id)
                        runs += 1
                        totals["events"] += result["events"]
                        findings.update(result["findings"])
            cross = await _cross_run_pairs(client)
            runs += int(cross["runs"])
            totals["events"] += int(cross["events"])
            findings.update(cross["findings"])

        return {
            "runs": runs,
            "events": totals["events"],
            "findings_by_rule": dict(findings),
            "repeats": repeats,
            "include_happyrobot": include_happyrobot,
            "live_jev": live_jev,
            "cross_run": cross["note"],
        }
    finally:
        settings.typesafe_api_key = original_key


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the monitor lab with long Sentinel traces")
    parser.add_argument("--repeats", type=int, default=4, help="Replay each trace this many times")
    parser.add_argument(
        "--no-happyrobot",
        action="store_true",
        help="Skip the 72 HappyRobot traces",
    )
    parser.add_argument(
        "--live-jev",
        action="store_true",
        help="Call TypeSafe Jev on every event (slow, requires TYPESAFE_API_KEY)",
    )
    args = parser.parse_args()
    report = asyncio.run(
        seed(
            repeats=max(1, args.repeats),
            include_happyrobot=not args.no_happyrobot,
            live_jev=args.live_jev,
        )
    )
    print(
        "seed-lab "
        f"runs={report['runs']} events={report['events']} "
        f"findings={report['findings_by_rule']}"
    )
    print(f"cross_run: {report['cross_run']}")
    print(f"live_jev={report['live_jev']} repeats={report['repeats']}")


if __name__ == "__main__":
    main()
