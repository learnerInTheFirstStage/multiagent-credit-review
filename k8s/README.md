# Kubernetes (KAN-75)

The whole system on Kubernetes, so the case workers scale with the number of waiting cases. Docker Compose stays the way to run it day to day. The same files run on kind, on AWS and on Google Cloud. Outside this folder only one thing changed: the monthly report can be kept in Google Cloud Storage (`services/orchestrator/app/reports.py`, `REPORTS_BUCKET=gs://<bucket>`).

## Why

- **Scale where the load is.** A case spends 60–180 s waiting on the model. New cases go through RabbitMQ to the workers (KAN-74), and KEDA sets how many workers run from the queue's length, 1 to 5.
- **Recover on their own, and never run twice what must run once.** Crashed containers restart, and traffic reaches a service only once it is ready. A stopped worker finishes its case first, and a killed one leaves its case to another worker. The Orchestrator, Bank and Card are stopped before their new copy starts, which costs a few seconds per deploy: the gateway, frontend and Auth deploy with no downtime, those three don't.
- **Fair resources.** Each pod has a memory limit, so one heavy case can't starve the rest.
- **Reviewable deployment.** What runs, how many copies, with which settings, is in these files.

## What runs

| Service | Kubernetes | Copies | Why |
|---|---|---|---|
| `worker` | Deployment + KEDA ScaledObject | 1–5, one per case in the queue | the part that scales |
| `orchestrator` | Deployment, `strategy: Recreate` | exactly 1 | it owns the retry loop, the monthly report and the startup pickup, which must not run twice; Recreate means a deploy never runs two |
| `bank`, `card` | Deployment, `strategy: Recreate` | 1 | each seeds its database at start; two copies starting together collide (seen on Card: `duplicate key ... customers_pkey`) |
| `auth` | Deployment | 1 | no state; can go to 2 |
| `frontend`, `gateway` | Deployment | 2 | no state |
| `postgres`, `rabbitmq` | StatefulSet with a volume | 1 | on AWS: RDS and Amazon MQ |

- **Secrets:** a Secret, `credit-review`, made by `up.sh` from the repo-root `.env`. Anything `.env` leaves out gets compose's fallback on kind, and a random value in a cloud, kept across re-runs. It is never written to a file in the repo. Each pod gets only the keys it needs.
- **Settings that are not secret:** `settings` in `base/kustomization.yaml`, with compose's defaults. People's Auth0 settings are in `auth0`, made by `up.sh` from `.env`'s `AUTH0_DOMAIN` and `AUTH0_AUDIENCE`, on kind and in a cloud.
- **The gateway** runs stock nginx with `base/gateway.conf`, a copy of `gateway/nginx.conf`. The original looks names up through Docker's DNS, which Kubernetes doesn't have. **When a route changes in `gateway/nginx.conf`, change it here too.**

## Run it on a laptop

Needs Docker (about 4 GB free for it), [kind](https://kind.sigs.k8s.io/) and `kubectl`.

```bash
k8s/up.sh                                   # cluster, images, KEDA, everything; again after a code change
open http://127.0.0.1:8088                  # the app, through the gateway
kubectl -n credit-review get pods
kind delete cluster --name credit-review    # remove it all
```

With the three `AUTH0_*` values in `.env`, the review app signs people in through Auth0, as on compose: the frontend image is built with them, and `up.sh` gives the Orchestrator and Card the domain and audience. The Auth0 app allows http://127.0.0.1:8088 and http://localhost:8088. Without them nobody can sign in, since the cluster has no dev login.

### The end-to-end check

`scripts/e2e.py` runs unchanged, on a cluster without Auth0: its analyst and manager tokens are made in the containers, and with Auth0 the services refuse them. Empty the `auth0` ConfigMap first (`k8s/up.sh` fills it again):

```bash
kubectl -n credit-review create configmap auth0 --from-literal=AUTH0_DOMAIN= --from-literal=AUTH0_AUDIENCE= \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n credit-review rollout restart deployment/orchestrator deployment/card
kubectl -n credit-review rollout status deployment/orchestrator --timeout=900s
kubectl -n credit-review rollout status deployment/card --timeout=900s
```

Then point it at the cluster:

```bash
kubectl -n credit-review port-forward svc/orchestrator 8034:8000 &
kubectl -n credit-review port-forward svc/card 8033:8000 &
python scripts/e2e.py --orchestrator-url http://127.0.0.1:8034 --card-url http://127.0.0.1:8033 \
  --container "kubectl -n credit-review exec -i deploy/orchestrator -c orchestrator --" \
  --card-container "kubectl -n credit-review exec -i deploy/card -c card --"
```

### Watch the workers scale, without the model

The workers' test mode (KAN-74) sleeps instead of running a case, so this costs nothing:

```bash
kubectl -n credit-review set env deploy/worker MOCK_PROCESSING=1 MOCK_SECONDS=60
# send several signals at once, then:
kubectl -n credit-review get hpa,pods -w
kubectl -n credit-review set env deploy/worker MOCK_PROCESSING- MOCK_SECONDS-   # back to real cases
kubectl apply -k k8s/overlays/local                                             # and the settings' values
```

### The 27 demo cases

To load them into a cluster, here or on AWS: `demo/load-k8s.sh` (`demo/README.md`).

## Run it on AWS: k3s on one EC2 instance

Needs the AWS CLI signed in (`aws login`), its [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html), Docker and `kubectl`. The cluster is reached through an SSM tunnel. Nothing on the instance is open to the internet, unless `PUBLIC_HTTPS=1` (below) opens ports 80 and 443 for the app.

```bash
REPORTS_BUCKET=<your reports bucket> k8s/aws/create.sh   # the bucket if new, ECR, IAM role, security group, EC2 with k3s (k8s/aws/state.env)
k8s/aws/push-images.sh                                   # build the five images for linux/amd64 and push them to ECR
k8s/aws/connect.sh                                       # wait for k3s, save k8s/aws/kubeconfig, open the tunnel
k8s/up.sh aws-k3s                                        # KEDA, secrets, deploy
export KUBECONFIG=$PWD/k8s/aws/kubeconfig
kubectl -n credit-review port-forward svc/gateway 8089:80   # the app on http://127.0.0.1:8089
k8s/aws/teardown.sh                                      # delete everything create.sh made
k8s/aws/teardown-test.sh                                 # check teardown.sh with a stand-in aws: no account, no tunnel touched
k8s/aws/push-images-test.sh                              # check push-images.sh builds for the x86_64 instance: no docker, no AWS
```

- **Instance:** m7i-flex.large (2 vCPU, 8 GB), Amazon Linux 2023, k3s with Secrets encrypted at rest, IMDSv2 only. About $0.096 an hour in us-east-2; free-tier eligible on accounts created after July 2025, drawn from their credits.
- **Images:** from ECR. The instance's role pulls them; a timer renews k3s's ECR login every 6 hours.
- **Reports:** to S3 through the instance's role, which may write to the reports bucket's `monthly/` only. No AWS key is in a pod. A bucket that exists is used as is and kept. One that does not is made by `create.sh` in the region, with SSE-S3, Block Public Access and no Object Lock, and `teardown.sh` deletes it with its reports. Its reports are not locked against early deletion. The Orchestrator sets no retention on a report, so for write-once retention name a bucket with Object Lock and a default retention rule, as the 2026-11-21 test bucket had (GOVERNANCE, 1 day).
- **One instance, one role:** every pod on it shares the instance's AWS permissions. Per-service roles need EKS Pod Identity.
- **Sign-in:** through Auth0, with `.env`'s settings, on http://127.0.0.1:8089 through the port-forward, or on the public address. `scripts/e2e.py` runs through the tunnel as in [The end-to-end check](#the-end-to-end-check), with `KUBECONFIG` set: empty the `auth0` ConfigMap first.
  On a public cluster, run `kubectl -n credit-review scale deploy/caddy --replicas=0` while the `auth0` ConfigMap is empty for e2e. `k8s/up.sh` brings back both.
- **Teardown:** a step that fails does not stop the rest. If any failed, `teardown.sh` lists what may remain and keeps `state.env`, the record of what `create.sh` made. With a public address, remove `https://<the address>` from the Auth0 app's callback, logout and web origin URLs (`services/auth/README.md`).

### A public HTTPS address

```bash
PUBLIC_HTTPS=1 REPORTS_BUCKET=<your reports bucket> k8s/aws/create.sh   # also an Elastic IP, and 80 and 443 open
k8s/aws/push-images.sh && k8s/aws/connect.sh && k8s/up.sh aws-k3s       # up.sh also starts Caddy, then prints https://<the address>
k8s/up-test.sh                                                          # check up.sh refuses .env.example's JWT_SECRET here: no cluster touched
```

- **What it adds:** an Elastic IP tagged `credit-review-k3s`, associated with the instance, and TCP 80 and 443 from anywhere on its security group. The cluster's API stays behind the SSM tunnel. `create.sh` saves `EIP_ALLOCATION`, `EIP_ASSOCIATION` and `PUBLIC_IP` to `state.env`; `teardown.sh` disassociates and releases the address.
- **Caddy** (`aws/caddy.yaml`, Caddy 2.11.6) listens on the node's ports 80 and 443 and passes everything to the gateway. It gets a Let's Encrypt certificate for the IP address itself (ACME profile `shortlived`, HTTP-01 on port 80) and keeps it on the node's disk, in `/var/lib/caddy`, so a restart does not ask again. `up.sh` fills in the address, so it is never committed.
- **The token in the live-updates stream:** the stream carries the access token as `?token=`, and Caddy's error lines quote the URI, so its log replaces that value with `REDACTED`.
- **Sign-in:** each new address must be added to the Auth0 app by hand: `https://<the address>/login` as a callback, `https://<the address>` as a logout and web origin (`services/auth/README.md`).
- **Cost:** one public IPv4, USD 0.005 an hour whether in use or idle ([Amazon VPC pricing](https://aws.amazon.com/vpc/pricing/)). While the instance runs, the Elastic IP takes the place of its own public address. Unlike that one, it is charged while the instance is stopped too, until `teardown.sh` releases it.

## Run it on Google Cloud: k3s on one VM

The same shape as AWS, in a new project made for it, so deleting the project removes everything. Needs `gcloud` signed in with an open billing account, Docker and `kubectl`. Nothing on the VM is open to the internet: SSH and the cluster's API are reached through Identity-Aware Proxy (IAP). Every command names the new project; gcloud's default project is never read or changed.

```bash
k8s/gcp/create.sh                                        # project, Artifact Registry, service account, bucket, network, VM with k3s
k8s/gcp/push-images.sh                                   # build and push the five images to Artifact Registry
k8s/gcp/connect.sh                                       # wait for k3s, save k8s/gcp/kubeconfig, open the IAP tunnel
k8s/up.sh gcp-k3s                                        # KEDA, secrets, deploy
export KUBECONFIG="$PWD/k8s/gcp/kubeconfig"
kubectl -n credit-review port-forward svc/gateway 8099:80   # the app on http://127.0.0.1:8099
k8s/gcp/teardown.sh                                      # delete the project, bucket and all
```

Sign-in through Auth0 needs an address the Auth0 app allows: forward to 8089 instead of 8099, or add 8099 to the app.

| | AWS (`k8s/aws/`) | Google Cloud (`k8s/gcp/`) |
|---|---|---|
| Machine | EC2 m7i-flex.large, Amazon Linux 2023 | Compute Engine e2-standard-2, Debian 12, Shielded VM |
| Way in | SSM Session Manager, no inbound rule; with `PUBLIC_HTTPS=1`, 80 and 443 for Caddy | IAP, inbound only from IAP's range (22 and 6443) |
| Images | ECR; k3s's login renewed every 6 h | Artifact Registry; a pull secret renewed every 10 min from the VM's account |
| Reports | S3: Object Lock GOVERNANCE 1 day on the 2026-11-21 test bucket, none on a bucket `create.sh` makes | Cloud Storage, retention policy 1 day, public access prevented |
| Identity in the pods | the instance's role | the VM's service account; links signed through IAM (no key file) |
| Overlay | `overlays/aws-k3s` | `overlays/gcp-k3s` |
| Cost | about $0.096/h; with `PUBLIC_HTTPS=1`, $0.005/h for the Elastic IP, stopped or not | about $0.08/h (VM, address, disk) |

## Checked on Google Cloud, 2026-11-21 (us-east1)

| Check | Result |
|---|---|
| `k8s/base/` | Deployed unchanged; only the overlay differs |
| `scripts/e2e.py`, real model, through the IAP tunnel | **PASS.** c_010 to `pause_card`, analyst then manager approve, Card shows the card paused. Dispatched through RabbitMQ to a worker. |
| Monthly report on the Orchestrator's start | Last month's (2026-10) written to Cloud Storage on its own |
| Monthly report through the gateway | `201`. In Cloud Storage: `application/pdf`, metadata `format: PDF/A-3U` and its sha256, retained until the next day. The signed link answered 200, and the file's sha256 matched. veraPDF: PDF/A-3u compliant. No AWS or Google key in the pod's environment. |
| Fuller reports for screenshots | 18 more real cases (real model, decided through the analyst and manager steps): 10 closed in November, and 8 whose dates were then **moved back into October in the test database only**, so October has a filled report. Each case moved by whole days (opened, closed, decisions, signal), so its own times stay in order. The October report's figures are real cases with demo dates, not October activity. Both months' earlier, sparser copies are still in the bucket: retained, and a new run adds a copy. |
| A stored copy deleted | Refused for the project's owner (retention policy) and for the VM's account (`403`: it may create, not delete) |
| 8 signals at once, workers in test mode | KEDA went from 1 to 5 workers in about 40 s, the queue drained, and the workers went back to 1 (5, 3, 2, 1) |
| A worker killed mid-case, in test mode | Another worker took the case: 10 published, 1 redelivered, 10 acknowledged, none left |
| From the internet | 22, 6443, 80 and 30080 closed on the VM's address |
| k3s Secrets encryption | Enabled |
| Found and fixed on the way | On a VM, Google's credentials read the account's email as `default` until their first refresh, so the first link failed to sign (the PDF was already kept). Fixed in `reports.py`, with a test; a link that can't be signed now leaves the copy kept and the answer with no link. |

## Checked on AWS, 2026-11-21 (us-east-2)

| Check | Result |
|---|---|
| `scripts/e2e.py`, real model, through the SSM tunnel | **PASS.** c_010 to `pause_card`, analyst then manager approve, Card shows the card paused. Dispatched through RabbitMQ to a worker. |
| Monthly report through the gateway | Written to S3 with the instance's role: `application/pdf`, metadata `format: PDF/A-3U` and its sha256, Object Lock GOVERNANCE. The download link answered 200, and the file's sha256 matched. No AWS key in the pod's environment. |
| 8 signals at once, workers in test mode | KEDA went from 1 to 5 workers in about 10 s, the queue drained, and the workers went back to 1. |
| A worker killed mid-case | Another worker took the case and finished it: 10 published, 1 redelivered, 10 acknowledged, none left. |
| k3s Secrets encryption | `k3s secrets-encrypt status`: Enabled |

## Checked on kind, 2026-11-20

| Check | Result |
|---|---|
| `scripts/e2e.py`, real model (`claude-sonnet-5-5`) | **PASS.** c_010 to `pause_card`, analyst then manager approve, Card shows the card paused. The case went through RabbitMQ to a worker ("dispatched via rabbitmq"). |
| 8 signals at once, workers in test mode (60 s a case) | KEDA went from 1 to 5 workers in about 20 s. The queue drained in about 2 min, and the workers went back to 1 (5, 3, 2, 1). |
| Each case run once | RabbitMQ counted 9 published, 9 delivered, **0 redelivered**, 9 acknowledged. |
| RabbitMQ's readiness check | compose's `rabbitmq-diagnostics ping` starts a whole Erlang VM each time. Under load it timed out, RabbitMQ was marked not ready, and the workers and KEDA lost it. Now a TCP check: node CPU went from about 330% to 26%. |
| Orchestrator redeployed | The old pod had stopped before the new one was created: never two running. |
| A worker stopped normally mid-case (as a deploy or scale-down does) | It logged "stop requested", finished the case 87 s later, acknowledged it, then exited. No other worker ran it. |
| A worker killed mid-case (as a crash) | RabbitMQ gave the case to another worker, which finished it: 2 published, 1 redelivered, 2 acknowledged, none left. |
| A signal sent while the Orchestrator restarts | The gateway answered `502` for about 12 s. The same signal sent again then came back `duplicate`, so a sender that retries can't open a second case. |
| Through the gateway (`127.0.0.1:8088`) | the app, `/api/orchestrator/health`, the live case stream (`/cases/events`), and a monthly report saved to the reports volume |

## Leaving AWS

What the system depends on, and how each part would move to another provider. **Tested: the whole system moved to Google Cloud and passed the same checks** (above). Contracts, costs and timelines belong to the company's exit plan, not here. US guidance expects banks to plan how they would move an activity to another provider or in-house ([Interagency Guidance on Third-Party Relationships, 2023](https://www.federalreserve.gov/frrs/guidance/interagency-guidance-on-third-party-relationships.htm)).

| Dependency | AWS-specific? | How it moves | On Google Cloud |
|---|---|---|---|
| The application (9 services) | No | The same `k8s/` files on any Kubernetes | **Done:** `k8s/base/` unchanged, a new overlay |
| Postgres (RDS on AWS) | Only the managed service | Dump and restore into any Postgres 16 | Ran in the cluster; Cloud SQL would be the managed one |
| RabbitMQ (Amazon MQ on AWS) | Only the managed service | The same software anywhere. Let the queue drain first. | Ran in the cluster |
| Monthly reports (S3 with Object Lock) | **Yes** | A store with write-once retention and conditional writes. Old copies stay locked in S3 until their date, so copy them over or keep S3 read-only until then. | **Done:** `REPORTS_BUCKET=gs://<bucket>`, retention policy, `if_generation_match=0` |
| Images (ECR) | Yes | Push to the new registry | **Done:** Artifact Registry |
| AWS permissions (IAM) | Yes | Recreated as the new platform's permissions | **Done:** one service account, least privilege |
| Model API (Anthropic) | Not AWS | Unchanged. It is a third party of its own, with its own exit question. | Unchanged |

## Known limits

- **A few seconds of downtime per deploy of the Orchestrator, Bank or Card** (about 12 s measured for the Orchestrator). A case already running waits and is retried. Whatever sends signals should retry on an error.
- **A case a killed worker held continues on another worker from its last saved step** (`app/runner.py` resumes the case's saved state). With a real model, the step that was in progress makes its model calls again. Nothing reaches Card before a person approves, so no action is taken twice.

- **Secrets:** the Secret is made from `.env` by `up.sh`. A shared cluster would use a secrets manager instead, such as AWS Secrets Manager with External Secrets.
- **Images:** on kind, `up.sh` loads the images compose builds. In a cloud they come from ECR or Artifact Registry as `:latest`; a production setup would pin each deploy to an image digest.
- **No dead-letter queue:** a case that errors is dropped from the queue (`services/orchestrator/app/mq/worker.py`), as under compose.
