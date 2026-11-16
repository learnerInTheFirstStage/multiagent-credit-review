# Bank Service

Owner: Allen.

Linked accounts, daily balances and transactions, served as read-only analysis tools. No model runs here.

The Cash Flow Agent that calls these tools lives in `services/orchestrator/app/agents/cash_flow`.

Today: `GET /health` only. Next: the tasks under the "Bank Service + Cash Flow Agent" epic (KAN-5).

Database: `bank`. Local port: 8002. Not routed by the gateway; only the Orchestrator calls it.
