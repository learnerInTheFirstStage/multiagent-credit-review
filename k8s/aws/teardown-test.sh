#!/usr/bin/env bash
# Checks teardown.sh with a stand-in aws and pkill first on PATH: no AWS account is used and no process is stopped.
#
#   k8s/aws/teardown-test.sh     exit status 0 when every check passed
# shellcheck disable=SC2016  # the stand-ins and the checks expand their own arguments
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

run() {  # run <name> <aws arguments that fail> [extra state.env line]: a copy of teardown.sh, in $work/<name>
  local dir="$work/$1"
  mkdir -p "$dir/bin"
  cp "$here/teardown.sh" "$dir/"
  cat >"$dir/state.env" <<EOF
REGION="us-east-2"
REPORTS_BUCKET="reports-test"
INSTANCE="i-0000test"
SG="sg-0000test"
EIP_ALLOCATION="eipalloc-0000test"
EIP_ASSOCIATION="eipassoc-0000test"
PUBLIC_IP="203.0.113.10"
ROLE="credit-review-k3s"
PROFILE="credit-review-k3s"
REPOS="auth bank"
${3:-}
EOF
  printf '#!/bin/bash\necho "$*" >>"%s/aws.log"\ncase "$*" in *"%s"*) exit 1 ;; esac\n' "$dir" "$2" >"$dir/bin/aws"
  printf '#!/bin/bash\nprintf "%%s" "$2" >"%s/pkill.pattern"\nexit 1\n' "$dir" >"$dir/bin/pkill"
  chmod +x "$dir/bin/aws" "$dir/bin/pkill"
  touch "$dir/aws.log"
  PATH="$dir/bin:/usr/bin:/bin" bash "$dir/teardown.sh" >"$dir/out" 2>&1
  echo "$?" >"$dir/status"
}

run failed delete-security-group
check "a failed step keeps state.env" test -f "$work/failed/state.env"
check "a failed step exits non-zero, does not say Deleted, and names what may remain" \
  bash -c '[ "$(cat "$1/status")" != 0 ] && ! grep -q Deleted "$1/out" && grep -qx "  security group sg-0000test" "$1/out"' _ "$work/failed"
check "the steps after a failed one still run" grep -q "ecr delete-repository" "$work/failed/aws.log"

run created no-such-call 'BUCKET_CREATED="1"'
check "with every step done, state.env is removed" test ! -f "$work/created/state.env"
check "the Elastic IP is released" grep -q "ec2 release-address --region us-east-2 --allocation-id eipalloc-0000test" "$work/created/aws.log"
check "a bucket create.sh made is deleted" grep -q "s3 rb s3://reports-test --force" "$work/created/aws.log"
check "the released address is named for removal from Auth0" grep -q "Remove https://203.0.113.10 from the Auth0 app" "$work/created/out"

run existing no-such-call
check "a bucket create.sh did not make is kept" bash -c '! grep -q "s3 rb" "$1/aws.log"' _ "$work/existing"

# connect.sh's tunnel as ps shows it: the aws CLI, then the plugin it starts.
pattern="$(cat "$work/created/pkill.pattern")"
tunnel() {
  printf 'aws ssm start-session --region us-east-2 --target %s --document-name AWS-StartPortForwardingSession --parameters portNumber=6443,localPortNumber=16443\n' "$1"
  printf 'session-manager-plugin AWS_SSM_START_SESSION_RESPONSE us-east-2 StartSession  {"Target": "%s", "DocumentName": "AWS-StartPortForwardingSession"} https://ssm.us-east-2.amazonaws.com\n' "$1"
}
check "the tunnel to this instance is closed, both processes" bash -c '! grep -vqE "$1"' _ "$pattern" < <(tunnel i-0000test)
check "a tunnel to another instance is left alone" bash -c '! grep -qE "$1"' _ "$pattern" < <(tunnel i-0000other)

exit "$failures"
