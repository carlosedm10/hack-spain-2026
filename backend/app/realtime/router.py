from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.graph import graph
from app.graph.neo4j import neo4j_graph
from app.realtime import broker

router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.get("/{run_id}/snapshot")
async def snapshot(run_id: str) -> dict[str, Any]:
    persistent_graph = await neo4j_graph.graph(run_id)
    timeline = await neo4j_graph.timeline(run_id)
    if not persistent_graph["nodes"]:
        persistent_graph = _memory_graph(run_id)

    latest = timeline[-1] if timeline else {}
    assessment = latest.get("assessment") or {}
    event = latest.get("event") or {}
    drift = _json(assessment.get("drift_json"), {})
    findings = _json(assessment.get("findings_json"), [])
    gate = _json(assessment.get("gate_json"), {})
    jev_answers = _json(assessment.get("jev_answers_json"), {})
    actions = _json(assessment.get("dispatch_actions_json"), [])
    cursor = await neo4j_graph.latest_cursor(run_id)

    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "cursor": cursor,
        "run": {
            "run_id": run_id,
            "level": int(graph.level(run_id)),
            "decision": gate.get("decision", "allow"),
            "status": "active",
            "latest_sequence": event.get("sequence"),
            "latest_event_id": event.get("id"),
            "updated_at": event.get("timestamp"),
            "degraded": not bool(assessment),
            "graph_persisted": bool(timeline),
        },
        "graph": persistent_graph,
        "monitor": {
            "drift": drift,
            "latest_jev": {
                "event_id": event.get("id"),
                "assessment_id": assessment.get("id"),
                "answers": jev_answers,
                "model": assessment.get("jev_model"),
                "latency_ms": assessment.get("jev_latency_ms"),
            }
            if assessment
            else None,
            "findings": findings,
            "latest_gate": {"event_id": event.get("id"), "gate": gate} if gate else None,
        },
        "dispatch_actions": [
            action for action in actions if action.get("kind") != "counter_action"
        ],
        "counters": [action for action in actions if action.get("kind") == "counter_action"],
    }


@router.get("/{run_id}/stream")
async def realtime_stream(
    run_id: str,
    after: str | None = Query(default=None),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    cursor = last_event_id or after
    if cursor not in {None, "0"} and not await neo4j_graph.cursor_exists(run_id, cursor):
        raise HTTPException(
            status_code=409,
            detail={
                "type": "urn:hackspain:realtime:cursor-reset-required",
                "title": "Realtime cursor is no longer available",
                "detail": "Fetch a new snapshot before reconnecting.",
                "run_id": run_id,
            },
        )

    async def events() -> AsyncIterator[str]:
        queue = broker.subscribe(run_id)
        try:
            for envelope in await neo4j_graph.stream_after(run_id, cursor):
                yield _sse(envelope)
            while True:
                try:
                    envelope = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield f": heartbeat {datetime.now(UTC).isoformat()}\n\n"
                    continue
                yield _sse(envelope)
        finally:
            broker.unsubscribe(run_id, queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


def _sse(envelope: dict[str, Any]) -> str:
    payload = json.dumps(envelope, default=str, separators=(",", ":"))
    return f"id: {envelope['stream_id']}\nevent: {envelope['type']}\ndata: {payload}\n\n"


def _json(value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        return value if value is not None else default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _memory_graph(run_id: str) -> dict[str, list[dict[str, Any]]]:
    nodes = []
    edges = []
    for node in graph.run_nodes(run_id):
        nodes.append(
            {
                "id": node.id,
                "labels": ["Event" if node.event else "Run"],
                "properties": {
                    "id": node.id,
                    "run_id": node.run_id,
                    "level": int(node.level),
                    "threshold": node.threshold,
                    "intent": node.intent,
                    "event": node.event,
                },
            }
        )
        for neighbor in node.neighbors:
            edges.append(
                {
                    "id": f"RELATED:{node.id}:{neighbor.id}",
                    "source": node.id,
                    "target": neighbor.id,
                    "type": "RELATED",
                    "properties": {},
                }
            )
    return {"nodes": nodes, "edges": edges}
