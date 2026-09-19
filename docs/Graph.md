# Graphs: persistent Neo4j and transitional ActionGraph

Neo4j is the persistent source for visualization and replay. It stores every normalized event, entities, causal relationships, assessments, realtime messages and L0–L5 decisions. JSONL is the append-only recovery tape. The Python `ActionGraph` remains a transitional in-process projection used for level and `key_nodes`; it is not the persistent graph database.

Neo4j labels and relationships:

- `Run`, `Event`, `Assessment`, `Entity`, `StreamMessage`
- `HAS_EVENT`, `NEXT`, `HAS_ASSESSMENT`, `TOUCHES`, `CAUSED_BY`, `DERIVED_FROM`
- entity projections such as `CALLS`, `READS`, `WRITES`, `SCHEDULES`, `SPEAKS_TO`

The stable frontend and SSE contract is [RealtimeGraphAPI.md](RealtimeGraphAPI.md).

Every classified action also materializes an `ActionGraph` node, benign included. The **key nodes** are the flagged subset (level ≥ 1), while Neo4j and JSONL keep the complete record.

On every new event, `jev` re-reads **short-term and long-term context in parallel** (it owns how to mix them):

* **Short-term** — the most recent nodes. A burst of dangerous nodes one after another, in a short window, is itself a risk signal.
* **Long-term** — the key-node history of the run. A later node that is only mildly bad still inherits extra weight if the run already had real problems, even if thirty harmless events sat in between.

How `jev` rewrites, promotes, or weights those nodes is `jev`'s job. We always hand it both contexts; we do not freeze old scores on our side. Request schema and how the two contexts map to `state`: [Jev.md](Jev.md).

* Dynamic: Starts empty and supports adding nodes at runtime.
* Single-rooted: exactly one root — the first node created; every run node hangs off it (`root → run:{run_id} → action nodes`), created lazily by `ensure_run(run_id)`. `root` is an entry point, not a parent.
* Shared actions: action nodes are keyed by a stable signature derived from `kind`, `tool`, `target`, and `args`. The same action in different runs maps to one node with `run_ids` and `visit_count`, and later steps can link back to earlier ones, forming cycles.
* Undirected: nodes keep `neighbors` — a **mutual** adjacency list: if `a` lists `b`, `b` lists `a`. There is no `parent`: a node can have any number of neighbors, and **cycles are allowed** (self-loops are rejected). `connect(src, dst)` adds a mutual edge between two existing nodes (duplicates and self-loops rejected). Traversals (`reachable()`) are cycle-safe (visited set), so a loop can never hang `save`/`load`.
* Runs are isolated by `run_id`, not by the graph: with undirected edges, traversal alone cannot tell runs apart, so a run's nodes are exactly the ones stamped with its `run_id` (in insertion order). Even explicit cross-run edges cannot leak one run's nodes into another's level or key-node history. Sentinel inspect history is **link-scoped** (current run plus events that share target, `derived_from`/`caused_by`, agent+target, memory/entity, or tool+target). When Neo4j is enabled, linked runs are **rehydrated from the store** into the in-process monitor cache before `inspect`; without Neo4j, cross-run links exist only for runs still present in the same process.
* Complete: one node per classified action — a `level_0_benign` verdict still materializes a `Level.NONE` node. The flagged subset is `key_nodes()` (level ≥ 1). Degraded Jev stays tape-only **unless** Sentinel/gate already raised the run level; that elevation is materialized so the run cannot drop on the next event.
* Node: Each node has a threshold and an optional associated tool (default: `None`). A materialized node also records `{run_id, level, intent, event, action_id, created_at}` — the evidence needed to replay a run and drive the playbooks ([Actions.md](Actions.md)).
* Singleton: A single shared instance manages the entire graph.
* Persistence: Supports efficient `save()` and `load()` operations. The snapshot stores the explicit `root` id plus, per node, its `neighbors` by id — so loops survive the roundtrip. All fields except the live `tool` object survive.
* Safety: Thread-safe operations to prevent concurrent access issues.
* Constraint: threshold must be a float between 0.0 and 1.0 (inclusive). It is `jev`'s `confidence` for the chain ending at this node ([Jev.md](Jev.md)); discrete levels 1–5 are derived from it ([Actions.md](Actions.md)).

Implemented in `backend/app/graph/`: `Node` lives in `models.py`, the `ActionGraph` manager and module-level `graph` singleton in `manager.py`, colocated tests under `tests/`.

### Run state is derived, not stored in ActionGraph alone

Neo4j persists `:Run` nodes (escalate-only `level`, timestamps) plus every `:Event`, `:Assessment`, and entity edge. The in-memory **ActionGraph** is a write-through cache: `prior_level`, `key_nodes`, and `/api/graph/stream` read it after **hydration from Neo4j** on ingest (see `backend/app/runs/service.py`). On application startup, every persisted run is restored into the ActionGraph so the global stream is never empty when history exists. If the process restarts, classification, Sentinel inspect, and the dashboard all rehydrate from the store; RAM is not a second source of truth when Neo4j is enabled.

Everything the pipeline still derives locally from ActionGraph nodes stamped with `run_id`:

- `graph.level(run_id)` — the run's effective level: `max` over the run's node levels (default `Level.NONE`). Escalate-only and L1-stickiness fall out of `max()` over an append-only structure — a run never downgrades, and `prior_level` for the next jev call is just this value.
- `graph.key_nodes(run_id)` — the run's *flagged* nodes (level ≥ 1) in insertion order; this is the `long_term` array handed to `jev` ([Jev.md](Jev.md)). All nodes, benign included, are `run_nodes(run_id)`.
- `graph.actionable_level(run_id)` — the max level among nodes whose `threshold` clears the action gate. A low-confidence L3 is recorded (the run level still reads L3) but does not fire a playbook until a confident verdict confirms it.

`append(run_id, …)` chains a new action under the run's last node. New actions reuse an existing node when their signature matches, increasing `visit_count` and adding the run to `run_ids`; `connect(src, dst)` adds extra edges between existing nodes (this is how cycles form); `update(node_id, **fields)` is how the dispatcher later stamps `action_id` on a node that fired a playbook; `clear()` resets the instance in place (a human clearing them from the viewer, [Actions.md](Actions.md)).

### Live stream

`GET /api/graph/stream` is a Server-Sent Events feed of the whole graph — every run, not one. Clients keep the graph in memory and never poll.

- **First message — `snapshot`**: `{revision, root, nodes}` with every node and its `neighbors` by id. Every serializable field travels; the live `tool` object never does.
- **Every later message — `update`**: `{revision, root, upsert_nodes, removed_node_ids}`. An upserted node carries its *complete* neighbor list, so applying it replaces the client's version of that node wholesale; edges are therefore never patched, only re-sent from both endpoints.
- **`revision`** increments by one per published change and is the client's gap detector: an update whose revision is not `previous + 1` means the client missed something and must reconnect.
- **One update per composite operation**: `append()` may create the root, the run node and the key node — subscribers see a single update, never a partial graph. Failed mutations publish nothing.
- **Subscribe is atomic**: the listener is registered and the snapshot captured under one lock acquisition, so no change can slip between "read the snapshot" and "start listening".
- **Slow clients are dropped**: each subscriber owns a bounded queue (64 updates); overflow ends that stream so the client reconnects and resynchronizes from a fresh snapshot. There is no replay buffer — reconnecting always starts from a new snapshot.

The hub is `backend/app/graph/stream.py` (one queue per client, subscribe/unsubscribe), the endpoint `backend/app/graph/router.py`. `useGraphStream()` in `frontend/src/graph/` mirrors the stream in memory — one `Graph`, upserts keyed by node id so existing nodes update in place. Both views render that mirror: `frontend/src/live/` is the projector at `/live`, and the analyst dashboard at `/` mounts the same hook (selection reveals the verdict and associated protective trace). Both derive each run's path `run:{run_id} → {run_id}:1 → {run_id}:2 …` via `chainEdges()` and colour it by the run's escalate-only level; mutual adjacency is deduplicated into undirected edges, with stable per-run columns and zoom/fit controls. `?demo` on either route swaps the stream for the mock timeline in `src/dashboard/demo.ts`, which is also the only source of provisional “Awaiting Jev” overlays — `layoutGraph` drops an overlay the moment a real node with that id arrives. The dashboard's side panels use real feeds via `src/dashboard/feeds.ts`: classified nodes become log lines, and the protective-actions panel polls `GET /api/demo/incidents/latest` for journaled `ActionTransition`s — the `page_oncall` transitions (`mode: "real"`) are the HappyRobot pager calls. Raw container stdout still has no endpoint — `runs/log.py` tails per-run JSONL internally but does not expose it.

### Example

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from app.classification.models import Level


@dataclass
class Node:
    id: str
    neighbors: list["Node"] = field(default_factory=list)  # mutual (undirected)
    threshold: float = 0.0  # Range: [0.0, 1.0]
    tool: Optional[Any] = None
    run_id: Optional[str] = None
    level: Level = Level.NONE
    intent: Optional[str] = None
    event: Optional[dict[str, Any]] = None
    action_id: Optional[str] = None
    created_at: Optional[datetime] = None
```
