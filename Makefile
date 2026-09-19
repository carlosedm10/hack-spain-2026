# ------------------------------ Docker Compose ------------------------------ #
.PHONY: build up down restart

build:
	@echo ":: build: ."
	docker compose up --build -d --force-recreate
	@echo "Backend: http://localhost:8000/"
	@echo "OpenAPI: http://localhost:8000/docs"
	@echo "Frontend: http://localhost:3000/"

up:
	@echo ":: up: ."
	docker compose up -d --force-recreate
	@echo "Backend: http://localhost:8000/"
	@echo "OpenAPI: http://localhost:8000/docs"
	@echo "Frontend: http://localhost:3000/"

down:
	@echo ":: down: ."
	docker compose down --remove-orphans

restart:
	@echo ":: restart: ."
	docker compose restart

# ----------------------------- Backend Package Management ----------------------------- #
.PHONY: uv-lock uv-add uv-update uv-remove uv-lock-regenerate

# Usage:
#   make uv-add PKG="package[extras]==version"
#   make uv-update
#   make uv-update PKG=foo
#   make uv-remove PKG=foo
uv-lock:
	docker compose run --rm backend-hackspain uv lock

uv-add:
	docker compose run --rm backend-hackspain uv add $(PKG)

uv-update:
ifeq ($(PKG),)
	docker compose run --rm backend-hackspain uv lock --upgrade
else
	docker compose run --rm backend-hackspain uv lock --upgrade-package $(PKG)
endif

uv-remove:
	docker compose run --rm backend-hackspain uv remove $(PKG)

uv-lock-regenerate:
	docker compose run --rm backend-hackspain uv lock --refresh

# ----------------------------- Frontend Package Management ----------------------------- #
.PHONY: bun-install bun-add bun-update bun-remove bun-lock-regenerate

# Usage:
#   make bun-install
#   make bun-add PKG=package
#   make bun-update PKG=package
#   make bun-remove PKG=package
bun-install:
	docker compose exec -T frontend-hackspain bun install

bun-add:
	docker compose exec -T frontend-hackspain bun add $(PKG)

bun-update:
	docker compose exec -T frontend-hackspain bun update $(PKG)

bun-remove:
	docker compose exec -T frontend-hackspain bun remove $(PKG)

bun-lock-regenerate:
	docker compose exec -T frontend-hackspain bun install --lockfile-only

# ----------------------------- Terminals ----------------------------- #
.PHONY: backend-shell frontend-shell neo4j-shell

backend-shell:
	docker compose exec backend-hackspain sh

frontend-shell:
	docker compose exec frontend-hackspain sh

neo4j-shell:
	docker compose exec neo4j-hackspain cypher-shell -u $${NEO4J_USER:-neo4j} -p $${NEO4J_PASSWORD:-hackspain-local}

# ----------------------------- Debugging ----------------------------- #
.PHONY: logs-backend logs-frontend logs-neo4j logs

# App logs never include the database — that is logs-neo4j.
logs-backend:
	docker compose logs -f backend-hackspain

logs-frontend:
	docker compose logs -f frontend-hackspain

logs-neo4j:
	docker compose logs -f neo4j-hackspain

logs:
	docker compose logs -f backend-hackspain frontend-hackspain

# ----------------------------- Code Formatting ----------------------------- #
.PHONY: lint-backend lint-frontend lint format-backend format-frontend format lint-fix-backend lint-fix-frontend lint-fix

# Usage:
#   make format-backend / make format-frontend / make format
#   make lint-fix-backend / make lint-fix-frontend / make lint-fix
#   make lint
lint-backend:
	docker compose exec -T backend-hackspain uv run ruff check .

lint-frontend:
	docker compose exec -T frontend-hackspain bun run lint

lint:
	make lint-backend
	make lint-frontend

format-backend:
	docker compose exec -T backend-hackspain uv run ruff format .

format-frontend:
	docker compose exec -T frontend-hackspain bun run format

format:
	make format-backend
	make format-frontend

lint-fix-backend:
	docker compose exec -T backend-hackspain uv run ruff check --fix .

lint-fix-frontend:
	docker compose exec -T frontend-hackspain bun run lint --fix

lint-fix:
	make lint-fix-backend
	make lint-fix-frontend

# ----------------------------- Testing ----------------------------- #
.PHONY: test-backend test-frontend test-agent eval-integrity monitor-eval test

test-backend:
	docker compose exec -T backend-hackspain uv run pytest $(TEST)

test-frontend:
	docker compose exec -T frontend-hackspain bun run test

test-agent:
	$(MAKE) -C agent test TEST="$(TEST)"

eval-integrity:
	docker compose exec -T backend-hackspain uv run pytest app/evals/tests/test_happyrobot_cases.py

monitor-eval:
	docker compose exec -T backend-hackspain uv run python -m app.evals.run_monitor_regression --output /tmp/monitor-eval.json

.PHONY: lab seed-lab help-lab
lab:
	@echo "Monitor lab (stack must be up):"
	@echo "  http://localhost:8000/lab"
	@echo "  http://localhost:8000/lab/conversation"
	@echo "  http://localhost:8000/lab/benchmarks"
	@echo "  http://localhost:8000/lab/inspector"

# GNU make treats `make seed-lab --live-jev` as a make option and dies.
# Pass a variable: LIVE_JEV=1 (JEV, not KEV). Default is degraded (seed_lab clears the key).
LIVE_JEV ?=
SEED_LAB_FLAGS :=
ifneq ($(LIVE_JEV),)
SEED_LAB_FLAGS += --live-jev
endif

help-lab:
	@echo "Lab / evals:"
	@echo "  make lab                     print lab URLs"
	@echo "  make seed-lab                ingest Sentinel+HappyRobot, degraded Jev"
	@echo "  make seed-lab LIVE_JEV=1     keep TYPESAFE_API_KEY (not: make seed-lab --live-jev)"
	@echo "  make monitor-eval            72 HappyRobot traces through service.ingest"
	@echo "  make eval-integrity          HappyRobot corpus pytest"

seed-lab:
	docker compose exec -T backend-hackspain uv run python -m app.evals.seed_lab $(SEED_LAB_FLAGS)

test:
	make test-backend
	make test-frontend

# ----------------------------- Agent stack ----------------------------- #
# The sandbox fleet lives in compose.agents.yaml on agentnet — a separate file
# and network from the product stack, so containment can never hit the viewer.
.PHONY: agents-build agents-up agents-down collect

agents-build:
	@echo ":: agents-build: compose.agents.yaml"
	mkdir -p .local/harness/decisions .local/runs
	docker compose -f compose.agents.yaml up --build -d

agents-up:
	@echo ":: agents-up: compose.agents.yaml"
	mkdir -p .local/harness/decisions .local/runs
	docker compose -f compose.agents.yaml up -d

agents-down:
	@echo ":: agents-down: compose.agents.yaml"
	docker compose -f compose.agents.yaml down

# Forward one sandbox's JSONL events to the ingest API (needs the product stack up).
collect:
	./scripts/collect.sh hackspain_agent $(RUN_ID)

# ----------------------------- Experiments ----------------------------- #
.PHONY: bench bench-analyze bench-plots

# Subset: make bench ARGS="pipeline slow_burn_50"
bench:
	docker compose exec -T backend-hackspain uv run python /experiments/bench.py $(ARGS)

bench-analyze:
	docker compose exec -T backend-hackspain uv run python /experiments/analysis.py $(ARGS)

bench-plots:
	docker compose exec -T backend-hackspain uv run python /experiments/plots.py

# ----------------------------- ⛔️ DANGER ZONE ⛔️ ----------------------------- #
.PHONY: clean clean-builder

# NUCLEAR: drops named volumes, Neo4j data included. `make up` rebuilds from zero.
clean:
	docker compose down --volumes --remove-orphans

clean-builder: clean
	docker builder prune -f
