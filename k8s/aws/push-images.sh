#!/usr/bin/env bash
# Build the images with compose and push them to the ECR repositories create.sh made, as :latest.
# The aws-k3s overlay pulls :latest on every pod start, so a push and `k8s/up.sh aws-k3s` deploy the new code.
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck disable=SC1091
. ./state.env
cd ../..

# The instance is x86_64 (create.sh's AMI). Compose builds for the machine it runs on, so on Apple Silicon it
# would push arm64 images the node cannot start.
export DOCKER_DEFAULT_PLATFORM=linux/amd64
docker compose build ${REPOS}
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$REGISTRY" >/dev/null
for repo in ${REPOS}; do
  docker tag "credit-review-$repo:latest" "$REGISTRY/credit-review/$repo:latest"
  docker push -q "$REGISTRY/credit-review/$repo:latest"
done
docker logout "$REGISTRY" >/dev/null
