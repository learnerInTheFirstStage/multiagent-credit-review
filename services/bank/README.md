# Bank Service

Owner: Allen.

Linked accounts, daily balances and transactions, served as read-only analysis tools. No model runs here.

The Cash Flow Agent that calls these tools lives in `services/orchestrator/app/agents/cash_flow`.

Today: `GET /health` is the only endpoint. Every start creates six tables (customers, counterparties, linked_accounts, daily_balances, bank_transactions, prior_year_weekly) and, when the database is empty, seeds the 12 customers from `data/shared_customers.yaml` (`SHARED_CUSTOMERS_YAML` overrides the path). After changing that file, run `docker compose down -v` and `docker compose up --build`, since the file is copied into the image. To seed an empty database by hand: `docker compose exec bank python -m app.db.seed`. Tests: `services/bank/tests`. Nothing calls Bank yet: the Orchestrator's stand-in Cash Flow specialist reads the YAML file directly.

Next: the tasks under the "Bank Service + Cash Flow Agent" epic (KAN-5). The topics the Cash Flow Agent must answer are listed in `services/orchestrator/README.md` (Agents). The evidence kinds are `KIND_TOPIC` in `services/orchestrator/app/graph/causes.py`, and `services/orchestrator/app/agents/mock.py` returns every kind and key the checks read.

Database: `bank`. Local port: 8002. Not routed by the gateway; only the Orchestrator calls it.
