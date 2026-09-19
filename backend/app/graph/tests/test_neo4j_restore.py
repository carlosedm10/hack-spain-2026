from __future__ import annotations

import json

from app.classification.models import Level
from app.graph.neo4j import _gate_from_assessment, _intent_from_assessment
from app.monitor.models import GateDecision, ToolDecision


def test_gate_and_intent_parsing_from_assessment_blob():
    gate = GateDecision(
        decision=ToolDecision.ALLOW,
        incident_level=Level.CRITICAL,
        event_risk=0.88,
        trajectory_risk=0.91,
        reasons=["sentinel"],
    )
    assessment = {
        "gate_json": gate.model_dump_json(),
        "jev_answers_json": json.dumps({"intent": {"type": "choice", "choice": "exfiltrate"}}),
    }
    parsed_gate = _gate_from_assessment(assessment)
    assert parsed_gate is not None
    assert parsed_gate.incident_level == Level.CRITICAL
    assert _intent_from_assessment(assessment) == "exfiltrate"


async def test_restore_classification_skips_rows_without_assessment(monkeypatch):
    from app.graph import neo4j as neo4j_module

    store = neo4j_module.Neo4jGraphStore()

    async def run_exists(_run_id: str) -> bool:
        return True

    async def timeline(_run_id: str):
        return [
            {
                "event": {
                    "id": "e1",
                    "run_id": "r1",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "kind": "file_read",
                    "phase": "completed",
                    "origin": "agent",
                    "args_json": "{}",
                    "result_json": "null",
                    "effect_json": "{}",
                    "caused_by_json": "[]",
                    "derived_from_json": "[]",
                    "metadata_json": "{}",
                    "raw_json": "{}",
                },
                "assessment": None,
            }
        ]

    monkeypatch.setattr(store, "_run_exists", run_exists)
    monkeypatch.setattr(store, "timeline", timeline)
    assert await store.restore_classification("r1") == []
