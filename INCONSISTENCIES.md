# Known inconsistencies

Tracked flaws that are knowingly left in place. Resolve an entry by fixing it, then delete it from this list (see [AGENTS.md](AGENTS.md)).

## Referenced but never written

- [docs/scenarios.md](docs/scenarios.md) references optional richer-demo services that do not exist: `victim-agent`, `customers-db`, and `docker-lure`. Detection coverage for those mechanisms lives in the eval corpus instead.

## Not built

- The in-process `app.dispatch` dispatcher records idempotent L1–L5 kinds and executes demo-world counters for the lab. Host-side playbooks and HappyRobot paging live in `app.actions.ActionService` after the deterministic gate; automatic Docker containment from the backend container remains separate because the API does not mount the Docker socket.
- [docs/Actions.md](docs/Actions.md) references containment scripts (`scripts/contain.sh`, `scripts/cut-egress.sh`, `scripts/kill-swarm.sh`) that exist in-repo but are not invoked automatically from the backend container.
