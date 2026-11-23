#!/usr/bin/env bash
# Reach the k3s cluster on the instance through an SSM tunnel: the cluster's API is not open to the internet.
#
#   k8s/aws/connect.sh          waits for k3s, saves its kubeconfig to k8s/aws/kubeconfig, opens the tunnel
#   export KUBECONFIG=k8s/aws/kubeconfig
#
# Needs the AWS CLI's Session Manager plugin (session-manager-plugin) on PATH.
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck disable=SC1091
. ./state.env
PORT="${PORT:-16443}"
command -v session-manager-plugin >/dev/null || { echo "Install the Session Manager plugin first."; exit 1; }

run() {  # run a shell command on the instance through SSM and print its output
  local id status
  id="$(aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE" --document-name AWS-RunShellScript \
    --parameters "commands=[\"$1\"]" --query Command.CommandId --output text)"
  for _ in $(seq 60); do
    status="$(aws ssm get-command-invocation --region "$REGION" --command-id "$id" --instance-id "$INSTANCE" \
      --query Status --output text 2>/dev/null || echo Pending)"
    case "$status" in Success|Failed|Cancelled|TimedOut) break ;; esac
    sleep 2
  done
  aws ssm get-command-invocation --region "$REGION" --command-id "$id" --instance-id "$INSTANCE" \
    --query StandardOutputContent --output text
}

echo "== waiting for the instance to join SSM"
until [ "$(aws ssm describe-instance-information --region "$REGION" --filters "Key=InstanceIds,Values=$INSTANCE" \
  --query 'length(InstanceInformationList)' --output text)" = 1 ]; do sleep 5; done
echo "== waiting for k3s"
until [ "$(run 'test -f /var/lib/k3s-ready && echo ready')" = ready ]; do sleep 10; done

echo "== saving the kubeconfig (admin credentials: k8s/aws/kubeconfig, git-ignored, mode 600)"
umask 077
run 'cat /etc/rancher/k3s/k3s.yaml' | sed "s|https://127.0.0.1:6443|https://127.0.0.1:$PORT|" >kubeconfig

if ! nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
  echo "== opening the tunnel to the cluster's API on 127.0.0.1:$PORT"
  nohup aws ssm start-session --region "$REGION" --target "$INSTANCE" --document-name AWS-StartPortForwardingSession \
    --parameters "portNumber=6443,localPortNumber=$PORT" >/dev/null 2>&1 &
  until nc -z 127.0.0.1 "$PORT" 2>/dev/null; do sleep 1; done
fi
KUBECONFIG="$PWD/kubeconfig" kubectl get nodes
echo
echo "export KUBECONFIG=\"$PWD/kubeconfig\""
