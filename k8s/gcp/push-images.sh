#!/usr/bin/env bash
# Build the images with compose and push them to the Artifact Registry repository create.sh made, as :latest.
# The gcp-k3s overlay pulls :latest on every pod start, so a push and `k8s/up.sh gcp-k3s` deploy the new code.
set -euo pipefail

cd "$(dirname "$0")"
# shellcheck disable=SC1091
. ./state.env
cd ../..

docker compose build ${REPOS}
# A short-lived token of the gcloud login, for this push only; nothing is added to Docker's config for good.
gcloud auth print-access-token | docker login --username oauth2accesstoken --password-stdin "https://$REGION-docker.pkg.dev" >/dev/null
for repo in ${REPOS}; do
  docker tag "credit-review-$repo:latest" "$REGISTRY/$repo:latest"
  docker push -q "$REGISTRY/$repo:latest"
done
docker logout "$REGION-docker.pkg.dev" >/dev/null
