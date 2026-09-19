#!/usr/bin/env bash
# Fire the HappyRobot staging pager and open the live action wallboard.
# Secrets live in gitignored .env.
# Usage: scripts/page.sh
#        ONCALL_PHONE=+34600000000 scripts/page.sh
#        scripts/page.sh '{"tipo_emergencia":"…","pautas":"…","nivel_gravedad":"4","nombre_contacto":"…","telefono":"+34600000000","nodos":"…"}'
# PAGE_WATCH=0  fire without polling
# PAGE_OPEN=0   fire without opening the browser
# LADDER_URL    defaults to http://localhost:3000/ladder
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a

: "${HAPPYROBOT_HOOK_URL:?set HAPPYROBOT_HOOK_URL in .env}"
: "${HAPPYROBOT_API_KEY:?set HAPPYROBOT_API_KEY in .env}"
: "${ONCALL_PHONE:?set ONCALL_PHONE in .env}"

LADDER_URL="${LADDER_URL:-http://localhost:3000/ladder}"
STATUS_PATH="$ROOT/assets/quiver/pager-status.json"

PAYLOAD=$(PAGE_JSON="${1-}" python3 - <<'PY'
import json, os

def e164(raw: str) -> str:
    raw = (raw or "").strip()
    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return ""
    if raw.startswith("+"):
        return "+" + digits
    if digits.startswith("34") and len(digits) >= 11:
        return "+" + digits
    if len(digits) == 9:
        return "+34" + digits
    return "+" + digits

raw = os.environ.get("PAGE_JSON") or ""
payload = json.loads(raw) if raw else {
    "tipo_emergencia": "La AGI se ha escapado de su sandbox",
    "pautas": "Ya estamos cortando el acceso a internet. No hace falta autorizar el corte.",
    "nivel_gravedad": "4",
    "nombre_contacto": os.environ.get("ONCALL_NAME") or "",
    "nodos": (
        "Ha leido secretos, los ha enviado a un host desconocido "
        "y ha intentado salir del sandbox."
    ),
}
if not payload.get("telefono"):
    payload["telefono"] = e164(os.environ.get("ONCALL_PHONE") or "")
if not payload["telefono"]:
    raise SystemExit("ONCALL_PHONE missing")
print(json.dumps(payload))
PY
)

LEVEL=$(python3 -c 'import json,sys
p=json.loads(sys.argv[1])
print(p.get("nivel_gravedad") or 4)
' "$PAYLOAD")

python3 - "$STATUS_PATH" "$LEVEL" <<'PY'
import json, sys, time
from pathlib import Path
path, level = Path(sys.argv[1]), sys.argv[2]
try:
    n = int(str(level).lower().replace("l", "").replace("crítico", "4").replace("critico", "4") or "4")
except ValueError:
    n = 4
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({
    "level": n if 1 <= n <= 5 else 4,
    "call": "ringing",
    "contain": "idle",
    "run_id": "",
    "source": "happyrobot",
    "hr_status": "starting",
    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}, indent=2) + "\n")
PY

if [[ "${PAGE_OPEN:-1}" != "0" ]]; then
  if command -v open >/dev/null; then
    open "$LADDER_URL"
  elif command -v xdg-open >/dev/null; then
    xdg-open "$LADDER_URL" >/dev/null 2>&1 || true
  fi
fi
echo "ladder: $LADDER_URL"

RESP=$(curl -sS -X POST "$HAPPYROBOT_HOOK_URL" \
  -H "Authorization: Bearer $HAPPYROBOT_API_KEY" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")
printf '%s\n' "$RESP"

RUN_ID=$(python3 -c 'import json,sys
raw=sys.stdin.read()
try:
    data=json.loads(raw)
except Exception:
    sys.exit(0)
rid=data.get("run_id") or data.get("id")
if isinstance(data.get("data"), dict):
    rid=rid or data["data"].get("run_id") or data["data"].get("id")
print(rid or "")
' <<<"$RESP")

if [[ -z "${RUN_ID}" ]]; then
  echo "page.sh: no run_id in HappyRobot response; ladder will not update" >&2
  exit 0
fi

if [[ "${PAGE_WATCH:-1}" == "0" ]]; then
  python3 "$ROOT/scripts/pager_watch.py" --run-id "$RUN_ID" --level "$LEVEL" --once
  exit 0
fi

python3 "$ROOT/scripts/pager_watch.py" --run-id "$RUN_ID" --level "$LEVEL"
