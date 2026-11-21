#!/usr/bin/env bash
# Build the images with compose and push them to the ECR repositories create.sh made, as :latest.
# The aws-k3s overlay pulls :latest on every pod start, so a push and `k8s/up.sh aws-k3s` deploy the new code.
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck disable=SC1091
. ./state.env
cd ../..

docker compose build ${REPOS}
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
for repo in ${REPOS}; do
  docker tag "credit-review-$repo:latest" "$REGISTRY/credit-review/$repo:latest"
  docker push -q "$REGISTRY/credit-review/$repo:latest"
done
docker logout "$REGISTRY" >/dev/null
