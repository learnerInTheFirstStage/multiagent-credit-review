"""The Bank fixture against the stand-in, and the Cash Flow agent (no model) against mock.cash_flow.

The agent with no model answers from the records the topic's preflight fetched. For every customer and
topic it must return at least the records the stand-in returns, with the same content: the case graph
rejects an answer whose record differs from one the case already holds (build._usable), and its rules and
gaps read the records.
"""

from __future__ import annotations

import pytest

from app.agents import mock
from app.agents.cash_flow import cash_flow
from app.agents.cash_flow import guardrails
from app.graph import figures
from contracts import InvestigateRequest
from tests import _bank_standin
from tests._bank_standin import BankStandin

CUSTOMERS = sorted(mock.customer_ids())
RECORDED = _bank_standin.recorded()


def test_the_fixture_holds_exactly_the_stand_ins_records():
    # The fixture is recorded from the real Bank: python -m tests._bank_standin --from-bank <url>
    # Every key counts: the stand-in gives a payment's date and the weekly series as Bank's seeder writes them.
    for customer_id in CUSTOMERS:
        ours = {item["id"]: item for key, body in RECORDED.items()
                if key.split("/")[0] == customer_id for item in body["evidence"]}
        theirs = {item["id"]: item for key, body in _bank_standin.build().items()
                  if key.split("/")[0] == customer_id for item in body["evidence"]}
        assert ours == theirs, customer_id


def asks(customer_id: str) -> list[tuple[str | None, dict]]:
    topics = [(topic, {}) for topic in (*mock.BANK_TOPICS, None) if topic != "payee_history"]
    payees = [("payee_history", {"record_id": key.rsplit("/", 1)[1]})
              for key in RECORDED if key.startswith(f"{customer_id}/payee_history/")]
    return [*topics, *payees, ("accounts", {"account_id": "acct_not_linked"})]


def request(customer_id: str, topic: str | None, **focus) -> InvestigateRequest:
    return InvestigateRequest(case_id="case_test", customer_id=customer_id, question="What does the bank side show?",
                              window_start="2026-11-03", window_end="2026-11-10", topic=topic, **focus)


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)


@pytest.mark.parametrize("customer_id", CUSTOMERS)
def test_with_no_model_the_agent_returns_every_record_the_stand_in_returns_unchanged(customer_id):
    for topic, focus in asks(customer_id):
        ask = request(customer_id, topic, **focus)
        theirs = mock.cash_flow(ask)
        ours = {item.id: item.model_dump(mode="json") for item in cash_flow(ask, http=BankStandin().client()).evidence}
        if topic is None:
            # A payee's history needs a payment's id; with no topic the agent does not look one up.
            theirs.evidence = [item for item in theirs.evidence if item.kind != "payee_history"]
        for item in theirs.evidence:
            assert item.id in ours, (topic, focus, item.id)
            assert ours[item.id] == item.model_dump(mode="json"), (topic, focus, item.id)


@pytest.mark.parametrize("customer_id", CUSTOMERS)
def test_every_finding_code_writes_passes_its_own_checks(customer_id):
    for topic, focus in asks(customer_id):
        response = cash_flow(request(customer_id, topic, **focus), http=BankStandin().client())
        records = {item.id: item.model_dump(mode="json") for item in response.evidence}
        checked = guardrails.check([finding.model_dump(mode="json") for finding in response.findings], records)
        assert checked.problems == [], (topic, focus)
        for finding in response.findings:
            cited = [records[i] for i in finding.evidence_ids]
            assert figures.not_backed(finding.claim, figures.allowed_figures(cited)) == [], finding.claim
