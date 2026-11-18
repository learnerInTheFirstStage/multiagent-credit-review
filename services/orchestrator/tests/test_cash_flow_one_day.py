"""A one-day window (start == end), which the contract and the case graph allow, reaches Bank as asked and
gets data. Bank answered 422 to it before (services/bank tests/test_transactions.py holds the regression
test); this guards the agent's side: nothing here refuses or rewrites the day."""

from __future__ import annotations

from app.agents.cash_flow import cash_flow
from contracts import InvestigateRequest
from tests._bank_standin import BankStandin

DAY = "2026-11-10"


def test_a_one_day_question_is_sent_as_that_day_and_answered():
    bank = BankStandin()
    request = InvestigateRequest(case_id="case_test", customer_id="c_002", question="What moved that day?",
                                 window_start=DAY, window_end=DAY, topic="transactions")
    response = cash_flow(request, model=None, http=bank.client())
    assert bank.params("cash_summary") == [{"start": DAY, "end": DAY}]
    assert bank.params("transactions")[0]["start"] == DAY
    assert response.evidence and response.findings
