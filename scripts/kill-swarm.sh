#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=${1:-}
RUN_LOG_DIR=${RUN_LOG_DIR:-/var/lib/hackspain/runs}
FORENSICS_DIR=${FORENSICS_DIR:-/var/lib/hackspain/forensics/$(date -u +%Y%m%dT%H%M%SZ)}

if [[ "$DRY_RUN" == "--dry-run" ]]; then
  echo "+ mkdir -p $FORENSICS_DIR"
  echo "+ cp -a $RUN_LOG_DIR/. $FORENSICS_DIR/"
  echo "+ docker compose -f compose.agents.yaml down --timeout 5"
  exit 0
fi

mkdir -p "$FORENSICS_DIR"
cp -a "$RUN_LOG_DIR"/. "$FORENSICS_DIR"/
docker compose -f compose.agents.yaml down --timeout 5
