# HackSpain 2026 — The One Doc

This repo is the local **hackspain** product stack for the 2026 hackathon: FastAPI, React/Vite, Neo4j for the monitoring graph, and Postgres for future business state. The participant **hackspain** CLI is a separate binary; its command surface lives in [docs/cli.md](cli.md) and must stay aligned with [hackspain.app/cli](https://hackspain.app/cli).

## The layers

```
browser :3000  →  frontend-hackspain (Vite :5173)  →  backend-hackspain :8000
                                                      →  neo4j-hackspain :7687
                                                      →  postgres-hackspain :5432
```

- **frontend/** — React/Vite UI, host port 3000. Compose maps `3000:5173` and expects `frontend/package.json`. Vite proxies `/api` to `backend-hackspain:8000`, so the browser talks to one origin. The UI renders nothing today: it mirrors the live graph in memory ([docs/Graph.md](Graph.md)).
- **backend/** — FastAPI app (`app.main:app`), uv, SQLAlchemy, Alembic. **CORS allows only `http://localhost:3000`.**
- **postgres-hackspain** — Postgres 18. Backend waits on a healthy `pg_isready`. Named volume `postgres_data_hackspain`.
- **neo4j-hackspain** — Neo4j Community. Persistent event/entity/assessment graph plus resumable realtime messages.

Compose network is `appnet_hackspain`. Services are named `backend-hackspain`, `frontend-hackspain`, `postgres-hackspain`.

## The taxonomy

These names repeat in compose, Makefile targets, and env vars.

| Name | Covers | Where it lives |
|---|---|---|
| **Runs** | HTTP ingest + JSONL tape: `POST /api/runs/{run_id}/events`, run lookup, run list | `backend/app/runs/` |
| **Realtime monitor API** | Contrato frontend: snapshot, SSE, lifecycle y grafo Neo4j | [docs/RealtimeGraphAPI.md](RealtimeGraphAPI.md) |
| **Classification** | `jev` client, watcher client, two-tier pipeline (τ trigger, gate, degraded path) | `backend/app/classification/` |
| **Monitoring Graph** | Complete Neo4j event/entity/assessment graph; sparse `key_nodes` only for Jev context; transitional ActionGraph SSE | `backend/app/graph/` · `frontend/src/graph/` · [docs/Graph.md](Graph.md) |
| **health** | Liveness JSON `{status: ok}` | `GET /health` on the API |
| **hackspain CLI** | Participant terminal client (not this repo's code) | [docs/cli.md](cli.md) |
| **Agent monitoring** | Host-side capture of a sandboxed agent run | [docs/AgentMonitoring.md](AgentMonitoring.md) |
| **Actions** | `jev` intent → levels 1–5 → deterministic playbooks | [docs/Actions.md](Actions.md) |
| **Demo scenarios** | Malicious-agent harness + the L1–L5 demo arcs | [docs/scenarios.md](scenarios.md) |
| **jev** | Classifier: chain intent → level 0–5 + confidence + intent choice | Called from `backend/app/classification/jev.py`, over HTTP from the monitoring host |

## How it's built

One path for local work:

```
make build/up → compose → uvicorn (reload) + bun dev + postgres
              → Alembic uses DATABASE_URL on the compose hostname
```

GitHub Actions copies `.env_template` to `.env`, then `make build`, `make lint`, and `make test`. Live Jev (`make monitor-eval`) stays a local command: GitHub runners cannot reach TypeSafe reliably, so it is not a required check.

### The principles that matter

- **Make is the public interface** — CI and humans run the same verbs (`lint`, `test`, `migrate`). Do not duplicate tool commands in workflow YAML.
- **Secrets stay in gitignored `.env`** — `.envrc` only loads; new keys are documented in `.env_template`.
- **Backend venv lives outside the bind mount** — `UV_PROJECT_ENVIRONMENT=/opt/venv` so `./backend:/app` does not wipe dependencies on the host.
- **CLI commands are not invented here** — the binary is unpublished in this tree; [docs/cli.md](cli.md) tracks the official surface.

## How data flows

Settings (`DATABASE_URL`, `SECRET_KEY`, `DEBUG`) come from the process environment. Compose injects `DATABASE_URL` with host `postgres-hackspain` (not `localhost`). Pydantic settings also accept a `.env` next to the process cwd (`/app` in the container), and ignore extra keys such as `POSTGRES_*`.

- **Reads**: `GET /health` hits no database. Run `snapshot`, `stream`, `timeline` and `graph` read the persistent Neo4j monitor; `/api/graph/stream` remains the transitional whole-ActionGraph SSE used by the headless mirror already on `main`.
- **Writes**: `POST /api/runs/{run_id}/events` normalizes, redacta y añade el evento al tape JSONL; ejecuta clasificación, drift, Sentinel, gate y dispatch; y persiste grafo y `StreamMessage` en Neo4j. Postgres no almacena ninguna parte del grafo. La demo de contramedidas usa un world state mínimo en memoria.
- **Sync / background**: Neo4j `StreamMessage` ofrece replay SSE por run; el ActionGraph transitorio también fan-out mutations a sus clientes conectados.
- **Agent run (product path)**: tool preflight → tape completo → SafetyDrift/Sentinel → Jev → gate determinista → ejecución o rechazo → Neo4j persiste evento, entidades, assessment y stream SSE → dispatcher registra playbooks y ejecuta counters del world demo.

### Entities

- **Run**: identified by `run_id` in the URL — no SQL table. Neo4j persists its level and event membership; the complete event tape is JSONL, while the legacy API still derives key nodes from the in-memory ActionGraph.
- **Event / Assessment / Entity**: Neo4j persists the directed monitoring graph; its concrete labels, relationships and frontend payloads live in [RealtimeGraphAPI.md](RealtimeGraphAPI.md).
- **Node / Verdict**: the transitional ActionGraph keeps `{level, score, intent, action_id}` per [docs/Graph.md](Graph.md); `jev`'s answer is a `Verdict` dataclass in `backend/app/classification/models.py`.

### One example, end to end

1. Copy `.env_template` → `.env` and `direnv allow` (or export the same keys).
2. `make build` starts Postgres and Neo4j, then uvicorn on `:8000` and the frontend container on `:3000`.
3. Browser or `curl` `GET http://localhost:8000/health` → `{"status":"ok"}`.
4. `curl -X POST localhost:8000/api/runs/demo/events -H 'content-type: application/json' -d '{"event":"file_read","path":"/app/.env"}'` → verdict JSON. With no `TYPESAFE_API_KEY` this returns a clean degraded verdict (level unchanged, `degraded: true`), and the event still lands on the tape — not a 500.
5. OpenAPI UI is at `http://localhost:8000/docs`.

## Key decisions and caveats (why it is this way)

- **Compose hostname in `DATABASE_URL`**: the API talks to `postgres-hackspain`, not `localhost`. Host-side tools that are not on `appnet_hackspain` must use `localhost:5432` instead.
- **CORS pinned to the Vite origin**: `http://localhost:3000` only — other origins are rejected on purpose until a real frontend origin exists.
- **Tests live next to the code they cover**: feature tests under `app/<feature>/tests/`; health lives in `app/tests/`. Not a top-level `backend/tests/`.
- **`make lint` / `make test` run inside already-up containers**: the stack must be up first. `make build` builds AND starts it (`docker compose up --build -d`), so CI only needs `make build` — no separate `make up` step.
- **Postgres data volume mounts at `/var/lib/postgresql`, not `/var/lib/postgresql/data`**: postgres 18+ images store data in `/var/lib/postgresql/<major>/docker` and the entrypoint hard-fails on any mount at the old `/data` path — even an empty volume. Bumping the major version still needs a dump-and-restore or volume reset (`docker compose down -v`).
- **Frontend image must not copy host `node_modules`**: root `.dockerignore` excludes `frontend/node_modules` so `COPY frontend/ .` cannot overwrite the Linux install with Darwin Rollup binaries. Recreate the anonymous `/code/node_modules` volume after a bad copy (`docker compose up --renew-anon-volumes`).
- **`PYTHONPATH=/app` on the backend**: Alembic is invoked as a venv entrypoint (`uv run alembic`), which does not put the bind-mounted `app/` package on `sys.path`. Uvicorn's `app.main:app` import still works because `uv run` adds the project cwd.
- **Compose build cache is env-injected**: `cache_from`/`cache_to` interpolate `CACHE_FROM`/`CACHE_TO`; CI sets them to the GitHub Actions cache (`type=gha`), local builds default to throwaway `/tmp` dirs. Only one CI workflow exists (`ci.yml`) — it covers push and PRs to `main`, with in-progress runs cancelled on new commits.
- **Compose injects `TYPESAFE_API_KEY` only via `env_file: .env`**: do not also set `environment: TYPESAFE_API_KEY=${TYPESAFE_API_KEY:-}` — an empty interpolation overrides the file and the process looks keyed or blank depending on Settings. Pydantic settings ignore empty env values and strip wrapping quotes. The secret stays gitignored, documented in `.env_template`.
- **`jev` contributes the level; deterministic code owns the decision**: criticality is intent of the *chain*, not of one event. Drift, Sentinel and gate can raise the incident level. `app.dispatch` records lab kinds and demo-world counters; **`ActionService` runs host playbooks and HappyRobot paging after the gate**, using `gate.incident_level`, not raw Jev alone.
- **Sentinel inspect history is link-scoped, rehydrated from Neo4j when enabled**: the current run's recent events always participate; other runs participate when they share a real link (target, `derived_from`/`caused_by`, agent+target, memory/entity keys, tool+target). On ingest, missing in-process history and linked runs are restored from Neo4j before `inspect`; the ActionGraph cache is rebuilt for `prior_level` / `key_nodes`. With Neo4j off, cross-run inspect is process-local only. JSONL remains the append-only tape.
- **Lab suite default is full ingest; smoke is opt-in:** `POST /api/evals/benchmarks` walks Sentinel traces through `service.ingest` (JSONL, monitor.prepare, Jev-or-degrade, gate, dispatch, Neo4j persist, SSE). `?smoke=true` is the in-process `_walk_trace` and must not be pitched as Jev science. **LIVE** is only set when a classify returns `latency_ms`; a present `TYPESAFE_API_KEY` plus HTTP 401 is degraded. `make seed-lab LIVE_JEV=1` is the Make variable (`--live-jev` is not a make option).
- **One node per action; key nodes are level ≥ 1**: every classified event becomes a node, so the graph is the run's complete action sequence. `key_nodes()` (level ≥ 1) is the flagged subset — that's the `long_term` history. On every event, `jev` gets the recent burst (tape) and the key-node history in parallel — a streak of bad nodes is dangerous; a mild node after earlier problems still counts for more. How those are mixed is `jev`'s job.
- **Classification state and persistent state**: Neo4j `:Run.level` is escalate-only; the ActionGraph cache derives `max` level over hydrated nodes for the same `run_id`. There is no SQL run table yet.
- **Two graph representations during migration**: the ActionGraph is an undirected in-process cache isolated by `run_id`; the persistent Neo4j monitoring graph is directed (`HAS_EVENT`, `NEXT`, causal and entity edges) and is the source for replay, inspect rehydration, and visualization.
- **The tape and the graphs are different things**: JSONL keeps each normalized event for short-term context, replay and recovery; the ActionGraph keeps classified chain nodes for Jev; Neo4j keeps the persistent directed event/entity/assessment graph. Their exact frontend boundary is [RealtimeGraphAPI.md](RealtimeGraphAPI.md).
- **Dashboard still uses the transitional ActionGraph stream**: React `/`, `/live`, and `/ladder` read `GET /api/graph/stream` and `/api/demo/incidents/latest` while Neo4j + per-run SSE remain the long-term contract ([RealtimeGraphAPI.md](RealtimeGraphAPI.md)). On startup the ActionGraph is rehydrated from Neo4j so the global stream immediately reflects persisted runs; new ingests update it in place and reuse action-signature nodes across runs.
- **Frontend tests are DOM tests, not typechecks**: `bun test` mounts dashboard hooks in happy-dom against a fake `EventSource`. `bun run lint` (`tsc --noEmit`) still owns typechecking; `make test` no longer just re-runs it.
- **Tests are offline**: every unit test drives clients through `httpx.MockTransport`, so `make test` passes with no API keys. `make monitor-eval` is the local live Jev runner; CI does not call it.
- **Levels only escalate; L1 is sticky**: a run never auto-downgrades. After L1, later `jev` calls get `prior_level=1`. L1–L3 are one agent; L4–L5 are the environment. Agent sandboxes must sit on a separate network (`agentnet`) from the product compose stack so L4 does not black-hole the viewer.
- **L4/L5 paging is parallel notification, not authorization**: `ActionService` places the HappyRobot call beside containment steps; `app.dispatch` may still record a `page_oncall` kind for the lab. A missing pager integration must never block infrastructure containment.

## Where the details live

- [docs/cli.md](cli.md) — participant CLI commands, flags, exit codes.
- [docs/Graph.md](Graph.md) — persistent Neo4j graph plus the transitional ActionGraph/SSE projection. Code: `backend/app/graph/`, existing headless mirror: `frontend/src/graph/`.
- [docs/RealtimeGraphAPI.md](RealtimeGraphAPI.md) — contrato implementable en español para snapshot + SSE, payloads, lifecycle, grafo Neo4j y world state.
- [docs/AgentMonitoring.md](AgentMonitoring.md) — sandbox capture signals.
- [docs/Actions.md](Actions.md) — levels 1–5, playbooks, pager, build order.
- [docs/HappyRobotEvals.md](HappyRobotEvals.md) — HappyRobot + Sentinel eval corpora, lab pages, monitor coverage gaps.
- [docs/BenchmarkReport.md](BenchmarkReport.md) — dated scientific eval report (method, n, thresholds, live Jev vs degraded).
- [docs/scenarios.md](scenarios.md) — malicious-agent harness, toolset, and the demo scenarios (L1–L5).
- The code — `backend/app/` (API), `compose.yaml` + `docker/` (runtime), root `Makefile` (verbs).
