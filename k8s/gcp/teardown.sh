#!/usr/bin/env bash
# Delete everything create.sh made: the whole Google Cloud project it created for this demo, with its VM, registry,
# network, service account and reports bucket. Billing for it stops at once; Google keeps the project restorable
# for 30 days (gcloud projects undelete), then removes it for good.
#
# Only the project named in state.env is deleted, and only one create.sh made (its id starts with credit-review-exit-).
set -uo pipefail

cd "$(dirname "$0")"
[ -f state.env ] || { echo "No state.env: nothing to delete."; exit 0; }
# shellcheck disable=SC1091
. ./state.env
case "${PROJECT:-}" in
  credit-review-exit-*) ;;
  *) echo "state.env names project '${PROJECT:-}', which create.sh did not make. Nothing deleted."; exit 1 ;;
esac

pkill -f "start-iap-tunnel ${VM:-none} " 2>/dev/null && echo "tunnel closed"
[ -n "${REGION:-}" ] && docker logout "$REGION-docker.pkg.dev" >/dev/null 2>&1

echo "== deleting the project $PROJECT"
gcloud projects delete "$PROJECT" --quiet || exit 1
rm -f state.env kubeconfig
echo "Deleted. Restorable until 30 days from now with: gcloud projects undelete $PROJECT"
