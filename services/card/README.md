# Card Service

Owner: James.

Customers, limits, card spend and repayments, served as read-only analysis tools. It also carries out an action after a person approves it. No model runs here.

The Credit Behavior Agent that calls these tools lives in `services/orchestrator/app/agents/credit_behavior`.

Today: `GET /health` only. Next: the tasks under the "Card Service + Credit Behavior Agent" epic (KAN-6).

Database: `card`. Local port: 8003. Not routed by the gateway; only the Orchestrator calls it.
