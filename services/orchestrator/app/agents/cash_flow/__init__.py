"""Cash Flow Agent. Owner: Allen.

Answers one question about a customer's bank records. Its tools call the
read-only analysis endpoints on the Bank Service. It holds no state, reports
facts with evidence and never recommends a limit action. See README.md.

Public entry point:
    cash_flow(request: InvestigateRequest) -> InvestigateResponse

The signature matches app/agents/mock.py::cash_flow so runtime.py can swap one
for the other without the graph noticing (KAN-36).
"""

from app.agents.cash_flow.agent import cash_flow  # noqa: F401
