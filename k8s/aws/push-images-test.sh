#!/usr/bin/env bash
# Checks push-images.sh with a stand-in docker and aws first on PATH: nothing is built, pushed or signed in.
#
#   k8s/aws/push-images-test.sh     exit status 0 when every check passed
# shellcheck disable=SC2016  # the stand-ins expand their own arguments
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

run() {  # run <name> [VAR=value in the caller's shell]: a copy of push-images.sh, in $work/<name>/k8s/aws
  local dir="$work/$1"
  shift
  mkdir -p "$dir/k8s/aws" "$dir/bin"
  cp "$here/push-images.sh" "$dir/k8s/aws/"
  cat >"$dir/k8s/aws/state.env" <<EOF
REGION="us-east-2"
REGISTRY="000000000000.dkr.ecr.us-east-2.amazonaws.com"
REPOS="auth orchestrator"
EOF
  # The stand-in reads the password on login, as docker does, so the script runs to its end.
  printf '#!/bin/sh\n[ "$1" = login ] && cat >/dev/null\necho "platform=${DOCKER_DEFAULT_PLATFORM:-} $*" >>"%s/docker.log"\n' "$dir" >"$dir/bin/docker"
  printf '#!/bin/sh\necho stand-in-password\n' >"$dir/bin/aws"
  chmod +x "$dir/bin/docker" "$dir/bin/aws"
  env -u DOCKER_DEFAULT_PLATFORM "$@" PATH="$dir/bin:/usr/bin:/bin" /bin/bash "$dir/k8s/aws/push-images.sh" >"$dir/out.log" 2>&1
  echo "$?" >"$dir/status"
}
built_for_amd64() {  # the script finished, built, and every compose build ran for linux/amd64
  local log="$work/$1/docker.log"
  [ "$(cat "$work/$1/status")" = 0 ] && grep -q ' compose build ' "$log" &&
    ! grep ' compose build ' "$log" | grep -qv '^platform=linux/amd64 '
}

run plain
check "the images are built for the x86_64 instance" built_for_amd64 plain
run arm DOCKER_DEFAULT_PLATFORM=linux/arm64
check "a caller's arm64 default does not reach the build" built_for_amd64 arm

exit "$failures"
