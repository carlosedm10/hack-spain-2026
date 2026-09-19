from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.classification.models import Level
from app.config import settings
from app.graph.models import Node
from app.graph.neo4j import ClassificationStep

_UPDATABLE_FIELDS = frozenset({"level", "threshold", "intent", "event", "action_id"})


@dataclass(frozen=True)
class GraphUpdate:
    revision: int
    root: str | None
    upsert_nodes: list[dict[str, Any]]
    removed_node_ids: list[str]


GraphListener = Callable[[GraphUpdate], None]


class ActionGraph:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._nodes: dict[str, Node] = {}
        self._root_id: str | None = None
        self._revision = 0
        self._listeners: list[GraphListener] = []
        self._batch_depth = 0
        self._dirty_upserts: dict[str, None] = {}
        self._dirty_removed: set[str] = set()

    def subscribe(self, listener: GraphListener) -> dict[str, Any]:
        with self._lock:
            self._listeners.append(listener)
            return self._snapshot()

    def unsubscribe(self, listener: GraphListener) -> None:
        with self._lock:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def _snapshot(self) -> dict[str, Any]:
        return {
            "revision": self._revision,
            "root": self._root_id,
            "nodes": [_node_payload(node) for node in self._nodes.values()],
        }

    @contextmanager
    def _batch(self) -> Iterator[None]:
        self._batch_depth += 1
        try:
            yield
        finally:
            self._batch_depth -= 1
        if self._batch_depth == 0:
            self._emit()

    def _emit(self) -> None:
        if not self._dirty_upserts and not self._dirty_removed:
            return
        self._revision += 1
        update = GraphUpdate(
            revision=self._revision,
            root=self._root_id,
            upsert_nodes=[
                _node_payload(self._nodes[nid]) for nid in self._dirty_upserts if nid in self._nodes
            ],
            removed_node_ids=sorted(self._dirty_removed),
        )
        self._dirty_upserts = {}
        self._dirty_removed = set()
        for listener in list(self._listeners):
            with suppress(Exception):
                listener(update)

    def _mark_upsert(self, node: Node) -> None:
        self._dirty_upserts[node.id] = None
        self._dirty_removed.discard(node.id)

    def _mark_removed(self, node_id: str) -> None:
        if node_id not in self._dirty_upserts:
            self._dirty_removed.add(node_id)

    @property
    def root(self) -> Node | None:
        with self._lock:
            return self._nodes.get(self._root_id) if self._root_id else None

    @property
    def nodes(self) -> list[Node]:
        with self._lock:
            return list(self._nodes.values())

    def get_node(self, node_id: str) -> Node | None:
        with self._lock:
            return self._nodes.get(node_id)

    def add_node(
        self,
        node_id: str,
        connect: Node | None = None,
        threshold: float = 0.0,
        tool: Any | None = None,
        **fields: Any,
    ) -> Node:
        if not isinstance(node_id, str) or not node_id:
            raise ValueError(f"node id must be a non-empty string, got {node_id!r}")
        value = _validate_threshold(threshold)

        with self._lock:
            if node_id in self._nodes:
                raise ValueError(f"node id already exists: {node_id!r}")
            if self._root_id is None:
                if connect is not None:
                    raise ValueError(
                        "graph is empty: the first node must be the root (connect=None)"
                    )
            elif connect is None:
                raise ValueError(f"root already exists ({self._root_id!r}): connect is required")
            elif self._nodes.get(connect.id) is not connect:
                raise ValueError(f"connect target {connect.id!r} is not in the graph")

            node = Node(id=node_id, threshold=value, tool=tool, **fields)
            with self._batch():
                self._nodes[node_id] = node
                if connect is not None:
                    connect.neighbors.append(node)
                    node.neighbors.append(connect)
                    self._mark_upsert(connect)
                else:
                    self._root_id = node_id
                self._mark_upsert(node)
            return node

    def connect(self, src: Node, dst: Node) -> None:
        with self._lock:
            if self._nodes.get(src.id) is not src:
                raise ValueError(f"source node {src.id!r} is not in the graph")
            if self._nodes.get(dst.id) is not dst:
                raise ValueError(f"destination node {dst.id!r} is not in the graph")
            if src is dst:
                raise ValueError("self-loops are not allowed")
            if dst in src.neighbors:
                raise ValueError(f"edge {src.id!r} -- {dst.id!r} already exists")
            with self._batch():
                src.neighbors.append(dst)
                dst.neighbors.append(src)
                self._mark_upsert(src)
                self._mark_upsert(dst)

    def ensure_run(self, run_id: str) -> Node:
        if not isinstance(run_id, str) or not run_id:
            raise ValueError(f"run id must be a non-empty string, got {run_id!r}")
        with self._lock:
            run_node = self._nodes.get(f"run:{run_id}")
            if run_node is not None:
                return run_node
            with self._batch():
                if self._root_id is None:
                    self.add_node("root")
                root = self._nodes[self._root_id]
                return self.add_node(f"run:{run_id}", connect=root, run_id=run_id)

    def append(
        self,
        run_id: str,
        *,
        level: Level | int,
        threshold: float,
        intent: str | None = None,
        event: dict[str, Any] | None = None,
        action_id: str | None = None,
    ) -> Node:
        with self._lock, self._batch():
            # Composite op: ensure_run may create the root and the run node
            # before the new key node — all of it lands in one GraphUpdate.
            run_node = self.ensure_run(run_id)
            # Run membership is the run_id stamp, not graph traversal: an undirected
            # graph cannot keep runs isolated by direction alone.
            chained = [n for n in self._nodes.values() if n.run_id == run_id and n is not run_node]
            last = chained[-1] if chained else run_node
            seq = len(chained) + 1
            return self.add_node(
                f"{run_id}:{seq}",
                connect=last,
                threshold=threshold,
                run_id=run_id,
                level=Level(level),
                intent=intent,
                event=event,
                action_id=action_id,
                created_at=datetime.now(UTC),
            )

    def run_nodes(self, run_id: str) -> list[Node]:
        with self._lock:
            run_node = self._nodes.get(f"run:{run_id}")
            if run_node is None:
                return []
            return [
                run_node,
                *(n for n in self._nodes.values() if n.run_id == run_id and n is not run_node),
            ]

    def key_nodes(self, run_id: str) -> list[Node]:
        return [n for n in self.run_nodes(run_id) if n.level >= Level.MILD]

    def level(self, run_id: str) -> Level:
        return max((n.level for n in self.run_nodes(run_id)), default=Level.NONE)

    def actionable_level(self, run_id: str) -> Level:
        return max(
            (n.level for n in self.run_nodes(run_id) if n.threshold >= settings.action_gate),
            default=Level.NONE,
        )

    def hydrate_run(self, run_id: str, steps: list[ClassificationStep]) -> None:
        """Rebuild the in-memory chain from Neo4j when this run is not cached yet."""
        with self._lock:
            chained = [
                node
                for node in self._nodes.values()
                if node.run_id == run_id and node.id != f"run:{run_id}"
            ]
            if chained or not steps:
                return
            with self._batch():
                run_node = self.ensure_run(run_id)
                last = run_node
                for index, step in enumerate(steps, start=1):
                    node_id = f"{run_id}:{index}"
                    node = self.add_node(
                        node_id,
                        connect=last,
                        threshold=step.threshold,
                        run_id=run_id,
                        level=step.level,
                        intent=step.intent,
                        event=step.event,
                        action_id=None,
                        created_at=datetime.now(UTC),
                    )
                    last = node

    def update(self, node_id: str, **fields: Any) -> Node:
        unknown = set(fields) - _UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"unknown node fields: {sorted(unknown)!r}")
        if "threshold" in fields:
            fields["threshold"] = _validate_threshold(fields["threshold"])
        if "level" in fields:
            fields["level"] = Level(fields["level"])

        with self._lock:
            node = self._nodes.get(node_id)
            if node is None:
                raise ValueError(f"node id does not exist: {node_id!r}")
            with self._batch():
                for name, value in fields.items():
                    setattr(node, name, value)
                self._mark_upsert(node)
            return node

    def clear(self) -> None:
        with self._lock:
            removed = list(self._nodes)
            with self._batch():
                self._nodes = {}
                self._root_id = None
                for node_id in removed:
                    self._mark_removed(node_id)

    def save(self, path: str | Path) -> None:
        with self._lock:
            payload = {
                "root": self._root_id,
                "nodes": [_node_row(node) for node in self._nodes.values()],
            }

        target = Path(path)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, target)
        except BaseException:
            os.unlink(tmp)
            raise

    def load(self, path: str | Path) -> None:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        root_id = data.get("root")
        rows = data["nodes"]

        with self._lock:
            created: dict[str, Node] = {}
            for row in rows:
                node = Node(
                    id=row["id"],
                    threshold=_validate_threshold(row["threshold"]),
                    run_id=row.get("run_id"),
                    level=Level(row.get("level", 0)),
                    intent=row.get("intent"),
                    event=row.get("event"),
                    action_id=row.get("action_id"),
                    created_at=(
                        datetime.fromisoformat(row["created_at"]) if "created_at" in row else None
                    ),
                )
                created[node.id] = node

            for row in rows:
                node = created[row["id"]]
                seen: set[str] = set()
                for neighbor_id in row.get("neighbors", []):
                    if neighbor_id == node.id:
                        raise ValueError(f"snapshot contains a self-loop on {node.id!r}")
                    if neighbor_id in seen:
                        raise ValueError(
                            f"snapshot contains duplicate edge {node.id!r} -- {neighbor_id!r}"
                        )
                    seen.add(neighbor_id)
                    neighbor = created.get(neighbor_id)
                    if neighbor is None:
                        raise ValueError(f"snapshot references unknown neighbor {neighbor_id!r}")
                    if neighbor not in node.neighbors:
                        node.neighbors.append(neighbor)
                        neighbor.neighbors.append(node)

            if rows:
                if root_id is None:
                    raise ValueError("snapshot contains nodes but no root")
                if root_id not in created:
                    raise ValueError(f"snapshot root {root_id!r} is not among the nodes")
            else:
                root_id = None

            with self._batch():
                for node_id in self._nodes:
                    self._mark_removed(node_id)
                self._nodes = created
                self._root_id = root_id
                for node in created.values():
                    self._mark_upsert(node)

    def reachable(self, node: Node) -> list[Node]:
        ordered: list[Node] = []
        visited: set[str] = {node.id}

        def visit(current: Node) -> None:
            for neighbor in current.neighbors:
                if neighbor.id in visited:
                    continue
                visited.add(neighbor.id)
                ordered.append(neighbor)
                visit(neighbor)

        visit(node)
        return ordered


def _node_payload(node: Node) -> dict[str, Any]:
    return {
        "id": node.id,
        "neighbors": [n.id for n in node.neighbors],
        "threshold": node.threshold,
        "run_id": node.run_id,
        "level": int(node.level),
        "intent": node.intent,
        "event": node.event,
        "action_id": node.action_id,
        "created_at": node.created_at.isoformat() if node.created_at else None,
    }


def _node_row(node: Node) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": node.id,
        "neighbors": [n.id for n in node.neighbors],
        "threshold": node.threshold,
    }
    if node.run_id is not None:
        row["run_id"] = node.run_id
    if node.level != Level.NONE:
        row["level"] = int(node.level)
    if node.intent is not None:
        row["intent"] = node.intent
    if node.event is not None:
        row["event"] = node.event
    if node.action_id is not None:
        row["action_id"] = node.action_id
    if node.created_at is not None:
        row["created_at"] = node.created_at.isoformat()
    return row


def _validate_threshold(threshold: float) -> float:
    if isinstance(threshold, bool) or not isinstance(threshold, int | float):
        raise TypeError(f"threshold must be a float in [0.0, 1.0], got {threshold!r}")
    value = float(threshold)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"threshold must be in [0.0, 1.0], got {value}")
    return value


graph = ActionGraph()
