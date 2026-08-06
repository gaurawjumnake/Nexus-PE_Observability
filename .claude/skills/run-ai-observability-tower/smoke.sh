#!/usr/bin/env bash
# Smoke-tests the Nexus KPI Pipeline FastAPI server end-to-end.
# Assumes the server is already up (see SKILL.md "Run" section) and that
# NO_PROXY includes localhost/127.0.0.1 (a corporate HTTP_PROXY otherwise
# intercepts loopback traffic and returns a 502).
set -euo pipefail

HOST="${HOST:-http://127.0.0.1:8005}"
export NO_PROXY="localhost,127.0.0.1,${NO_PROXY:-}"

pass=0
fail=0

check() {
  local desc="$1" method="$2" path="$3" data="${4:-}"
  local code
  if [ -n "$data" ]; then
    code=$(curl -s -o /tmp/smoke_body.json -w "%{http_code}" -X "$method" "$HOST$path" \
      -H "Content-Type: application/json" -d "$data" --max-time 60)
  else
    code=$(curl -s -o /tmp/smoke_body.json -w "%{http_code}" -X "$method" "$HOST$path" --max-time 30)
  fi
  if [[ "$code" == 2* ]]; then
    echo "PASS  [$code] $desc"
    pass=$((pass+1))
  else
    echo "FAIL  [$code] $desc"
    echo "      body: $(head -c 300 /tmp/smoke_body.json)"
    fail=$((fail+1))
  fi
}

check "liveness"              GET  /health
check "db connectivity"       GET  /health/db
check "list all KPIs"         GET  /kpis/
check "list jobs"             GET  /jobs/?limit=3
check "agentic chat ask (sql route)" POST /chat/ask \
  '{"message":"What is the active_ai_users KPI for fluke in 2024?","company_id":"fluke","session_id":"smoke-test"}'

echo
echo "Results: $pass passed, $fail failed"
[ "$fail" -eq 0 ]