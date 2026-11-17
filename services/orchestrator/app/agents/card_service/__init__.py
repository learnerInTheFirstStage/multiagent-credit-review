"""Card Service agent (the Credit Behavior specialist). Owner: James.

Answers one question about a customer's card records. Its tools call the
read-only analysis endpoints on the Card Service. It holds no state, reports
facts with evidence and never recommends a limit action.

Public entry point:
    card_service(request: InvestigateRequest) -> InvestigateResponse

The signature matches app/agents/mock.py::credit_behavior so runtime.py can swap
one for the other without the graph noticing (KAN-36). Its answers still say
agent="credit_behavior": that name is the shared contract's (contracts.investigate.AgentName).
"""

from app.agents.card_service.agent import card_service  # noqa: F401
