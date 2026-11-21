#!/usr/bin/env bash
# Create the Google Cloud side of the k3s demo, the same shape as k8s/aws/: one VM running k3s, reached only through
# Identity-Aware Proxy (IAP), in a NEW project made for it, so deleting that project (teardown.sh) removes all of it.
#
#   k8s/gcp/create.sh        then k8s/gcp/push-images.sh, k8s/gcp/connect.sh, k8s/up.sh gcp-k3s
#   k8s/gcp/teardown.sh      deletes the project
#
# Made, and written to k8s/gcp/state.env:
#   Project credit-review-exit-<random>, linked to BILLING_ACCOUNT (default: the only open billing account)
#   Artifact Registry repository credit-review (Docker)
#   Service account credit-review-k3s: read that repository, write and read the reports bucket, sign its own links
#   Cloud Storage bucket for the monthly reports: no public access, a 1-day retention policy (S3 Object Lock's role)
#   Network credit-review whose only inbound rule is SSH and the k3s API from IAP's range: nothing else reaches it
#   VM e2-standard-2, Debian 12, 30 GB, Shielded VM, OS Login
#
# Every command names the project: gcloud's own default project is never read or changed.
set -euo pipefail

cd "$(dirname "$0")"
REGION="${GCP_REGION:-us-east1}"
ZONE="${GCP_ZONE:-$REGION-b}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-standard-2}"
K3S_VERSION="${K3S_VERSION:-v1.35.9+k3s1}"
NAME=credit-review-k3s
IAP_RANGE=35.235.240.0/20   # where IAP's TCP forwarding connects from
[ -f state.env ] && { echo "state.env exists: this demo is already up. Run teardown.sh first."; exit 1; }

BILLING="${BILLING_ACCOUNT:-$(gcloud billing accounts list --filter=open=true --format='value(name)')}"
[ "$(printf '%s\n' "$BILLING" | grep -c .)" = 1 ] || { echo "Set BILLING_ACCOUNT: there is not exactly one open billing account."; exit 1; }

SUFFIX="$(openssl rand -hex 3)"
PROJECT="credit-review-exit-$SUFFIX"
BUCKET="credit-review-reports-$SUFFIX"
SA="$NAME@$PROJECT.iam.gserviceaccount.com"
REGISTRY="$REGION-docker.pkg.dev/$PROJECT/credit-review"
export CLOUDSDK_CORE_PROJECT="$PROJECT"   # for any command below that would otherwise fall back to the default
save() { printf "%s=\"%s\"\n" "$1" "$2" >>state.env; }

echo "== project $PROJECT"
gcloud projects create "$PROJECT" --name="credit-review exit test" --quiet
save PROJECT "$PROJECT"
save REGION "$REGION"
save ZONE "$ZONE"
gcloud billing projects link "$PROJECT" --billing-account="$BILLING" --format='value(billingEnabled)'
gcloud services enable --project="$PROJECT" compute.googleapis.com artifactregistry.googleapis.com \
  iap.googleapis.com iamcredentials.googleapis.com storage.googleapis.com
# Enabling Compute makes a network called default whose rules let SSH, RDP and ping in from anywhere. Nothing here
# uses it; delete those rules so the project has nothing open.
for rule in default-allow-ssh default-allow-rdp default-allow-icmp; do
  gcloud compute firewall-rules delete "$rule" --project="$PROJECT" --quiet 2>/dev/null || true
done

echo "== Artifact Registry repository credit-review"
gcloud artifacts repositories create credit-review --project="$PROJECT" --location="$REGION" \
  --repository-format=docker --description="credit-review images"
save REGISTRY "$REGISTRY"
save REPOS "auth bank card orchestrator frontend"

echo "== service account $SA"
gcloud iam service-accounts create "$NAME" --project="$PROJECT" --display-name="credit-review k3s VM"
# A new account takes a few seconds to be seen by IAM.
grant() { for attempt in 1 2 3 4 5 6; do "$@" --quiet >/dev/null 2>&1 && return; sleep 10; done; "$@" --quiet >/dev/null; }
grant gcloud artifacts repositories add-iam-policy-binding credit-review --project="$PROJECT" --location="$REGION" \
  --member="serviceAccount:$SA" --role=roles/artifactregistry.reader
# The download link: the VM's account has no private key, so it signs through IAM, which needs this on itself.
grant gcloud iam service-accounts add-iam-policy-binding "$SA" --project="$PROJECT" \
  --member="serviceAccount:$SA" --role=roles/iam.serviceAccountTokenCreator

echo "== bucket gs://$BUCKET"
# A copy cannot be deleted or replaced for a day, as the S3 test bucket's Object Lock does. The policy is not locked,
# so the project can still be deleted.
gcloud storage buckets create "gs://$BUCKET" --project="$PROJECT" --location="$REGION" \
  --uniform-bucket-level-access --public-access-prevention --retention-period=1d
save REPORTS_BUCKET "$BUCKET"
# Create and read, not delete or overwrite: objectCreator has no storage.objects.delete.
grant gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" \
  --role=roles/storage.objectCreator
grant gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" \
  --role=roles/storage.objectViewer

echo "== network $NAME, inbound from IAP only"
gcloud compute networks create "$NAME" --project="$PROJECT" --subnet-mode=custom
gcloud compute networks subnets create "$NAME" --project="$PROJECT" --network="$NAME" --region="$REGION" \
  --range=10.10.0.0/24
gcloud compute firewall-rules create "$NAME-iap" --project="$PROJECT" --network="$NAME" --direction=INGRESS \
  --source-ranges="$IAP_RANGE" --allow=tcp:22,tcp:6443 --description="SSH and the k3s API, through IAP only"

echo "== VM $MACHINE_TYPE in $ZONE"
STARTUP="$(mktemp)"
trap 'rm -f "$STARTUP"' EXIT
sed -e "s|__REGISTRY_HOST__|$REGION-docker.pkg.dev|; s|__K3S_VERSION__|$K3S_VERSION|" startup.sh >"$STARTUP"
# The VM keeps an outbound address (to pull public images and reach the model's API); the firewall lets nothing in.
gcloud compute instances create "$NAME" --project="$PROJECT" --zone="$ZONE" --machine-type="$MACHINE_TYPE" \
  --image-family=debian-12 --image-project=debian-cloud --boot-disk-size=30GB --boot-disk-type=pd-balanced \
  --network="$NAME" --subnet="$NAME" --service-account="$SA" --scopes=cloud-platform \
  --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
  --metadata=enable-oslogin=TRUE,block-project-ssh-keys=TRUE --metadata-from-file=startup-script="$STARTUP" \
  --format='value(name,status)'
save VM "$NAME"
echo "Running. k3s installs in a few minutes; then k8s/gcp/push-images.sh and k8s/gcp/connect.sh."
