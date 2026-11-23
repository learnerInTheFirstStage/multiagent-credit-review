#!/usr/bin/env bash
# Deploy the whole system to Kubernetes. Run from anywhere in the checkout:
#
#   k8s/up.sh              a local kind cluster: create it if needed, build and load the images, deploy
#   k8s/up.sh aws-k3s      k3s on EC2 (k8s/aws/): after create.sh, push-images.sh and connect.sh.
#                          With create.sh's PUBLIC_HTTPS=1, also Caddy (k8s/aws/caddy.yaml) on https://<Elastic IP>
#   k8s/up.sh gcp-k3s      k3s on a Google Cloud VM (k8s/gcp/): the same three scripts there
#   kind delete cluster --name credit-review       remove the local cluster
#
# Needs docker, kind (local only) and kubectl. The secrets come from the repo-root .env, as compose reads them.
# Anything .env leaves out gets compose's local fallback on kind, and a random value in a cloud, kept across re-runs.
# They go into the cluster only, never into a file in the repo.
set -euo pipefail

cd "$(dirname "$0")/.."
OVERLAY="${1:-local}"
CLUSTER=credit-review
NAMESPACE=credit-review
KEDA_VERSION=2.21.0
IMAGES=(auth bank card orchestrator frontend)            # built by compose as credit-review-<name>
PUBLIC_IP=""                                             # from k8s/aws/state.env only, never from the shell

step() { printf '\n== %s\n' "$*"; }

case "$OVERLAY" in
  local)
    if ! kind get clusters | grep -qx "$CLUSTER"; then
      step "Creating the kind cluster $CLUSTER"
      kind create cluster --config k8s/kind.yaml
    fi
    kubectl config use-context "kind-$CLUSTER" >/dev/null

    step "Building the images with compose (the worker uses the Orchestrator's)"
    docker compose build "${IMAGES[@]}"
    # Only the images built here. The cluster pulls postgres, rabbitmq and nginx from Docker Hub itself: kind
    # cannot load a multi-platform image ("content digest ... not found").
    step "Loading the images into the cluster"
    kind load docker-image --name "$CLUSTER" "${IMAGES[@]/#/credit-review-}"
    ;;
  aws-k3s|gcp-k3s)
    CLOUD="k8s/${OVERLAY%-k3s}"
    [ -f "$CLOUD/state.env" ] && [ -f "$CLOUD/kubeconfig" ] || { echo "Run $CLOUD/create.sh and connect.sh first."; exit 1; }
    # shellcheck disable=SC1090
    . "$CLOUD/state.env"
    export KUBECONFIG="$PWD/$CLOUD/kubeconfig"
    kubectl get nodes >/dev/null || { echo "No answer from the cluster: is $CLOUD/connect.sh's tunnel open?"; exit 1; }
    ;;
  *) echo "Unknown overlay $OVERLAY: local, aws-k3s or gcp-k3s"; exit 1 ;;
esac

step "Installing KEDA $KEDA_VERSION, which scales the workers"
kubectl apply --server-side --force-conflicts \
  -f "https://github.com/kedacore/keda/releases/download/v$KEDA_VERSION/keda-$KEDA_VERSION.yaml" >/dev/null
kubectl -n keda rollout status deployment/keda-operator --timeout=300s

step "Secrets and the Postgres setup script"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
(
  set -a
  # shellcheck disable=SC1091
  [ -f .env ] && . ./.env
  set +a
  # A password .env leaves out: compose's fallback on kind; in a cloud the one already in the cluster, or a new random one.
  pick() {
    local name="$1" fallback="$2" current
    if [ -n "${!name:-}" ]; then printf '%s' "${!name}"; return; fi
    if [ "$OVERLAY" = local ]; then printf '%s' "$fallback"; return; fi
    current="$(kubectl -n "$NAMESPACE" get secret credit-review -o "jsonpath={.data.$name}" 2>/dev/null | base64 -d || true)"
    if [ -n "$current" ]; then printf '%s' "$current"; else openssl rand -hex 24; fi
  }
  env_file="$(mktemp)"
  trap 'rm -f "$env_file"' EXIT
  chmod 600 "$env_file"
  rabbit_user="${RABBITMQ_USER:-credit-review}"
  rabbit_password="$(pick RABBITMQ_PASSWORD local-rabbit)"
  cat >"$env_file" <<EOF
POSTGRES_PASSWORD=$(pick POSTGRES_PASSWORD local-postgres)
AUTH_DB_PASSWORD=$(pick AUTH_DB_PASSWORD local-auth)
BANK_DB_PASSWORD=$(pick BANK_DB_PASSWORD local-bank)
CARD_DB_PASSWORD=$(pick CARD_DB_PASSWORD local-card)
ORCHESTRATOR_DB_PASSWORD=$(pick ORCHESTRATOR_DB_PASSWORD local-orchestrator)
JWT_SECRET=$(pick JWT_SECRET local-jwt-secret-for-development-only)
RABBITMQ_USER=$rabbit_user
RABBITMQ_PASSWORD=$rabbit_password
RABBITMQ_MANAGEMENT_URL=http://$rabbit_user:$rabbit_password@rabbitmq.$NAMESPACE.svc.cluster.local:15672/
LLM_MODEL=${LLM_MODEL:-}
LLM_API_KEY=${LLM_API_KEY:-}
LLM_STRUCTURED_OUTPUT=${LLM_STRUCTURED_OUTPUT:-}
LANGFUSE_PUBLIC_KEY=${LANGFUSE_PUBLIC_KEY:-}
LANGFUSE_SECRET_KEY=${LANGFUSE_SECRET_KEY:-}
LANGFUSE_BASE_URL=${LANGFUSE_BASE_URL:-}
EOF
  # .env.example's secret is public: on a public address, anyone could sign tokens with it.
  if [ -n "${PUBLIC_IP:-}" ] && grep -qx 'JWT_SECRET=local-jwt-secret-for-development-only' "$env_file"; then
    echo "A public address needs a real JWT_SECRET in .env"; exit 1
  fi
  kubectl -n "$NAMESPACE" create secret generic credit-review --from-env-file="$env_file" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  # People's sign-in: .env's Auth0, as compose takes it. The frontend image is built with it too, and the Auth0 app
  # allows kind's http://127.0.0.1:8088, a cloud's port-forward on :8089 and each public address
  # (services/auth/README.md). scripts/e2e.py needs it empty (k8s/README.md, The end-to-end check).
  kubectl -n "$NAMESPACE" create configmap auth0 \
    --from-literal=AUTH0_DOMAIN="${AUTH0_DOMAIN:-}" --from-literal=AUTH0_AUDIENCE="${AUTH0_AUDIENCE:-}" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
)
kubectl -n "$NAMESPACE" create configmap postgres-init --from-file=infra/postgres/init/ \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

step "Applying k8s/overlays/$OVERLAY"
running="$(kubectl -n "$NAMESPACE" get deployment -o name 2>/dev/null || true)"
if [ "$OVERLAY" = local ]; then
  kubectl apply -k k8s/overlays/local
else
  # The account's registry and bucket are filled in here, so neither is committed.
  kubectl kustomize "k8s/overlays/$OVERLAY" |
    sed -e "s|IMAGE_REGISTRY|$REGISTRY|g" -e "s|REPORTS_BUCKET_NAME|$REPORTS_BUCKET|g" -e "s|AWS_REGION_NAME|$REGION|g" |
    kubectl apply -f -
fi
# Public HTTPS, when create.sh made an Elastic IP. The address is filled in here, so it is not committed.
if [ -n "${PUBLIC_IP:-}" ]; then
  sed "s|PUBLIC_IP_ADDRESS|$PUBLIC_IP|g" k8s/aws/caddy.yaml | kubectl apply -f -
fi
# On a re-run the images may be new under the same names: restart what runs them, so the pods take them up.
if [ -n "$running" ]; then
  kubectl -n "$NAMESPACE" rollout restart deployment >/dev/null
fi

step "Waiting for every service"
for deployment in auth bank card orchestrator worker frontend gateway; do
  kubectl -n "$NAMESPACE" rollout status "deployment/$deployment" --timeout=900s
done
if [ -n "${PUBLIC_IP:-}" ]; then
  kubectl -n "$NAMESPACE" rollout status deployment/caddy --timeout=300s
fi

if [ "$OVERLAY" = local ]; then
  echo; echo "Up. The app: http://127.0.0.1:8088"
elif [ -n "${PUBLIC_IP:-}" ]; then
  echo; echo "Up. The app: https://$PUBLIC_IP (its first certificate can take a minute)"
else
  echo; echo "Up. The app: kubectl -n $NAMESPACE port-forward svc/gateway 8089:80, then http://127.0.0.1:8089"
fi
cat <<EOF
  kubectl -n $NAMESPACE get pods                       what runs
  kubectl -n $NAMESPACE get scaledobject,hpa           the workers' scaling
  kubectl -n $NAMESPACE logs deploy/worker -f          a worker's log
EOF
