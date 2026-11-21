#!/usr/bin/env bash
# Create the AWS side of the k3s demo: one EC2 instance running k3s, reached only through SSM.
#
#   REPORTS_BUCKET=<bucket> k8s/aws/create.sh     then k8s/aws/push-images.sh, k8s/aws/connect.sh, k8s/up.sh aws-k3s
#   k8s/aws/teardown.sh                           deletes everything this script made
#
# Made, and written to k8s/aws/state.env so teardown.sh can find it:
#   ECR repositories credit-review/{auth,bank,card,orchestrator,frontend}
#   IAM role and instance profile credit-review-k3s: SSM, ECR read, and write to REPORTS_BUCKET only
#   Security group credit-review-k3s with no inbound rule: nothing is reachable from the internet
#   EC2 m7i-flex.large (free-tier eligible on newer accounts), Amazon Linux 2023, 30 GB gp3, IMDSv2 only
#
# Credentials come from the AWS CLI's own chain (aws login, AWS_PROFILE), never from a file in the repo.
set -euo pipefail

cd "$(dirname "$0")"
REGION="${AWS_REGION:-$(aws configure get region)}"
NAME=credit-review-k3s
INSTANCE_TYPE="${INSTANCE_TYPE:-m7i-flex.large}"
K3S_VERSION="${K3S_VERSION:-v1.35.9+k3s1}"
REPOS=(auth bank card orchestrator frontend)
: "${REPORTS_BUCKET:?Set REPORTS_BUCKET, the bucket the monthly reports go to}"
[ -f state.env ] && { echo "state.env exists: this demo is already up. Run teardown.sh first."; exit 1; }

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
save() { printf "%s=\"%s\"\n" "$1" "$2" >>state.env; }
save REGION "$REGION"
save REGISTRY "$REGISTRY"
save REPORTS_BUCKET "$REPORTS_BUCKET"

echo "== ECR repositories"
for repo in "${REPOS[@]}"; do
  aws ecr create-repository --region "$REGION" --repository-name "credit-review/$repo" \
    --image-scanning-configuration scanOnPush=true --query repository.repositoryName --output text
done
save REPOS "${REPOS[*]}"

echo "== IAM role and instance profile $NAME"
aws iam create-role --role-name "$NAME" --query Role.Arn --output text --assume-role-policy-document '{
  "Version": "2012-10-17",
  "Statement": [{"Effect": "Allow", "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}'
save ROLE "$NAME"
aws iam attach-role-policy --role-name "$NAME" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam attach-role-policy --role-name "$NAME" --policy-arn arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly
# The monthly report (KAN-73): list the bucket, write new copies and read them back for the download link.
aws iam put-role-policy --role-name "$NAME" --policy-name monthly-reports --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [
    {\"Effect\": \"Allow\", \"Action\": \"s3:ListBucket\", \"Resource\": \"arn:aws:s3:::$REPORTS_BUCKET\"},
    {\"Effect\": \"Allow\", \"Action\": [\"s3:PutObject\", \"s3:GetObject\"], \"Resource\": \"arn:aws:s3:::$REPORTS_BUCKET/monthly/*\"}]}"
aws iam create-instance-profile --instance-profile-name "$NAME" --query InstanceProfile.Arn --output text
aws iam add-role-to-instance-profile --instance-profile-name "$NAME" --role-name "$NAME"
save PROFILE "$NAME"

echo "== Security group $NAME, no inbound rule"
VPC="$(aws ec2 describe-vpcs --region "$REGION" --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
SG="$(aws ec2 create-security-group --region "$REGION" --group-name "$NAME" --vpc-id "$VPC" \
  --description "credit-review k3s demo: no inbound, reached through SSM" --query GroupId --output text)"
save SG "$SG"
echo "$SG"

echo "== EC2 $INSTANCE_TYPE"
AMI="$(aws ssm get-parameter --region "$REGION" --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query Parameter.Value --output text)"
USER_DATA="$(mktemp)"
trap 'rm -f "$USER_DATA"' EXIT
sed -e "s|__REGION__|$REGION|; s|__REGISTRY__|$REGISTRY|; s|__K3S_VERSION__|$K3S_VERSION|" user-data.sh >"$USER_DATA"
# A new instance profile takes a few seconds to be usable by EC2.
for attempt in 1 2 3 4 5 6; do
  INSTANCE="$(aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type "$INSTANCE_TYPE" \
    --iam-instance-profile Name="$NAME" --security-group-ids "$SG" --user-data "file://$USER_DATA" \
    --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=30,VolumeType=gp3,DeleteOnTermination=true}' \
    --metadata-options 'HttpTokens=required,HttpPutResponseHopLimit=2,HttpEndpoint=enabled' \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME}]" \
    --query 'Instances[0].InstanceId' --output text 2>/dev/null)" && break
  echo "instance profile not ready yet, retrying ($attempt)"; sleep 10
done
[ -n "${INSTANCE:-}" ] || { echo "could not start the instance"; exit 1; }
save INSTANCE "$INSTANCE"
echo "$INSTANCE"
aws ec2 wait instance-running --region "$REGION" --instance-ids "$INSTANCE"
echo "Running. k3s installs in a few minutes; then k8s/aws/push-images.sh and k8s/aws/connect.sh."
