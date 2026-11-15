#!/bin/bash
# Checks the properties the skeleton promises. Run it with the stack up:
#   docker compose up --build -d && ./scripts/smoke.sh
set -uo pipefail

GATEWAY="${GATEWAY:-http://localhost:8080}"
failed=0

check() {
  local label="$1" expected="$2" url="$3" actual
  actual="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$url")"
  if [ "$actual" = "$expected" ]; then
    echo "ok    $label"
  else
    echo "FAIL  $label (expected $expected, got $actual)"
    failed=1
  fi
}

wait_for() {
  for _ in $(seq 1 30); do
    [ "$(curl -s -o /dev/null -m 2 -w '%{http_code}' "$1")" = "200" ] && return 0
    sleep 1
  done
  return 1
}

wait_for "$GATEWAY/api/orchestrator/health"

check "gateway answers"                          200 "$GATEWAY/health"
check "frontend through the gateway"             200 "$GATEWAY/"
check "auth through the gateway"                 200 "$GATEWAY/api/auth/health"
check "orchestrator through the gateway"         200 "$GATEWAY/api/orchestrator/health"
check "bank is not routed by the gateway"        404 "$GATEWAY/api/bank/health"
check "card is not routed by the gateway"        404 "$GATEWAY/api/card/health"
check "bank answers on its local port"           200 "http://localhost:8002/health"
check "card answers on its local port"           200 "http://localhost:8003/health"

# A service must not be able to open another service's database.
if docker compose exec -T postgres sh -c \
  'PGPASSWORD="$BANK_DB_PASSWORD" psql -h 127.0.0.1 -U bank -d card -c "select 1"' >/dev/null 2>&1; then
  echo "FAIL  bank role can connect to the card database"
  failed=1
else
  echo "ok    bank role cannot connect to the card database"
fi

# The gateway must keep routing after a service container is recreated.
docker compose up -d --force-recreate orchestrator >/dev/null 2>&1
if wait_for "$GATEWAY/api/orchestrator/health"; then
  echo "ok    gateway still routes after the orchestrator is recreated"
else
  echo "FAIL  gateway lost the orchestrator after it was recreated"
  failed=1
fi

exit "$failed"
