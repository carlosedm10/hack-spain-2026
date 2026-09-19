from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from neo4j import AsyncDriver, AsyncGraphDatabase

from app.classification.models import Level
from app.config import settings
from app.events import MonitorEvent
from app.monitor.models import DriftState, GateDecision, MonitorAssessment
from app.monitor.neighborhood import RUN_WINDOW


@dataclass(frozen=True)
class ClassificationStep:
    level: Level
    threshold: float
    intent: str | None
    event: dict[str, Any]


class Neo4jGraphStore:
    """Persistent event/entity graph. JSONL remains the append-only recovery tape."""

    def __init__(self) -> None:
        self._driver: AsyncDriver | None = None

    @property
    def enabled(self) -> bool:
        return settings.neo4j_enabled

    def _get_driver(self) -> AsyncDriver:
        if self._driver is None:
            self._driver = AsyncGraphDatabase.driver(
                settings.neo4j_uri,
                auth=(settings.neo4j_user, settings.neo4j_password),
            )
        return self._driver

    async def close(self) -> None:
        if self._driver is not None:
            await self._driver.close()
            self._driver = None

    async def verify(self) -> None:
        if self.enabled:
            await self._get_driver().verify_connectivity()

    async def setup(self) -> None:
        if not self.enabled:
            return
        statements = [
            "CREATE CONSTRAINT run_id IF NOT EXISTS FOR (r:Run) REQUIRE r.id IS UNIQUE",
            "CREATE CONSTRAINT event_id IF NOT EXISTS FOR (e:Event) REQUIRE e.id IS UNIQUE",
            "CREATE CONSTRAINT entity_id IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
            (
                "CREATE CONSTRAINT assessment_id IF NOT EXISTS "
                "FOR (a:Assessment) REQUIRE a.id IS UNIQUE"
            ),
            (
                "CREATE CONSTRAINT stream_message_id IF NOT EXISTS "
                "FOR (m:StreamMessage) REQUIRE m.id IS UNIQUE"
            ),
            "CREATE INDEX event_run_id IF NOT EXISTS FOR (e:Event) ON (e.run_id)",
            (
                "CREATE INDEX stream_run_sequence IF NOT EXISTS "
                "FOR (m:StreamMessage) ON (m.run_id, m.sequence, m.position)"
            ),
        ]
        async with self._get_driver().session(database=settings.neo4j_database) as session:
            for statement in statements:
                await session.run(statement)

    async def persist(self, event: MonitorEvent, assessment: MonitorAssessment) -> None:
        if not self.enabled:
            return
        query = """
        MERGE (run:Run {id: $run_id})
          ON CREATE SET run.created_at = $timestamp, run.level = 0
        SET run.updated_at = $timestamp,
            run.level = CASE WHEN run.level < $incident_level
                             THEN $incident_level ELSE run.level END
        MERGE (event:Event {id: $event_id})
        SET event += $event_properties
        MERGE (run)-[:HAS_EVENT]->(event)
        WITH run, event
        OPTIONAL MATCH (previous:Event {run_id: $run_id})
        WHERE previous.sequence = $previous_sequence
        FOREACH (_ IN CASE WHEN previous IS NULL THEN [] ELSE [1] END |
          MERGE (previous)-[:NEXT]->(event)
        )
        WITH run, event
        MERGE (assessment:Assessment {id: $assessment_id})
        SET assessment += $assessment_properties
        MERGE (event)-[:HAS_ASSESSMENT]->(assessment)
        RETURN event.id AS id
        """
        params = {
            "run_id": event.run_id,
            "timestamp": event.timestamp.isoformat(),
            "incident_level": int(assessment.gate.incident_level),
            "event_id": event.id,
            "event_properties": _without_none(event.graph_properties()),
            "previous_sequence": (event.sequence - 1) if event.sequence is not None else -1,
            "assessment_id": f"{event.id}:assessment",
            "assessment_properties": {
                "drift_json": assessment.drift.model_dump_json(),
                "findings_json": json.dumps(
                    [item.model_dump(mode="json") for item in assessment.findings],
                    sort_keys=True,
                ),
                "gate_json": assessment.gate.model_dump_json(),
                "jev_answers_json": json.dumps(assessment.jev_answers, default=str, sort_keys=True),
                "jev_model": assessment.jev_model,
                "jev_latency_ms": assessment.jev_latency_ms,
                "dispatch_actions_json": json.dumps(
                    assessment.dispatch_actions, default=str, sort_keys=True
                ),
            },
        }
        async with self._get_driver().session(database=settings.neo4j_database) as session:
            await session.run(query, **params)
            await self._persist_entities(session, event)
            await self._persist_causal_edges(session, event)

    async def _persist_entities(self, session, event: MonitorEvent) -> None:
        entities = [
            ("agent", event.agent),
            ("tool", f"tool:{event.tool}" if event.tool else None),
            ("target", event.target),
            ("channel", f"channel:{event.channel}" if event.channel else None),
        ]
        for kind, entity_id in entities:
            if not entity_id:
                continue
            await session.run(
                """
                MERGE (entity:Entity {id: $entity_id})
                SET entity.kind = $kind
                WITH entity
                MATCH (event:Event {id: $event_id})
                MERGE (event)-[:TOUCHES {role: $kind}]->(entity)
                """,
                entity_id=entity_id,
                kind=kind,
                event_id=event.id,
            )
        if event.agent and event.tool:
            await session.run(
                """
                MERGE (agent:Entity {id: $agent})
                SET agent.kind = 'agent'
                MERGE (tool:Entity {id: $tool})
                SET tool.kind = 'tool'
                MERGE (agent)-[calls:CALLS {event_id: $event_id}]->(tool)
                SET calls.run_id = $run_id, calls.phase = $phase
                """,
                agent=event.agent,
                tool=f"tool:{event.tool}",
                event_id=event.id,
                run_id=event.run_id,
                phase=event.phase.value,
            )
        if event.agent and event.target:
            relation = _target_relation(event.kind)
            await session.run(
                f"""
                MERGE (agent:Entity {{id: $agent}})
                SET agent.kind = 'agent'
                MERGE (target:Entity {{id: $target}})
                ON CREATE SET target.kind = 'target'
                MERGE (agent)-[edge:{relation} {{event_id: $event_id}}]->(target)
                SET edge.run_id = $run_id,
                    edge.phase = $phase,
                    edge.scope = $scope,
                    edge.amount = $amount
                """,
                agent=event.agent,
                target=event.target,
                event_id=event.id,
                run_id=event.run_id,
                phase=event.phase.value,
                scope=event.effect.scope,
                amount=event.effect.amount,
            )

    async def _persist_causal_edges(self, session, event: MonitorEvent) -> None:
        for relation, ids in (
            ("CAUSED_BY", event.caused_by),
            ("DERIVED_FROM", event.derived_from),
        ):
            for source_id in ids:
                await session.run(
                    f"""
                    MATCH (event:Event {{id: $event_id}})
                    MERGE (source:Event {{id: $source_id}})
                    ON CREATE SET source.placeholder = true
                    MERGE (event)-[:{relation}]->(source)
                    """,
                    event_id=event.id,
                    source_id=source_id,
                )

    async def _run_exists(self, run_id: str) -> bool:
        query = "MATCH (run:Run {id: $run_id}) RETURN run.id AS id"
        async with self._get_driver().session(database=settings.neo4j_database) as session:
            result = await session.run(query, run_id=run_id)
            return await result.single() is not None

    async def timeline(self, run_id: str) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        query = """
        MATCH (:Run {id: $run_id})-[:HAS_EVENT]->(event:Event)
        OPTIONAL MATCH (event)-[:HAS_ASSESSMENT]->(assessment:Assessment)
        RETURN properties(event) AS event, properties(assessment) AS assessment
        ORDER BY event.sequence, event.timestamp
        """
        async with self._get_driver().session(database=settings.neo4j_database) as session:
            result = await session.run(query, run_id=run_id)
            return [dict(record) async for record in result]

    async def restore_monitor_state(
        self, run_id: str, limit: int = 100
    ) -> tuple[list[MonitorEvent], DriftState | None]:
        if not await self._run_exists(run_id):
            return [], None
        rows = await self.timeline(run_id)
        recent = rows[-limit:]
        events: list[MonitorEvent] = []
        latest_drift: DriftState | None = None
        for row in recent:
            event_properties = row.get("event") or {}
            if event_properties.get("placeholder"):
                continue
            try:
                events.append(_monitor_event(event_properties))
            except (TypeError, ValueError):
                continue
            assessment = row.get("assessment") or {}
            drift_json = assessment.get("drift_json")
            if isinstance(drift_json, str):
                latest_drift = DriftState.model_validate_json(drift_json)
        return events, latest_drift

    async def linked_run_ids(self, event: MonitorEvent, limit: int = RUN_WINDOW) -> list[str]:
        if not self.enabled:
            return []
        entity_ids = [
            value
            for value in (
                event.agent,
                f"tool:{event.tool}" if event.tool else None,
                event.target,
                f"channel:{event.channel}" if event.channel else None,
            )
            if value
        ]
        ref_ids = [event.id, *event.caused_by, *event.derived_from]
        if not entity_ids and not event.caused_by and not event.derived_from:
            return []
        query = """
        CALL () {
          UNWIND $entity_ids AS eid
          MATCH (entity:Entity {id: eid})<-[:TOUCHES]-(other:Event)
          WHERE other.run_id <> $run_id AND coalesce(other.placeholder, false) = false
          RETURN DISTINCT other.run_id AS run_id
          UNION
          MATCH (seed:Event {run_id: $run_id})-[:TOUCHES]->(entity:Entity)<-[:TOUCHES]-(other:Event)
          WHERE other.run_id <> $run_id AND coalesce(other.placeholder, false) = false
          RETURN DISTINCT other.run_id AS run_id
          UNION
          UNWIND $ref_ids AS rid
          MATCH (ref:Event {id: rid})
          OPTIONAL MATCH (ref)-[:CAUSED_BY]->(caused:Event)
          OPTIONAL MATCH (cause:Event)-[:CAUSED_BY]->(ref)
          WITH $run_id AS run_id,
               collect(DISTINCT caused) + collect(DISTINCT cause) + collect(DISTINCT ref) AS nodes
          UNWIND nodes AS other
          WITH run_id, other
          WHERE other IS NOT NULL AND other.run_id <> run_id
            AND coalesce(other.placeholder, false) = false
          RETURN DISTINCT other.run_id AS run_id
        }
        RETURN run_id
        ORDER BY run_id
        LIMIT $limit
        """
        params = {
            "run_id": event.run_id,
            "entity_ids": entity_ids,
            "ref_ids": ref_ids,
            "limit": limit,
        }
        async with self._get_driver().session(database=settings.neo4j_database) as session:
            result = await session.run(query, **params)
            return [record["run_id"] async for record in result if record.get("run_id")]

    async def all_run_ids(self) -> list[str]:
        if not self.enabled:
            return []
        query = "MATCH (r:Run) RETURN r.id AS run_id ORDER BY r.id"
        async with self._get_driver().session(database=settings.neo4j_database) as session:
            result = await session.run(query)
            return [record["run_id"] async for record in result if record.get("run_id")]

    async def restore_neighborhood(
        self, event: MonitorEvent, limit: int = RUN_WINDOW
    ) -> dict[str, list[MonitorEvent]]:
        histories: dict[str, list[MonitorEvent]] = {}
        for run_id in await self.linked_run_ids(event, limit=limit):
            history, _ = await self.restore_monitor_state(run_id, limit=limit)
            if history:
                histories[run_id] = history
        return histories

    async def restore_classification(self, run_id: str) -> list[ClassificationStep]:
        if not await self._run_exists(run_id):
            return []
        rows = await self.timeline(run_id)
        steps: list[ClassificationStep] = []
        for row in rows:
            event_properties = row.get("event") or {}
            if event_properties.get("placeholder"):
                continue
            assessment = row.get("assessment") or {}
            if not assessment:
                continue
            try:
                monitor_event = _monitor_event(event_properties)
            except (TypeError, ValueError):
                continue
            gate = _gate_from_assessment(assessment)
            level = gate.incident_level if gate else Level.NONE
            threshold = gate.event_risk if gate else 0.0
            if gate and gate.trajectory_risk > threshold:
                threshold = gate.trajectory_risk
            if level >= Level.MILD and threshold < settings.action_gate:
                threshold = max(threshold, settings.action_gate)
            intent = _intent_from_assessment(assessment)
            steps.append(
                ClassificationStep(
                    level=level,
                    threshold=float(threshold),
                    intent=intent,
                    event=monitor_event.model_dump(mode="json"),
                )
            )
        return steps

    async def graph(self, run_id: str) -> dict[str, list[dict[str, Any]]]:
        if not self.enabled:
            return {"nodes": [], "edges": []}
        query = """
        MATCH (:Run {id: $run_id})-[:HAS_EVENT]->(event:Event)
        OPTIONAL MATCH (event)-[relationship]->(target)
        RETURN collect(DISTINCT {
          id: event.id, labels: labels(event), properties: properties(event)
        }) + collect(DISTINCT {
          id: target.id, labels: labels(target), properties: properties(target)
        }) AS nodes,
        collect(DISTINCT {
          source: event.id, target: target.id, type: type(relationship),
          properties: properties(relationship)
        }) AS edges
        """
        async with self._get_driver().session(database=settings.neo4j_database) as session:
            result = await session.run(query, run_id=run_id)
            record = await result.single()
            if record is None:
                return {"nodes": [], "edges": []}
            nodes = [item for item in record["nodes"] if item.get("id")]
            edges = [
                item
                for item in record["edges"]
                if item.get("source") and item.get("target") and item.get("type")
            ]
            entity_result = await session.run(
                """
                MATCH (source:Entity)-[relationship]->(target:Entity)
                WHERE relationship.run_id = $run_id
                RETURN source.id AS source_id, labels(source) AS source_labels,
                       properties(source) AS source_properties,
                       target.id AS target_id, labels(target) AS target_labels,
                       properties(target) AS target_properties,
                       type(relationship) AS relationship_type,
                       properties(relationship) AS relationship_properties
                """,
                run_id=run_id,
            )
            async for entity_record in entity_result:
                nodes.extend(
                    [
                        {
                            "id": entity_record["source_id"],
                            "labels": entity_record["source_labels"],
                            "properties": entity_record["source_properties"],
                        },
                        {
                            "id": entity_record["target_id"],
                            "labels": entity_record["target_labels"],
                            "properties": entity_record["target_properties"],
                        },
                    ]
                )
                edges.append(
                    {
                        "source": entity_record["source_id"],
                        "target": entity_record["target_id"],
                        "type": entity_record["relationship_type"],
                        "properties": entity_record["relationship_properties"],
                    }
                )
            unique_edges = _dedupe_edges(edges)
            for edge in unique_edges:
                role = (edge.get("properties") or {}).get("role")
                suffix = f":{role}" if role else ""
                edge["id"] = f"{edge['type']}:{edge['source']}:{edge['target']}{suffix}"
            return {"nodes": _dedupe(nodes, "id"), "edges": unique_edges}

    async def stats(self) -> dict[str, int]:
        if not self.enabled:
            return {"nodes": 0, "edges": 0, "events": 0, "runs": 0}
        async with self._get_driver().session(database=settings.neo4j_database) as session:
            nodes = await session.run("MATCH (n) RETURN count(n) AS n")
            node_row = await nodes.single()
            rels = await session.run("MATCH ()-[r]->() RETURN count(r) AS n")
            rel_row = await rels.single()
            events = await session.run("MATCH (e:Event) RETURN count(e) AS n")
            event_row = await events.single()
            runs = await session.run("MATCH (r:Run) RETURN count(r) AS n")
            run_row = await runs.single()
        return {
            "nodes": int(node_row["n"]) if node_row else 0,
            "edges": int(rel_row["n"]) if rel_row else 0,
            "events": int(event_row["n"]) if event_row else 0,
            "runs": int(run_row["n"]) if run_row else 0,
        }

    async def persist_stream(self, envelopes: list[dict[str, Any]]) -> None:
        if not self.enabled or not envelopes:
            return
        query = """
        UNWIND $messages AS message
        MERGE (stream:StreamMessage {id: message.stream_id})
        SET stream.run_id = message.run_id,
            stream.sequence = message.sequence,
            stream.position = message.position,
            stream.type = message.type,
            stream.emitted_at = message.emitted_at,
            stream.source_event_id = message.source_event_id,
            stream.payload_json = message.payload_json
        WITH stream, message
        MATCH (run:Run {id: message.run_id})
        MERGE (run)-[:HAS_STREAM_MESSAGE]->(stream)
        """
        messages = [
            {
                "stream_id": envelope["stream_id"],
                "run_id": envelope["run_id"],
                "sequence": envelope["sequence"],
                "position": envelope["position"],
                "type": envelope["type"],
                "emitted_at": envelope["emitted_at"],
                "source_event_id": envelope.get("source_event_id"),
                "payload_json": json.dumps(envelope, default=str, sort_keys=True),
            }
            for envelope in envelopes
        ]
        async with self._get_driver().session(database=settings.neo4j_database) as session:
            await session.run(query, messages=messages)

    async def stream_after(self, run_id: str, cursor: str | None = None) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        boundary = """
        OPTIONAL MATCH (cursor:StreamMessage {id: $cursor, run_id: $run_id})
        WITH cursor
        MATCH (:Run {id: $run_id})-[:HAS_STREAM_MESSAGE]->(message:StreamMessage)
        WHERE cursor IS NULL
           OR message.sequence > cursor.sequence
           OR (message.sequence = cursor.sequence AND message.position > cursor.position)
        RETURN message.payload_json AS payload
        ORDER BY message.sequence, message.position
        """
        async with self._get_driver().session(database=settings.neo4j_database) as session:
            result = await session.run(boundary, run_id=run_id, cursor=cursor)
            return [json.loads(record["payload"]) async for record in result]

    async def latest_cursor(self, run_id: str) -> str:
        if not self.enabled:
            return "0"
        query = """
        MATCH (:Run {id: $run_id})-[:HAS_STREAM_MESSAGE]->(message:StreamMessage)
        RETURN message.id AS cursor
        ORDER BY message.sequence DESC, message.position DESC
        LIMIT 1
        """
        async with self._get_driver().session(database=settings.neo4j_database) as session:
            result = await session.run(query, run_id=run_id)
            record = await result.single()
            return str(record["cursor"]) if record else "0"

    async def cursor_exists(self, run_id: str, cursor: str) -> bool:
        if not self.enabled:
            return cursor == "0"
        query = """
        MATCH (message:StreamMessage {id: $cursor, run_id: $run_id})
        RETURN count(message) > 0 AS exists
        """
        async with self._get_driver().session(database=settings.neo4j_database) as session:
            result = await session.run(query, run_id=run_id, cursor=cursor)
            record = await result.single()
            return bool(record and record["exists"])


def _without_none(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if item is not None}


def _dedupe(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    seen: set[Any] = set()
    unique: list[dict[str, Any]] = []
    for item in items:
        value = item.get(key)
        if value in seen:
            continue
        seen.add(value)
        unique.append(item)
    return unique


def _dedupe_edges(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, Any, Any, Any]] = set()
    unique: list[dict[str, Any]] = []
    for item in items:
        properties = item.get("properties") or {}
        key = (
            item.get("source"),
            item.get("target"),
            item.get("type"),
            properties.get("event_id"),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _target_relation(kind: str) -> str:
    if kind in {"file_read", "memory_read", "tool_read"}:
        return "READS"
    if kind in {"file_edit", "memory_write", "tool_write"}:
        return "WRITES"
    if kind == "schedule":
        return "SCHEDULES"
    if kind in {"utterance", "notification"}:
        return "SPEAKS_TO"
    return "TOUCHES_TARGET"


def _gate_from_assessment(assessment: dict[str, Any]) -> GateDecision | None:
    gate_json = assessment.get("gate_json")
    if not isinstance(gate_json, str) or not gate_json:
        return None
    try:
        return GateDecision.model_validate_json(gate_json)
    except ValueError:
        return None


def _intent_from_assessment(assessment: dict[str, Any]) -> str | None:
    answers_json = assessment.get("jev_answers_json")
    if not isinstance(answers_json, str) or not answers_json:
        return None
    try:
        answers = json.loads(answers_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(answers, dict):
        return None
    for key in ("intent", "chain_intent", "primary_intent"):
        value = answers.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            choice = value.get("choice") or value.get("value")
            if isinstance(choice, str) and choice:
                return choice
    return None


def _monitor_event(properties: dict[str, Any]) -> MonitorEvent:
    return MonitorEvent.model_validate(
        {
            "id": properties["id"],
            "run_id": properties["run_id"],
            "session_id": properties.get("session_id"),
            "sequence": properties.get("sequence"),
            "timestamp": properties["timestamp"],
            "kind": properties["kind"],
            "phase": properties["phase"],
            "origin": properties["origin"],
            "agent": properties.get("agent"),
            "tool": properties.get("tool"),
            "target": properties.get("target"),
            "channel": properties.get("channel"),
            "identity_state": properties.get("identity_state", "unknown"),
            "trust": properties.get("trust", "unknown"),
            "content": properties.get("content"),
            "args": json.loads(properties.get("args_json") or "{}"),
            "result": json.loads(properties.get("result_json") or "null"),
            "effect": json.loads(properties.get("effect_json") or "{}"),
            "caused_by": json.loads(properties.get("caused_by_json") or "[]"),
            "derived_from": json.loads(properties.get("derived_from_json") or "[]"),
            "policy_version": properties.get("policy_version"),
            "metadata": json.loads(properties.get("metadata_json") or "{}"),
            "raw": json.loads(properties.get("raw_json") or "{}"),
        }
    )


neo4j_graph = Neo4jGraphStore()
