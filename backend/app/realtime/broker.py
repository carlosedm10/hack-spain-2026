from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from app.events import MonitorEvent
from app.graph.neo4j import neo4j_graph
from app.monitor.models import MonitorAssessment

logger = logging.getLogger(__name__)


class RealtimeBroker:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    def subscribe(self, run_id: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._subscribers[run_id].add(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        subscribers = self._subscribers.get(run_id)
        if subscribers is None:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(run_id, None)

    async def publish(self, envelopes: list[dict[str, Any]]) -> None:
        try:
            await neo4j_graph.persist_stream(envelopes)
        except Exception:
            logger.exception("Could not persist realtime stream envelopes")
        for envelope in envelopes:
            for queue in list(self._subscribers.get(envelope["run_id"], set())):
                try:
                    queue.put_nowait(envelope)
                except asyncio.QueueFull:
                    self.unsubscribe(envelope["run_id"], queue)


def build_envelopes(
    event: MonitorEvent,
    assessment: MonitorAssessment,
    previous_event_id: str | None = None,
) -> list[dict[str, Any]]:
    emitted_at = datetime.now(UTC).isoformat()
    sequence = event.sequence or 0
    payloads: list[tuple[str, dict[str, Any]]] = []

    event_node = {
        "id": event.id,
        "labels": ["Event"],
        "properties": event.graph_properties(),
    }
    assessment_id = f"{event.id}:assessment"
    assessment_node = {
        "id": assessment_id,
        "labels": ["Assessment"],
        "properties": {
            "id": assessment_id,
            "event_id": event.id,
            "run_id": event.run_id,
        },
    }
    payloads.extend(
        [
            (
                "graph.node.upserted",
                {
                    "node": {
                        "id": f"run:{event.run_id}",
                        "labels": ["Run"],
                        "properties": {
                            "id": event.run_id,
                            "level": int(assessment.gate.incident_level),
                        },
                    }
                },
            ),
            ("graph.node.upserted", {"node": event_node}),
            ("graph.node.upserted", {"node": assessment_node}),
            (
                "graph.edge.upserted",
                {
                    "edge": _edge(
                        "HAS_EVENT",
                        f"run:{event.run_id}",
                        event.id,
                    )
                },
            ),
            (
                "graph.edge.upserted",
                {"edge": _edge("HAS_ASSESSMENT", event.id, assessment_id)},
            ),
            (
                "drift.updated",
                {"event_id": event.id, "drift": assessment.drift.model_dump(mode="json")},
            ),
            (
                "jev.assessed",
                {
                    "event_id": event.id,
                    "assessment_id": assessment_id,
                    "answers": assessment.jev_answers,
                    "model": assessment.jev_model,
                    "latency_ms": assessment.jev_latency_ms,
                    "degraded_reason": assessment.jev_degraded_reason,
                },
            ),
        ]
    )
    if previous_event_id:
        payloads.append(
            (
                "graph.edge.upserted",
                {"edge": _edge("NEXT", previous_event_id, event.id)},
            )
        )
    for source_id in event.caused_by:
        payloads.append(
            (
                "graph.edge.upserted",
                {"edge": _edge("CAUSED_BY", event.id, source_id)},
            )
        )
    for source_id in event.derived_from:
        payloads.append(
            (
                "graph.edge.upserted",
                {"edge": _edge("DERIVED_FROM", event.id, source_id)},
            )
        )
    for role, entity_id in (
        ("agent", event.agent),
        ("tool", f"tool:{event.tool}" if event.tool else None),
        ("target", event.target),
        ("channel", f"channel:{event.channel}" if event.channel else None),
    ):
        if not entity_id:
            continue
        payloads.extend(
            [
                (
                    "graph.node.upserted",
                    {
                        "node": {
                            "id": entity_id,
                            "labels": ["Entity"],
                            "properties": {"id": entity_id, "kind": role},
                        }
                    },
                ),
                (
                    "graph.edge.upserted",
                    {
                        "edge": _edge(
                            "TOUCHES",
                            event.id,
                            entity_id,
                            {"role": role},
                        )
                    },
                ),
            ]
        )
    for finding in assessment.findings:
        payloads.append(
            (
                "sentinel.finding",
                {
                    "finding": {
                        "id": f"{event.id}:{finding.rule_id}",
                        "event_id": event.id,
                        **finding.model_dump(mode="json"),
                    }
                },
            )
        )
    payloads.append(
        (
            "gate.decided",
            {"event_id": event.id, "gate": assessment.gate.model_dump(mode="json")},
        )
    )
    for action in assessment.dispatch_actions:
        event_type = "counter.updated" if action["kind"] == "counter_action" else "dispatch.action"
        key = "counter" if event_type == "counter.updated" else "action"
        payloads.append((event_type, {key: action}))
    payloads.append(
        (
            "run.updated",
            {
                "run": {
                    "run_id": event.run_id,
                    "level": int(assessment.gate.incident_level),
                    "decision": assessment.gate.decision.value,
                    "latest_sequence": event.sequence,
                    "latest_event_id": event.id,
                    "updated_at": event.timestamp.isoformat(),
                    "degraded": assessment.jev_model is None,
                    "degraded_reason": assessment.jev_degraded_reason,
                }
            },
        )
    )

    envelopes: list[dict[str, Any]] = []
    for position, (event_type, data) in enumerate(payloads):
        stream_id = f"{event.id}:{position}"
        envelopes.append(
            {
                "schema_version": 1,
                "stream_id": stream_id,
                "type": event_type,
                "run_id": event.run_id,
                "emitted_at": emitted_at,
                "source_event_id": event.id,
                "sequence": sequence,
                "position": position,
                "data": data,
            }
        )
    return envelopes


def _edge(
    edge_type: str,
    source: str,
    target: str,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    role = (properties or {}).get("role")
    suffix = f":{role}" if role else ""
    return {
        "id": f"{edge_type}:{source}:{target}{suffix}",
        "source": source,
        "target": target,
        "type": edge_type,
        "properties": properties or {},
    }


broker = RealtimeBroker()
