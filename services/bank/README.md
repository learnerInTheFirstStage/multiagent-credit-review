# Bank Service

Owner: Allen.

Linked accounts, daily balances and transactions, plus the Cash Flow Agent that answers where a customer's money went. It reports facts with evidence and never recommends a limit action.

Today: `GET /health` only. Next: the tasks under the "Bank Service + Cash Flow Agent" epic (KAN-5).

Database: `bank`. Local port: 8002. Not routed by the gateway; only the Orchestrator calls it.
