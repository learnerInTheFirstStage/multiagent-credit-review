#!/usr/bin/env bash
# Delete everything create.sh made, as listed in state.env. The reports bucket is deleted, with its reports, only when
# create.sh made it (BUCKET_CREATED=1); a bucket that was there before is kept.
# A step that fails does not stop the rest. If any failed, state.env is kept, as the record of what may remain.
#
#   k8s/aws/teardown-test.sh     checks this script with a stand-in aws: no AWS account, no tunnel touched
set -uo pipefail

cd "$(dirname "$0")" || exit 1
[ -f state.env ] || { echo "No state.env: nothing to delete."; exit 0; }
# shellcheck disable=SC1091
. ./state.env

failed=()
try() {  # try "<what may remain if it fails>" command...
  local what="$1"
  shift
  "$@" || failed+=("$what")
}

# connect.sh's tunnel: the aws CLI and its session-manager-plugin both name the instance before the document.
pkill -f "${INSTANCE:-none}.*AWS-StartPortForwardingSession" 2>/dev/null && echo "tunnel closed"

if [ -n "${EIP_ALLOCATION:-}" ]; then
  echo "== Elastic IP ${PUBLIC_IP:-} ($EIP_ALLOCATION)"
  if [ -n "${EIP_ASSOCIATION:-}" ]; then
    try "Elastic IP $EIP_ALLOCATION" aws ec2 disassociate-address --region "$REGION" --association-id "$EIP_ASSOCIATION"
  fi
  try "Elastic IP $EIP_ALLOCATION" aws ec2 release-address --region "$REGION" --allocation-id "$EIP_ALLOCATION"
fi
if [ -n "${INSTANCE:-}" ]; then
  echo "== terminating $INSTANCE"
  try "instance $INSTANCE" aws ec2 terminate-instances --region "$REGION" --instance-ids "$INSTANCE" \
    --query 'TerminatingInstances[0].CurrentState.Name' --output text
  try "instance $INSTANCE" aws ec2 wait instance-terminated --region "$REGION" --instance-ids "$INSTANCE"
fi
if [ -n "${SG:-}" ]; then
  echo "== security group $SG"
  try "security group $SG" aws ec2 delete-security-group --region "$REGION" --group-id "$SG"
fi
if [ -n "${PROFILE:-}" ]; then
  echo "== instance profile and role $PROFILE"
  try "instance profile $PROFILE" aws iam remove-role-from-instance-profile --instance-profile-name "$PROFILE" --role-name "$ROLE"
  try "instance profile $PROFILE" aws iam delete-instance-profile --instance-profile-name "$PROFILE"
fi
if [ -n "${ROLE:-}" ]; then
  try "IAM role $ROLE" aws iam delete-role-policy --role-name "$ROLE" --policy-name monthly-reports
  try "IAM role $ROLE" aws iam detach-role-policy --role-name "$ROLE" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
  try "IAM role $ROLE" aws iam detach-role-policy --role-name "$ROLE" --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
  try "IAM role $ROLE" aws iam delete-role --role-name "$ROLE"
fi
for repo in ${REPOS:-}; do
  echo "== ECR credit-review/$repo"
  try "ECR repository credit-review/$repo" aws ecr delete-repository --region "$REGION" \
    --repository-name "credit-review/$repo" --force --query repository.repositoryName --output text
done
if [ "${BUCKET_CREATED:-}" = 1 ]; then
  echo "== reports bucket $REPORTS_BUCKET and its reports"
  try "S3 bucket $REPORTS_BUCKET" aws s3 rb "s3://$REPORTS_BUCKET" --force --region "$REGION"
  bucket="deleted"
else
  bucket="kept"
fi

if [ "${#failed[@]}" -gt 0 ]; then
  echo
  echo "Not everything was deleted. These may remain, and may still cost money:"
  printf '  %s\n' "${failed[@]}" | uniq
  echo "state.env is kept. Fix the cause and run teardown.sh again: steps already done then fail as not found."
  echo "Once none of the above is left (check in the AWS console), delete k8s/aws/state.env and kubeconfig."
  exit 1
fi
rm -f state.env kubeconfig
if [ -n "${PUBLIC_IP:-}" ]; then
  echo "Remove https://$PUBLIC_IP from the Auth0 app's callback, logout and web origin URLs (services/auth/README.md)."
fi
echo "Deleted. The reports bucket ${REPORTS_BUCKET:-} is $bucket."
