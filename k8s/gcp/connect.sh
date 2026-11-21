#!/usr/bin/env bash
# Reach the k3s cluster on the VM through an IAP tunnel: nothing on the VM is open to the internet.
#
#   k8s/gcp/connect.sh          waits for k3s, saves its kubeconfig to k8s/gcp/kubeconfig, opens the tunnel
#   export KUBECONFIG=k8s/gcp/kubeconfig
#
# SSH (for the kubeconfig) and the tunnel both go through IAP, signed in as the gcloud login, with OS Login.
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck disable=SC1091
. ./state.env
PORT="${PORT:-26443}"   # not 16443, so the AWS tunnel can stay open at the same time

on_vm() {
  gcloud compute ssh "$VM" --project="$PROJECT" --zone="$ZONE" --tunnel-through-iap --quiet \
    --strict-host-key-checking=no --command="$1" 2>/dev/null
}

echo "== waiting for k3s"
until [ "$(on_vm 'test -f /var/lib/k3s-ready && echo ready' || true)" = ready ]; do sleep 10; done

echo "== saving the kubeconfig (admin credentials: k8s/gcp/kubeconfig, git-ignored, mode 600)"
umask 077
on_vm 'sudo cat /etc/rancher/k3s/k3s.yaml' | sed "s|https://127.0.0.1:6443|https://127.0.0.1:$PORT|" >kubeconfig

if ! nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
  echo "== opening the tunnel to the cluster's API on 127.0.0.1:$PORT"
  nohup gcloud compute start-iap-tunnel "$VM" 6443 --project="$PROJECT" --zone="$ZONE" \
    --local-host-port="localhost:$PORT" >/dev/null 2>&1 &
  until nc -z 127.0.0.1 "$PORT" 2>/dev/null; do sleep 1; done
fi
KUBECONFIG="$PWD/kubeconfig" kubectl get nodes
echo
echo "export KUBECONFIG=\"$PWD/kubeconfig\""
