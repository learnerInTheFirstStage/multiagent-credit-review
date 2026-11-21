#!/bin/bash
# EC2 user data: k3s on Amazon Linux 2023, pulling our images from ECR with the instance's role.
# create.sh fills in __REGION__, __REGISTRY__ and __K3S_VERSION__.
set -euxo pipefail

REGION=__REGION__
REGISTRY=__REGISTRY__

# k3s pulls from ECR with a token the instance's role gets. A token lasts 12 hours, so a timer renews it.
cat >/usr/local/bin/ecr-auth.sh <<'SCRIPT'
#!/bin/bash
set -euo pipefail
token="$(aws ecr get-login-password --region "$1")"
umask 077
mkdir -p /etc/rancher/k3s
printf 'configs:\n  "%s":\n    auth:\n      username: AWS\n      password: "%s"\n' "$2" "$token" >/etc/rancher/k3s/registries.yaml
SCRIPT
chmod 700 /usr/local/bin/ecr-auth.sh
/usr/local/bin/ecr-auth.sh "$REGION" "$REGISTRY"

cat >/etc/systemd/system/ecr-auth.service <<UNIT
[Unit]
Description=Renew k3s's ECR login
[Service]
Type=oneshot
ExecStart=/usr/local/bin/ecr-auth.sh $REGION $REGISTRY
# k3s reads registries.yaml at start. Its pods keep running through a restart.
ExecStartPost=/usr/bin/systemctl restart k3s
UNIT
cat >/etc/systemd/system/ecr-auth.timer <<'UNIT'
[Unit]
Description=Renew k3s's ECR login every 6 hours
[Timer]
OnBootSec=6h
OnUnitActiveSec=6h
[Install]
WantedBy=timers.target
UNIT

# Secrets encrypted at rest in k3s's datastore. Traefik is not needed: the gateway is the way in.
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=__K3S_VERSION__ sh -s - server \
  --secrets-encryption --disable traefik --write-kubeconfig-mode 600

systemctl daemon-reload
systemctl enable --now ecr-auth.timer
touch /var/lib/k3s-ready
