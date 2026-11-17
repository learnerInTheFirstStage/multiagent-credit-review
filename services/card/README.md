# Card Service

Owner: James.

Customers, limits, card spend and repayments, served as read-only analysis tools. It also carries out an action after a person approves it. No model runs here.

The Credit Behavior Agent that calls these tools lives in `services/orchestrator/app/agents/credit_behavior`.

Today: `GET /health` only. Next: the tasks under the "Card Service + Credit Behavior Agent" epic (KAN-6). The topics the Credit Behavior Agent must answer are listed in `services/orchestrator/README.md` (Agents). The evidence kinds are `KIND_TOPIC` in `services/orchestrator/app/graph/causes.py`, and `services/orchestrator/app/agents/mock.py` returns every kind and key the checks read.

The Orchestrator already calls the action endpoint, so until it exists Card answers 404 and the Orchestrator answers every approved reduction or pause with a 502. What it sends (a proposal, `services/orchestrator/app/card_client.py`):

- `POST /customers/{id}/actions` with the approver's own bearer token.
- Body: `{"type": "reduce_limit", "limit_cents": <the new limit, the engine's limit from the signal>}` or `{"type": "pause_card"}`, plus `case_id`, `customer_id`, `approved_by` and `approval_id` (`<case_id>:analyst` or `<case_id>:manager`, the same on a retry).
- It expects a 2xx answer with a JSON body. Anything else, or no answer within 10 seconds, counts as not carried out.
- Card is expected to check the approver's role again and apply each `approval_id` once. A `pause_card`, or a cut of more than half the current limit, comes with a manager's token and `<case_id>:manager`; any other reduction with an analyst's or a manager's token and `<case_id>:analyst`. `docker-compose.yml` does not pass `JWT_SECRET` to Card yet.

Database: `card`. Local port: 8003. Not routed by the gateway; only the Orchestrator calls it.
