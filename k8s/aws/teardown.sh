#!/usr/bin/env bash
# Delete everything create.sh made, as listed in state.env. The reports bucket and its locked copies are not touched.
set -uo pipefail

cd "$(dirname "$0")"
[ -f state.env ] || { echo "No state.env: nothing to delete."; exit 0; }
# shellcheck disable=SC1091
. ./state.env

pkill -f "AWS-StartPortForwardingSession.*${INSTANCE:-none}" 2>/dev/null && echo "tunnel closed"

if [ -n "${INSTANCE:-}" ]; then
  echo "== terminating $INSTANCE"
  aws ec2 terminate-instances --region "$REGION" --instance-ids "$INSTANCE" --query 'TerminatingInstances[0].CurrentState.Name' --output text
  aws ec2 wait instance-terminated --region "$REGION" --instance-ids "$INSTANCE"
fi
if [ -n "${SG:-}" ]; then
  echo "== security group $SG"
  aws ec2 delete-security-group --region "$REGION" --group-id "$SG"
fi
if [ -n "${PROFILE:-}" ]; then
  echo "== instance profile and role $PROFILE"
  aws iam remove-role-from-instance-profile --instance-profile-name "$PROFILE" --role-name "$ROLE"
  aws iam delete-instance-profile --instance-profile-name "$PROFILE"
fi
if [ -n "${ROLE:-}" ]; then
  aws iam delete-role-policy --role-name "$ROLE" --policy-name monthly-reports
  aws iam detach-role-policy --role-name "$ROLE" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
  aws iam detach-role-policy --role-name "$ROLE" --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
  aws iam delete-role --role-name "$ROLE"
fi
for repo in ${REPOS:-}; do
  echo "== ECR credit-review/$repo"
  aws ecr delete-repository --region "$REGION" --repository-name "credit-review/$repo" --force --query repository.repositoryName --output text
done
rm -f state.env kubeconfig
echo "Deleted. The reports bucket $REPORTS_BUCKET is kept."
