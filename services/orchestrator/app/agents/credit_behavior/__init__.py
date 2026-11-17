"""Credit Behavior Agent. Owner: James.

Answers one question about a customer's card records. Its tools call the
read-only analysis endpoints on the Card Service. It holds no state, reports
facts with evidence and never recommends a limit action.

Public entry point:
    credit_behavior(request: InvestigateRequest) -> InvestigateResponse

The signature matches app/agents/mock.py::credit_behavior so runtime.py can swap
one for the other without the graph noticing (KAN-36).
"""

from app.agents.credit_behavior.agent import credit_behavior  # noqa: F401
