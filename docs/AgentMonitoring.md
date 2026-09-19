## Agent Monitoring

Each agent runs inside an isolated environment (sandbox — one Docker container per run). We need full visibility into what the agent does in there: which actions it takes, which tools it executes, its reasoning, and the trace that connects them. Logs are captured **outside** the container (stdout, filesystem diff, network egress), so the agent cannot tamper with or hide its own trail.

### What we capture

| Signal | Source | How |
| --- | --- | --- |
| Agent transcript | The agent harness itself | `stream-json` / JSONL output — every message, tool call, tool result, and reasoning step |
| Actions / commands | Every process the agent spawns | stdout/stderr captured by the container runtime |
| File changes | Workspace state | `git diff` (or `docker diff`) on the workspace before/after the run |
| Network | Outbound requests | Egress routed through a proxy on the container's bridge network |
| Resources | CPU / memory / processes | `docker stats` / `docker events` |

### Requirements

- **Log everything** as structured events (JSONL), timestamped and ordered — one event per action/tool call.
- **Per-run isolation**: each run gets its own log directory and a clean container (`--rm`), no state leaking between runs.
- **Tamper-proof**: capture happens on the host side; nothing inside the sandbox can modify the record.
- **Queryable**: logs feed a trace viewer / timeline so we can replay a run end to end.

### Preflight, classification and persistence

Every harness tool first sends a `requested` event to `POST /api/runs/{run_id}/preflight`. Policy checks, SafetyDrift, Sentinel and Jev inspect it before the side effect; the deterministic gate returns `allow`, `hold` or `refuse`. An allowed call later emits exactly one terminal transition (`completed` or `failed`); a denied call emits `refused`, correlated through `metadata.operation_id` and `caused_by`.

JSONL stores every normalized and redacted transition. Neo4j stores every L0–L5 event, entity, causal edge, assessment, Markov state, gate decision and realtime `StreamMessage`; level ≥ 1 remains only the compact `key_nodes` projection handed to Jev as long context. See [RealtimeGraphAPI.md](RealtimeGraphAPI.md), [Actions.md](Actions.md) and [Graph.md](Graph.md).

The current harness records tool lifecycle and final assistant output. Hidden model chain-of-thought is neither required nor treated as trustworthy evidence.
