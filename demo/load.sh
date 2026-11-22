#!/usr/bin/env bash
# Load the 27 cases of the 2026-11-22 real-model run into a fresh local stack: every case waits for the analyst, as
# the run left it, and Bank and Card start from their seeded data. Run it again to undo what a demo changed.
#   demo/load.sh          asks before deleting the local stack's volumes
#   demo/load.sh --yes    does not ask
set -euo pipefail
cd "$(dirname "$0")/.."
compose() { docker compose -f docker-compose.yml -f demo/docker-compose.demo.yml "$@"; }

if [ "${1:-}" != "--yes" ]; then
  read -r -p "This deletes the local stack's volumes (its databases and kept monthly reports), then loads the demo cases. Go on? [y/N] " answer
  case "$answer" in y | Y) ;; *) exit 1 ;; esac
fi

compose down -v
# --wait uses Postgres's healthcheck, which passes once the init scripts have made the databases and roles.
compose up -d --wait postgres
gunzip -c demo/cases-2026-11-22.sql.gz | compose exec -T postgres psql -U postgres -d orchestrator -v ON_ERROR_STOP=1 -q >/dev/null
compose up -d

echo "Loaded. Cases by status:"
compose exec -T postgres psql -U postgres -d orchestrator -At -c "select status, count(*) from cases group by status order by status"
model=$(compose exec -T orchestrator sh -c 'echo "$LLM_MODEL"')
if [ -z "$model" ]; then
  echo "LLM_MODEL is empty: the queue lists the cases, but their reports and decisions need a model set (neither calls it)."
fi
