#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=${1:-}
NETWORK=${AGENT_BRIDGE:-agentnet}
PROXY_CONTAINER=${EGRESS_PROXY_CONTAINER:-hackspain_egress}

if [[ "$DRY_RUN" == "--dry-run" ]]; then
  echo "+ docker network disconnect $NETWORK $PROXY_CONTAINER"
  exit 0
fi

docker network disconnect "$NETWORK" "$PROXY_CONTAINER"
