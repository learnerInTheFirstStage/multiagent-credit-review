# demo

The 27 golden-set customers as the real model left them on 2026-11-22: one case each, waiting for the analyst. Load them to show the review app with real reports without running an investigation, and load them again to undo what a demo changed.

```bash
demo/load.sh
```

It deletes the local stack's volumes (`docker compose down -v`: the databases and the kept monthly reports), starts Postgres, loads `cases-2026-11-22.sql.gz` into the Orchestrator's database, and starts the rest with `docker-compose.demo.yml`, which puts all 27 customers on Bank, Card and the Orchestrator. Bank and Card seed themselves from the data files, so a card paused or a limit cut during a demo is back as it was. The default `docker compose up` does not load these cases.

## What you need

- A model in `.env` (`LLM_MODEL`, `LLM_API_KEY`). Opening a case's report and deciding it call no model, but the Orchestrator reads a case's saved state, and takes decisions, only while a model is set: without one, the queue lists the cases and their pages show no report, and a decision answers 503. A new signal does run the model, about $0.43 a case.
- Sign-in as `.env` sets it. A person sees the cases of the customers their token lists: the dev login lists `c_001` to `c_013`, and the Auth0 demo users list `c_001` to `c_012`. To show the other cases, add their ids to the user's `app_metadata.customers` (`services/auth/README.md`).
- One open case per customer: a new signal for any of the 27 joins its case until that case is closed. Approving a request for documents, or a pause or a cut over half by an analyst, leaves the case open for the next step.

## What is in it

The run is reported in `eval/golden-set-report-2026-11-22.md`: recommendation right on 24 of 27, cause on 23. The snapshot holds the cases, their signals, the investigation state the approvals resume from, and the 15 seeded past reviews. It holds no keys and no person's data; every customer is simulated.

The snapshot matches `main` on 2026-11-22: the Orchestrator's tables and the graph's steps and state. A change to either needs a new run and a new snapshot. `load.sh` uses only `docker-compose.yml` and `docker-compose.demo.yml`, not a local `docker-compose.override.yml`.
