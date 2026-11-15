# Card Service

Owner: James.

Customers, limits, card spend and repayments, plus the Credit Behavior Agent that answers whether a customer is behaving normally. It also carries out an action after a person approves it.

Today: `GET /health` only. Next: the tasks under the "Card Service + Credit Behavior Agent" epic (KAN-6).

Database: `card`. Local port: 8003. Not routed by the gateway; only the Orchestrator calls it.
