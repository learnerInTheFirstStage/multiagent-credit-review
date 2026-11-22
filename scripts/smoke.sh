#!/bin/bash
# Checks the properties the skeleton promises. Run it with the stack up:
#   docker compose up --build -d && ./scripts/smoke.sh
# A stack started with -p or --env-file: pass the same through docker compose's own variables,
# or the compose calls below act on the default project and its .env:
#   COMPOSE_PROJECT_NAME=demo COMPOSE_ENV_FILES=/path/to/.env ./scripts/smoke.sh
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

# An unrouted /api/<service>/ path falls through to the frontend, which answers 200 with its index.html.
# So the check is that the answer is not the service's own health JSON.
check_not_routed() {
  local service="$1"
  if [[ "$(curl -s -m 5 "$GATEWAY/api/$service/health")" == *"\"service\":\"$service\""* ]]; then
    echo "FAIL  $service is routed by the gateway"
    failed=1
  else
    echo "ok    $service is not routed by the gateway"
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
check_not_routed bank
check_not_routed card
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

# The Orchestrator logs the request ID the gateway returned. /health is not logged, so this is a 401 that is.
request_id="$(curl -s -o /dev/null -m 5 -D - "$GATEWAY/api/orchestrator/cases" \
  | tr -d '\r' | awk 'tolower($1) == "x-request-id:" { print $2 }')"
logged=0
for _ in $(seq 1 5); do
  if [ -n "$request_id" ] && [[ "$(docker compose logs orchestrator 2>&1)" == *"$request_id"* ]]; then
    logged=1
    break
  fi
  sleep 1
done
if [ "$logged" = 1 ]; then
  echo "ok    orchestrator logs the gateway's request ID"
else
  echo "FAIL  orchestrator does not log the gateway's request ID (${request_id:-none returned})"
  failed=1
fi

# The SSE stream sends the access token as ?token=. The gateway's access log must not write it.
# The request's access line is found by its own User-Agent. A fake token gets a 401.
agent="smoke-$$-$RANDOM"
curl -s -o /dev/null -m 5 -A "$agent" "$GATEWAY/api/orchestrator/cases/events?token=eyJsmoke.not.a.token"
line=""
for _ in $(seq 1 5); do
  line="$(docker compose logs gateway 2>&1 | grep -F "$agent")"
  [ -n "$line" ] && break
  sleep 1
done
if [ -z "$line" ]; then
  echo "FAIL  gateway's access log has no line for the event stream request"
  failed=1
elif [[ "$(docker compose logs gateway 2>&1)" == *"eyJsmoke"* ]]; then
  echo "FAIL  gateway's logs write the token"
  failed=1
else
  echo "ok    gateway's logs leave out the token"
fi

# The gateway must keep routing after a service container is recreated.
# Same project and env file as the calls above, so the recreated container keeps the stack's settings.
docker compose up -d --force-recreate orchestrator >/dev/null 2>&1
if wait_for "$GATEWAY/api/orchestrator/health"; then
  echo "ok    gateway still routes after the orchestrator is recreated"
else
  echo "FAIL  gateway lost the orchestrator after it was recreated"
  failed=1
fi

exit "$failed"
