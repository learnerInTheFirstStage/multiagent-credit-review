#!/usr/bin/env bash
# Checks that up.sh refuses .env.example's JWT_SECRET on a public address, with a stand-in kubectl first on PATH:
# no cluster is used.
#
#   k8s/up-test.sh     exit status 0 when every check passed
# shellcheck disable=SC2016  # the stand-in and the checks expand their own arguments
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
failures=0
check() {  # check "<label>" command...
  local label="$1"
  shift
  if "$@"; then echo "ok    $label"; else echo "FAIL  $label"; failures=1; fi
}

run() {  # run <name> <JWT_SECRET> <PUBLIC_IP>: a copy of up.sh aws-k3s, in $work/<name>
  local dir="$work/$1"
  mkdir -p "$dir/bin" "$dir/k8s/aws"
  cp "$here/up.sh" "$dir/k8s/"
  cp "$here/aws/caddy.yaml" "$dir/k8s/aws/"
  touch "$dir/k8s/aws/kubeconfig" "$dir/kubectl.log"
  printf 'REGION="us-east-2"\nREGISTRY="registry.test"\nREPORTS_BUCKET="reports-test"\nPUBLIC_IP="%s"\n' "$3" >"$dir/k8s/aws/state.env"
  printf 'JWT_SECRET=%s\n' "$2" >"$dir/.env"
  printf '#!/bin/bash\necho "$*" >>"%s/kubectl.log"\ncat >/dev/null\nexit 0\n' "$dir" >"$dir/bin/kubectl"
  chmod +x "$dir/bin/kubectl"
  PATH="$dir/bin:/usr/bin:/bin" "$BASH" "$dir/k8s/up.sh" aws-k3s </dev/null >"$dir/out" 2>&1
  echo "$?" >"$dir/status"
}
secret_applied() { grep -q "create secret generic credit-review" "$1/kubectl.log"; }

run public local-jwt-secret-for-development-only 203.0.113.10
check "a public address with the public JWT_SECRET is refused, and the Secret is never made" \
  bash -c '[ "$(cat "$1/status")" != 0 ] && grep -q "needs a real JWT_SECRET" "$1/out" &&
    ! grep -q "create secret generic" "$1/kubectl.log"' _ "$work/public"

run public-real "$(openssl rand -hex 24)" 203.0.113.10
check "a public address with a real JWT_SECRET goes on" secret_applied "$work/public-real"

run tunnel local-jwt-secret-for-development-only ""
check "without a public address the public JWT_SECRET is taken, as on kind" secret_applied "$work/tunnel"

exit "$failures"
