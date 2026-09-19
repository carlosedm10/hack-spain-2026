#!/usr/bin/env bash
# Forward one sandbox's JSONL events to the ingest API. The harness marks each
# event line with __hs_event__ on stdout; docker logs capture is host-side, so
# the agent cannot retract an event — it could forge marker lines, which is an
# accepted gap for now.
# Usage: scripts/collect.sh <container> [run_id]
set -euo pipefail

CONTAINER=${1:?usage: collect.sh <container> [run_id]}
RUN_ID=${2:-${RUN_ID:-demo}}
API=${API_URL:-http://localhost:8000}

exec python3 "$(dirname "$0")/collect.py" "$CONTAINER" "$RUN_ID" "$API"
