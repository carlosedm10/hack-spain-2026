#!/usr/bin/env bash
set -euo pipefail

RUN_ID=${1:?usage: contain.sh <run_id> [--dry-run]}
DRY_RUN=${2:-}
CONTAINER=${AGENT_CONTAINER:-hackspain_agent}
TOKENS_FILE=${AGENT_TOKENS_FILE:-proxy/tokens}

run() {
  if [[ "$DRY_RUN" == "--dry-run" ]]; then
    printf '+ %q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

run docker pause "$CONTAINER"

if [[ "$DRY_RUN" == "--dry-run" ]]; then
  echo "+ remove proxy token for run $RUN_ID from $TOKENS_FILE"
elif [[ -f "$TOKENS_FILE" ]]; then
  tmp=$(mktemp)
  awk -v run="$RUN_ID" '$1 != run {print}' "$TOKENS_FILE" >"$tmp"
  mv "$tmp" "$TOKENS_FILE"
fi
