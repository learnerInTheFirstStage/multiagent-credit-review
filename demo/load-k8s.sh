#!/usr/bin/env bash
# demo/load.sh for Kubernetes: load the 27 cases of the 2026-11-22 real-model run into a cluster k8s/up.sh made, and
# put all 27 customers on Bank, Card, the Orchestrator and the workers, as docker-compose.demo.yml does on compose.
# Run it again to undo what a demo changed. See demo/README.md.
#   demo/load-k8s.sh local          the kind cluster, through the kubectl context kind-credit-review
#   demo/load-k8s.sh aws-k3s        k3s on EC2, through k8s/aws/kubeconfig while k8s/aws/connect.sh's tunnel is open
#   --yes                           does not ask before deleting the databases
#   --fresh c_011,c_201,c_301       removes these customers' prepared open cases, so a live signal opens a fresh
#                                   investigation; their past reviews stay
set -euo pipefail
cd "$(dirname "$0")/.."
NAMESPACE=credit-review

usage() { echo "Usage: demo/load-k8s.sh local|aws-k3s [--yes] [--fresh c_011,c_201,c_301]" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }

case "${1:-}" in
  local | aws-k3s) target="$1"; shift ;;
  *) usage ;;
esac
yes="" fresh=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) yes=1 ;;
    --fresh) [ $# -ge 2 ] || usage; fresh="$2"; shift ;;
    *) usage ;;
  esac
  shift
done
fresh_ids=()
if [ -n "$fresh" ]; then
  IFS=, read -ra fresh_ids <<<"$fresh"
  for id in "${fresh_ids[@]}"; do
    [[ "$id" =~ ^c_[0-9]{3}$ ]] || { echo "Not a customer id: '$id'" >&2; exit 1; }
  done
fi

if [ "$target" = local ]; then
  kube=(kubectl --context kind-credit-review)
else
  [ -f k8s/aws/kubeconfig ] || { echo "No k8s/aws/kubeconfig: run k8s/aws/connect.sh first." >&2; exit 1; }
  export KUBECONFIG="$PWD/k8s/aws/kubeconfig"
  kube=(kubectl)
fi
k() { "${kube[@]}" -n "$NAMESPACE" "$@"; }
k get statefulset postgres >/dev/null || { echo "No answer from the cluster, or k8s/up.sh $target has not run." >&2; exit 1; }

if [ -z "$yes" ]; then
  read -r -p "This deletes the orchestrator, bank and card databases on $target, then loads the demo cases. Go on? [y/N] " answer
  case "$answer" in y | Y) ;; *) exit 1 ;; esac
fi

step "Pausing KEDA and stopping everything that opens the databases"
k annotate scaledobject worker autoscaling.keda.sh/paused-replicas=0 --overwrite >/dev/null
k wait scaledobject/worker --for=condition=Paused --timeout=60s >/dev/null
k scale deploy/orchestrator deploy/worker deploy/bank deploy/card --replicas=0 >/dev/null
# A stopping worker first finishes the case it holds, within its 600 s grace period.
deadline=$((SECONDS + 660))
while [ -n "$(k get pods -l 'app in (orchestrator,worker,bank,card)' --field-selector=status.phase!=Failed,status.phase!=Succeeded -o name)" ]; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "Still running after 11 minutes. Nothing was deleted; run this again once they have stopped." >&2
    k get pods -l 'app in (orchestrator,worker,bank,card)' --field-selector=status.phase!=Failed,status.phase!=Succeeded >&2
    exit 1
  fi
  sleep 3
done

step "Recreating the orchestrator, bank and card databases, empty, as infra/postgres/init makes them"
for db in orchestrator bank card; do
  k exec postgres-0 -- psql -U postgres -v ON_ERROR_STOP=1 -q -c "DROP DATABASE IF EXISTS $db WITH (FORCE)" \
    -c "CREATE DATABASE $db OWNER $db" -c "REVOKE ALL ON DATABASE $db FROM PUBLIC"
done

step "Loading demo/cases-2026-11-22.sql.gz into the orchestrator database"
# Nothing opens this database before the snapshot is in: the Orchestrator makes its tables when it starts.
gunzip -c demo/cases-2026-11-22.sql.gz |
  k exec -i postgres-0 -- psql -U postgres -d orchestrator -v ON_ERROR_STOP=1 -q >/dev/null

if [ -n "$fresh" ]; then
  step "Removing the prepared open case of $fresh"
  # The open statuses are OPEN_STATUSES in services/orchestrator/app/db/models.py. A case's saved graph state is kept
  # under its case_id (app/runner.py). The past reviews belong to other, closed cases.
  removed="$(k exec -i postgres-0 -- psql -U postgres -d orchestrator -1 -v ON_ERROR_STOP=1 -q -At -v ids="$fresh" <<'SQL'
CREATE TEMP TABLE fresh ON COMMIT DROP AS
  SELECT case_id FROM cases
  WHERE customer_id = ANY (string_to_array(:'ids', ','))
    AND status IN ('queued', 'investigating', 'waiting_for_data', 'needs_approval', 'needs_manager', 'awaiting_documents');
DELETE FROM signals WHERE case_id IN (SELECT case_id FROM fresh);
DELETE FROM outage_waits WHERE case_id IN (SELECT case_id FROM fresh);
DELETE FROM checkpoint_writes WHERE thread_id IN (SELECT case_id FROM fresh);
DELETE FROM checkpoint_blobs WHERE thread_id IN (SELECT case_id FROM fresh);
DELETE FROM checkpoints WHERE thread_id IN (SELECT case_id FROM fresh);
DELETE FROM cases WHERE case_id IN (SELECT case_id FROM fresh) RETURNING 'Removed ' || case_id || ' of ' || customer_id;
SQL
)"
  [ -z "$removed" ] || echo "$removed"
  for id in "${fresh_ids[@]}"; do
    grep -q " of $id\$" <<<"$removed" || echo "$id had no open case to remove."
  done
fi

step "The demo's data files, in the ConfigMap demo-data, mounted at /demo"
k create configmap demo-data --from-file=eval/extra_customers.yaml --from-file=eval/history_eval.yaml \
  --dry-run=client -o yaml | k apply -f - >/dev/null
# docker-compose.demo.yml's settings, with the files under /demo. The workers get the Orchestrator's: on Kubernetes they
# run the cases, and with SPECIALISTS=stand_in they read the customers from those files.
demo_settings() {  # the deployment, which is also its container's name; its env entries as JSON
  k patch deploy "$1" --type strategic -p "{\"spec\":{\"template\":{\"spec\":{
    \"volumes\":[{\"name\":\"demo-data\",\"configMap\":{\"name\":\"demo-data\"}}],
    \"containers\":[{\"name\":\"$1\",\"env\":[$2],
      \"volumeMounts\":[{\"name\":\"demo-data\",\"mountPath\":\"/demo\",\"readOnly\":true}]}]}}}}" >/dev/null
}
orchestrator_env='{"name":"SHARED_CUSTOMERS_FILE","value":"/data/shared_customers.yaml:/demo/extra_customers.yaml"},
  {"name":"HISTORY_SEED_FILE","value":"/eval/history.yaml:/demo/history_eval.yaml"}'
service_env='{"name":"SHARED_CUSTOMERS_YAML","value":"/app/data/shared_customers.yaml:/demo/extra_customers.yaml"}'
demo_settings orchestrator "$orchestrator_env"
demo_settings worker "$orchestrator_env"
demo_settings bank "$service_env"
demo_settings card "$service_env"

step "Starting Bank and Card, which seed all 27 customers, then the Orchestrator"
k scale deploy/bank deploy/card --replicas=1 >/dev/null
k rollout status deploy/bank --timeout=600s
k rollout status deploy/card --timeout=600s
k scale deploy/orchestrator --replicas=1 >/dev/null
k rollout status deploy/orchestrator --timeout=900s

step "Letting KEDA run the workers again"
k annotate scaledobject worker autoscaling.keda.sh/paused-replicas- >/dev/null
# KEDA brings it back to minReplicaCount at its next poll. Starting it here lets rollout status wait for a running worker.
k scale deploy/worker --replicas=1 >/dev/null
k rollout status deploy/worker --timeout=900s

step "Loaded"
echo "Cases by status:"
k exec postgres-0 -- psql -U postgres -d orchestrator -At -c "select status, count(*) from cases group by status order by status"
k exec postgres-0 -- psql -U postgres -d orchestrator -At -c "select count(*) || ' past reviews' from reviews"
for db in bank card; do
  k exec postgres-0 -- psql -U postgres -d "$db" -At -c "select count(*) || ' customers on $db' from customers"
done
k get pods
model="$(k exec deploy/orchestrator -c orchestrator -- sh -c 'echo "$LLM_MODEL"')"
if [ -z "$model" ]; then
  echo "LLM_MODEL is empty: the queue lists the cases, but their reports and decisions need a model set (neither calls it)."
fi
