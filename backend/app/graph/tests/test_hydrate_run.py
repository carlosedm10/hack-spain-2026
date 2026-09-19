from __future__ import annotations

from app.classification.models import Level
from app.graph import ActionGraph
from app.graph.neo4j import ClassificationStep


def test_hydrate_run_rebuilds_chain_without_duplicating():
    graph = ActionGraph()
    steps = [
        ClassificationStep(
            level=Level.MILD,
            threshold=0.8,
            intent="recon",
            event={"id": "e1", "run_id": "r1"},
        ),
        ClassificationStep(
            level=Level.SEVERE,
            threshold=0.92,
            intent="exfil",
            event={"id": "e2", "run_id": "r1"},
        ),
    ]
    graph.hydrate_run("r1", steps)
    assert graph.level("r1") == Level.SEVERE
    key_ids = [node.id for node in graph.key_nodes("r1")]
    assert key_ids == ["r1:1", "r1:2"]
    graph.hydrate_run("r1", steps)
    assert [node.id for node in graph.key_nodes("r1")] == key_ids
