# demo

The 27 golden-set customers as the real model left them on 2026-11-22: one case each, waiting for the analyst. Load them to show the review app with real reports without running an investigation, and load them again to undo what a demo changed.

```bash
demo/load.sh
```

It deletes the local stack's volumes (`docker compose down -v`: the databases and the kept monthly reports), starts Postgres, loads `cases-2026-11-22.sql.gz` into the Orchestrator's database, and starts the rest with `docker-compose.demo.yml`, which puts all 27 customers on Bank, Card and the Orchestrator. Bank and Card seed themselves from the data files, so a card paused or a limit cut during a demo is back as it was. The default `docker compose up` does not load these cases.

## On Kubernetes

```bash
demo/load-k8s.sh local                             # the kind cluster k8s/up.sh made (context kind-credit-review)
demo/load-k8s.sh aws-k3s                           # k3s on EC2, through k8s/aws/kubeconfig, with connect.sh's tunnel open
demo/load-k8s.sh local --fresh c_011,c_201,c_301   # the same, without these customers' prepared cases
```

It does on the cluster what `load.sh` does on compose, and asks first unless given `--yes`. It pauses KEDA, stops the Orchestrator, the workers, Bank and Card, recreates their three databases as `infra/postgres/init` makes them, and loads the snapshot. It mounts `eval/extra_customers.yaml` and `eval/history_eval.yaml` at `/demo` (ConfigMap `demo-data`) and sets what `docker-compose.demo.yml` sets, on the workers too. Then it starts Bank and Card, the Orchestrator, and the workers. The reports volume and RabbitMQ stay as they are.

- `--fresh` removes the prepared open case of each customer listed: the case, its signals, its saved investigation state and any wait for Bank or Card. A live signal for that customer then opens a fresh investigation, which calls the model. Their past reviews stay.
- The Auth0 users see only the customers in their `app_metadata.customers`. List there the ones the demo shows.
- A later `k8s/up.sh` keeps all this: `kubectl apply` leaves the settings the loader patched in, and the restart leaves the databases as they are. What a demo changed stays changed until the loader runs again.
- A loader stopped halfway leaves those four stopped and KEDA paused. Run it again.

## What you need

- A model in `.env` (`LLM_MODEL`, `LLM_API_KEY`). Opening a case's report and deciding it call no model, but the Orchestrator reads a case's saved state, and takes decisions, only while a model is set: without one, the queue lists the cases and their pages show no report, and a decision answers 503. A new signal does run the model, about $0.43 a case.
- Sign-in as `.env` sets it. A person sees the cases of the customers their token lists: the dev login lists `c_001` to `c_013`, the Auth0 analyst and manager list `c_001` to `c_012`, `c_201` and `c_301`, and the Auth0 admin lists all 27. To show the other cases, add their ids to the user's `app_metadata.customers` (`services/auth/README.md`).
- One open case per customer: a new signal for any of the 27 joins its case until that case is closed. Approving a request for documents, or a pause or a cut over half by an analyst, leaves the case open for the next step.

## What is in it

The run is reported in `eval/golden-set-report-2026-11-22.md`: recommendation right on 24 of 27, cause on 23. The snapshot holds the cases, their signals, the investigation state the approvals resume from, and the 15 seeded past reviews. It holds no keys and no person's data; every customer is simulated.

The snapshot matches `main` on 2026-11-22: the Orchestrator's tables and the graph's steps and state. A change to either needs a new run and a new snapshot. `load.sh` uses only `docker-compose.yml` and `docker-compose.demo.yml`, not a local `docker-compose.override.yml`.
