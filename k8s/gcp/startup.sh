#!/bin/bash
# The VM's startup script (it runs on every boot): k3s on Debian 12, pulling our images from Artifact Registry with
# the VM's service account. create.sh fills in __REGISTRY_HOST__ and __K3S_VERSION__.
set -euxo pipefail

REGISTRY_HOST=__REGISTRY_HOST__
NAMESPACE=credit-review

# Secrets encrypted at rest in k3s's datastore. Traefik is not needed: the gateway is the way in.
if ! command -v k3s >/dev/null; then
  curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=__K3S_VERSION__ sh -s - server \
    --secrets-encryption --disable traefik --write-kubeconfig-mode 600
fi
until k3s kubectl get --raw /readyz >/dev/null 2>&1; do sleep 2; done

# The pods pull with an access token of the VM's account, from the metadata server. A token lasts an hour, so a timer
# puts a fresh one in the namespace's pull secret every 10 minutes. Unlike k3s's registries.yaml, a secret is read at
# each pull, so k3s never restarts for it.
cat >/usr/local/bin/registry-auth.sh <<SCRIPT
#!/bin/bash
set -euo pipefail
token="\$(curl -sf -H 'Metadata-Flavor: Google' \
  http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token |
  sed -E 's/.*"access_token":"([^"]+)".*/\1/')"
k3s kubectl create namespace $NAMESPACE --dry-run=client -o yaml | k3s kubectl apply -f - >/dev/null
k3s kubectl -n $NAMESPACE create secret docker-registry artifact-registry --docker-server=$REGISTRY_HOST \
  --docker-username=oauth2accesstoken --docker-password="\$token" --dry-run=client -o yaml | k3s kubectl apply -f - >/dev/null
until k3s kubectl -n $NAMESPACE get serviceaccount default >/dev/null 2>&1; do sleep 2; done
k3s kubectl -n $NAMESPACE patch serviceaccount default -p '{"imagePullSecrets":[{"name":"artifact-registry"}]}' >/dev/null
SCRIPT
chmod 700 /usr/local/bin/registry-auth.sh
/usr/local/bin/registry-auth.sh

cat >/etc/systemd/system/registry-auth.service <<'UNIT'
[Unit]
Description=Put a fresh Artifact Registry token in the credit-review pull secret
[Service]
Type=oneshot
ExecStart=/usr/local/bin/registry-auth.sh
UNIT
cat >/etc/systemd/system/registry-auth.timer <<'UNIT'
[Unit]
Description=Renew the Artifact Registry pull secret every 10 minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=10min
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now registry-auth.timer
touch /var/lib/k3s-ready
