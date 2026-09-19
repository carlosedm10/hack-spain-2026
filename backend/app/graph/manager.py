from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections import defaultdict
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


def _signature(
    event: dict[str, Any] | None, run_id: str, seq: int
) -> str:
    """Stable action signature used to collapse repeated/related actions into one graph node.

    We reuse a node only when the action is semantically specific: kind plus at
    least one of tool, target, or args. Generic events (only a kind/event name)
    fall back to a per-run sequence id so unrelated actions stay distinct and
    legacy/unit-test appends keep stable ids.
    """
    if isinstance(event, dict):
        kind = event.get("kind") or event.get("event")
        tool = event.get("tool")
        target = event.get("target") or event.get("path") or event.get("dst") or event.get("cmd")
        args = event.get("args")
        if kind and (tool or target or args):
            key = json.dumps(
                {"kind": kind, "tool": tool, "target": target, "args": args},
                sort_keys=True,
                default=str,
            )
            return hashlib.sha256(key.encode()).hexdigest()[:12]
    return f"evt-{run_id}:{seq}"

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
        self._event_index: dict[str, Node] = {}
        self._pending_causal: defaultdict[str, list[Node]] = defaultdict(list)
        self._signature_index: dict[str, Node] = {}
        self._run_tails: dict[str, Node] = {}
        self._run_visits: defaultdict[str, list[str]] = defaultdict(list)

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
                self._register_event_id(node)
                if connect is not None:
                    self._safe_connect(node, connect)
                else:
                    self._root_id = node_id
                self._link_causal(node)
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

    def _safe_connect(self, src: Node, dst: Node) -> None:
        """Idempotent connect used for causal/cross-run edges."""
        if self._nodes.get(src.id) is not src or self._nodes.get(dst.id) is not dst:
            return
        if src is dst or dst in src.neighbors:
            return
        with self._batch():
            src.neighbors.append(dst)
            dst.neighbors.append(src)
            self._mark_upsert(src)
            self._mark_upsert(dst)

    def _register_event_id(self, node: Node) -> None:
        event_id = node.event.get("id") if isinstance(node.event, dict) else None
        if not isinstance(event_id, str):
            return
        self._event_index[event_id] = node
        pending = self._pending_causal.pop(event_id, [])
        for pending_node in pending:
            self._safe_connect(pending_node, node)

    def _link_causal(self, node: Node) -> None:
        if not isinstance(node.event, dict):
            return
        refs = [
            *(node.event.get("caused_by") or []),
            *(node.event.get("derived_from") or []),
        ]
        seen: set[str] = set()
        for source_id in refs:
            if not isinstance(source_id, str) or source_id in seen:
                continue
            seen.add(source_id)
            source = self._event_index.get(source_id)
            if source is not None:
                self._safe_connect(node, source)
            else:
                self._pending_causal[source_id].append(node)

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
                return self.add_node(
                    f"run:{run_id}",
                    connect=root,
                    run_id=run_id,
                    run_ids={run_id},
                )

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
            run_node = self.ensure_run(run_id)
            seq = len(self._run_visits.get(run_id, [])) + 1
            sig = _signature(event, run_id, seq)
            node_id = f"{run_id}:{seq}" if sig.startswith("evt-") else f"act:{sig}"
            existing = self._signature_index.get(sig)
            tail = self._run_tails.get(run_id, run_node)
            level_value = Level(level)

            if existing is None:
                node = self.add_node(
                    node_id,
                    connect=tail,
                    threshold=threshold,
                    run_id=run_id,
                    run_ids={run_id},
                    visit_count=1,
                    signature=sig,
                    level=level_value,
                    intent=intent,
                    event=event,
                    action_id=action_id,
                    created_at=datetime.now(UTC),
                )
                self._signature_index[sig] = node
            else:
                node = existing
                node.run_ids.add(run_id)
                node.visit_count += 1
                node.event = event
                if action_id is not None:
                    node.action_id = action_id
                if level_value > node.level:
                    node.level = level_value
                    node.intent = intent
                    node.threshold = threshold
                if tail is not node:
                    self._safe_connect(tail, node)
                self._register_event_id(node)
                self._link_causal(node)
                self._mark_upsert(node)

            self._run_tails[run_id] = node
            self._run_visits[run_id].append(node.id)
            return node

    def last_action(self, run_id: str) -> Node | None:
        with self._lock:
            tail = self._run_tails.get(run_id)
            if tail is None or tail.id.startswith("run:"):
                return None
            return tail

    def run_nodes(self, run_id: str) -> list[Node]:
        with self._lock:
            run_node = self._nodes.get(f"run:{run_id}")
            if run_node is None:
                return []
            ordered: list[Node] = [run_node]
            seen: set[str] = {run_node.id}
            for node_id in self._run_visits.get(run_id, []):
                node = self._nodes.get(node_id)
                if node is None or node.id in seen:
                    continue
                seen.add(node.id)
                ordered.append(node)
            return ordered

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
        """Rebuild the in-memory action graph from Neo4j, reusing shared action nodes."""
        with self._lock:
            if run_id in self._run_tails or not steps:
                return
            with self._batch():
                self.ensure_run(run_id)
                for step in steps:
                    self.append(
                        run_id,
                        level=step.level,
                        threshold=step.threshold,
                        intent=step.intent,
                        event=step.event,
                        action_id=None,
                    )

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
                old_event_id = (
                    node.event.get("id") if isinstance(node.event, dict) else None
                )
                for name, value in fields.items():
                    setattr(node, name, value)
                new_event_id = (
                    node.event.get("id") if isinstance(node.event, dict) else None
                )
                if old_event_id != new_event_id:
                    if old_event_id is not None and self._event_index.get(old_event_id) is node:
                        del self._event_index[old_event_id]
                    if new_event_id is not None:
                        self._event_index[new_event_id] = node
                        pending = self._pending_causal.pop(new_event_id, [])
                        for pending_node in pending:
                            self._safe_connect(pending_node, node)
                self._link_causal(node)
                self._mark_upsert(node)
            return node

    def clear(self) -> None:
        with self._lock:
            removed = list(self._nodes)
            with self._batch():
                self._nodes = {}
                self._root_id = None
                self._event_index = {}
                self._pending_causal.clear()
                self._signature_index = {}
                self._run_tails = {}
                self._run_visits = defaultdict(list)
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
                run_id = row.get("run_id")
                run_ids = set(row.get("run_ids") or ([run_id] if run_id else []))
                visit_count = row.get("visit_count", 1)
                node = Node(
                    id=row["id"],
                    threshold=_validate_threshold(row["threshold"]),
                    run_id=run_id,
                    run_ids=run_ids,
                    visit_count=visit_count,
                    signature=row.get("signature"),
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
                self._event_index = {}
                self._pending_causal.clear()
                self._signature_index = {}
                self._run_tails = {}
                self._run_visits = defaultdict(list)
                for node in created.values():
                    self._register_event_id(node)
                for node in created.values():
                    self._link_causal(node)
                for node in created.values():
                    sig = node.signature
                    if sig is None:
                        if node.id.startswith("act:"):
                            sig = node.id[4:]
                        else:
                            sig = _signature(node.event, node.run_id or "", 0)
                    existing = self._signature_index.get(sig)
                    if existing is None or node.visit_count > existing.visit_count:
                        self._signature_index[sig] = node
                    if node.run_id is not None:
                        self._run_tails[node.run_id] = node
                    for rid in node.run_ids:
                        self._run_visits[rid].append(node.id)
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
        "run_ids": sorted(node.run_ids),
        "visit_count": node.visit_count,
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
    if node.run_ids:
        row["run_ids"] = sorted(node.run_ids)
    if node.visit_count != 1:
        row["visit_count"] = node.visit_count
    if node.signature is not None:
        row["signature"] = node.signature
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
