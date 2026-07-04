#!/usr/bin/env bash
# Post-deploy smoke check (redesign P2) — run ON THE BOX after a restart:
#   bash scripts/smoke.sh
# Verifies every dashboard-facing surface actually serves, so a bad deploy is
# caught at the terminal instead of by tomorrow's watchdog email.
set -u
API=http://127.0.0.1:8000
DASH=http://127.0.0.1:3000
ENV_FILE="$(dirname "$0")/../.env"
TOKEN=$(grep -E '^API_TOKEN=' "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d ' \r')
AUTH=()
[ -n "${TOKEN:-}" ] && AUTH=(-H "Authorization: Bearer $TOKEN")

fail=0
check() { # check <label> <url> <must-contain> [extra curl args...]
  local label="$1" url="$2" needle="$3"; shift 3
  local body
  body=$(curl -sS -m 15 "$@" "$url" 2>&1)
  if [ $? -ne 0 ]; then echo "FAIL  $label — curl error: $body"; fail=1; return; fi
  if echo "$body" | grep -q "$needle"; then
    echo "ok    $label"
  else
    echo "FAIL  $label — missing '$needle' in response: ${body:0:160}"
    fail=1
  fi
}

echo "== API =="
check "health"          "$API/api/health"          '"db_ok"'            "${AUTH[@]}"
check "health schedule" "$API/api/health"          '"schedule"'         "${AUTH[@]}"
check "today"           "$API/api/today"           '"window_start_ms"'  "${AUTH[@]}"
check "policies"        "$API/api/policies/state"  '"trend"'            "${AUTH[@]}"
check "studies"         "$API/api/studies"         '"studies"'          "${AUTH[@]}"
check "longterm"        "$API/api/longterm/latest" '"tier"'             "${AUTH[@]}"
check "stock health"    "$API/api/stock/health"    '"ok"'               "${AUTH[@]}"

echo "== Dashboard =="
check "/ (Today)"  "$DASH/"        "Today"
check "/btc"       "$DASH/btc"     "Bitcoin"
check "/stocks"    "$DASH/stocks"  "recording\|Swing"
check "/lab"       "$DASH/lab"     "Lab"

echo
if [ $fail -eq 0 ]; then echo "SMOKE: all green"; else echo "SMOKE: FAILURES above"; fi
exit $fail
