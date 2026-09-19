from __future__ import annotations

import logging
from typing import Any

import httpx

from app.actions.models import DispatchAccepted
from app.classification import pipeline
from app.classification.models import Level
from app.dispatch import dispatcher
from app.events import EventPhase, MonitorEvent, normalize_event, redact_event
from app.graph import graph
from app.graph.neo4j import neo4j_graph
from app.monitor import monitor
from app.realtime import broker, build_envelopes
from app.runs import log

client_factory = httpx.AsyncClient
logger = logging.getLogger(__name__)


async def _hydrate_runtime(event: MonitorEvent) -> None:
    if not neo4j_graph.enabled:
        return
    run_id = event.run_id
    try:
        if not monitor.history(run_id):
            history, drift = await neo4j_graph.restore_monitor_state(run_id)
            monitor.restore(run_id, history, drift)
        neighborhood = await neo4j_graph.restore_neighborhood(event)
        for linked_run_id, history in neighborhood.items():
            if not monitor.history(linked_run_id):
                monitor.restore(linked_run_id, history, None)
        run_ids = {run_id, *neighborhood.keys()}
        for hydrate_run_id in run_ids:
            chained = [
                node
                for node in graph.run_nodes(hydrate_run_id)
                if node.id != f"run:{hydrate_run_id}"
            ]
            if chained:
                continue
            steps = await neo4j_graph.restore_classification(hydrate_run_id)
            graph.hydrate_run(hydrate_run_id, steps)
    except Exception:
        logger.exception("Could not hydrate runtime state for run %s", run_id)


async def ingest(
    run_id: str, event: dict[str, Any], client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    normalized = redact_event(normalize_event(run_id, event))
    normalized, is_new = log.append_event(normalized)
    normalized_payload = normalized.model_dump(mode="json")
    if not is_new:
        return {
            "level": int(graph.level(run_id)),
            "confidence": 0.0,
            "intent": None,
            "escalated": False,
            "degraded": False,
            "node_id": None,
            "event_id": normalized.id,
            "decision": "allow",
            "event_risk": 0.0,
            "trajectory_risk": 0.0,
            "reasons": ["duplicate_event"],
            "graph_persisted": True,
            "dispatch_actions": [],
            "jev_latency_ms": None,
            "duplicate": True,
            "drift": None,
            "findings": [],
        }
    owned = client is None
    if client is None:
        client = client_factory()
    await _hydrate_runtime(normalized)
    prepared = monitor.prepare(normalized)
    try:
        before_level = graph.level(run_id)
        verdict = await pipeline.evaluate(
            client,
            run_id,
            normalized_payload,
            monitor_context=prepared.context,
        )
    finally:
        if owned:
            await client.aclose()
    assessment = monitor.finalize(normalized, verdict, before_level, prepared)
    actions = dispatcher.handle(normalized, assessment)
    assessment.dispatch_actions = [action.model_dump(mode="json") for action in actions]
    effective_level = max(before_level, verdict.level, assessment.gate.incident_level)
    action_node = graph.last_action(run_id)
    if action_node is None and effective_level > Level.NONE:
        action_node = graph.append(
            run_id,
            level=effective_level,
            threshold=verdict.confidence,
            intent=verdict.intent,
            event=normalized_payload,
            action_id=None,
        )
    node_id = action_node.id if action_node is not None else None
    if action_node is not None and effective_level > action_node.level:
        graph.update(node_id, level=effective_level)

    incident_level = int(assessment.gate.incident_level)
    if (
        node_id is not None
        and not verdict.degraded
        and incident_level >= 1
    ):
        accepted = await dispatch_classified(run_id, incident_level, verdict.intent)
        if accepted is not None and accepted.planned_actions:
            graph.update(node_id, action_id=accepted.planned_actions[0].action_id)

    graph_persisted = True
    try:
        await neo4j_graph.persist(normalized, assessment)
    except Exception:
        graph_persisted = False
        logger.exception("Neo4j persistence failed for event %s", normalized.id)

    history = monitor.history(run_id)
    previous_event_id = history[-2].id if len(history) >= 2 else None
    await broker.publish(build_envelopes(normalized, assessment, previous_event_id))
    return {
        "level": int(effective_level),
        "confidence": verdict.confidence,
        "intent": verdict.intent,
        "escalated": effective_level > before_level,
        "degraded": verdict.degraded,
        "node_id": node_id,
        "event_id": normalized.id,
        "decision": assessment.gate.decision.value,
        "event_risk": assessment.gate.event_risk,
        "trajectory_risk": assessment.gate.trajectory_risk,
        "reasons": assessment.gate.reasons,
        "graph_persisted": graph_persisted,
        "dispatch_actions": assessment.dispatch_actions,
        "jev_latency_ms": verdict.latency_ms,
        "duplicate": False,
        "drift": assessment.drift.model_dump(mode="json"),
        "findings": [finding.model_dump(mode="json") for finding in assessment.findings],
    }


async def preflight(
    run_id: str, event: dict[str, Any], client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    payload = dict(event)
    payload["phase"] = EventPhase.REQUESTED.value
    return await ingest(run_id, payload, client)


async def dispatch_classified(
    run_id: str, level: int, intent: str | None
) -> DispatchAccepted | None:
    from app.actions.router import DispatchRequest, get_action_service

    return await get_action_service().dispatch(
        run_id,
        DispatchRequest(level=level, intent=intent),
    )
