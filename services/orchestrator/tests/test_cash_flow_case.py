"""The Cash Flow agent inside the real case graph (app.graph.build), against the stand-in-shaped Bank stub.

Each case runs the fixed_rules flow (the fixed plan, then what the code gaps name) twice: once with
mock.cash_flow as the bank side and once with the agent. The guideline outcomes must be the same: what was
asked and answered, the gap log, the guidelines whose needs the case's records meet, what they permit and
whether the case is handed over. The agent runs with no model (findings written by code) and with a model
that never finds anything (only the hard facts and the preflight records reach the case).
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from app import guidelines as service_guidelines
from app.agents import mock
from app.agents.cash_flow import cash_flow
from app.graph import facts, needs, plans
from tests._bank_standin import BankStandin
from tests._cash_flow_helpers import ScriptedChatModel
from tests.test_graph import FROM_THE_FACTS, Run, report, signal_of

CASES = ["c_002", "c_003", "c_005", "c_009", "c_011", "c_012"]
# Today's outcome with the stand-in, pinned so that a comparison of two empty runs cannot pass.
MET = {"c_002": {"G-04"}, "c_003": {"G-06", "G-07"}, "c_005": {"G-10a"}, "c_009": {"G-02"}, "c_011": {"G-11"},
       "c_012": {"G-10b", "G-11"}}
NOTHING_FOUND ='{"findings": [], "could_not_determine": [], "suggested_next": null}'


class NeverFinds(ScriptedChatModel):
    """A model that answers every turn with no findings."""

    script: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=NOTHING_FOUND))])


def outcomes(bank_side, customer_id: str) -> dict:
    run = Run([report(**FROM_THE_FACTS), report(**FROM_THE_FACTS)], plan=plans.FIXED_PLAN, follow_gaps=True,
              signal=signal_of(customer_id), guidelines=service_guidelines.load(),
              specialists={"cash_flow": bank_side, "credit_behavior": mock.credit_behavior})
    state, loaded = run.state, service_guidelines.load()
    records, asked = list(facts.ledger(state).values()), state["asked"]
    return {
        "asked": [(entry["tool"], entry.get("topic"), entry.get("record_id"), entry["outcome"]) for entry in asked],
        "gaps": [{k: v for k, v in entry.items() if k != "dropped"} for entry in state["gap_log"]],
        "met": {g["id"] for g in loaded if not needs.unmet(g, records, guidelines=loaded, known=records, asked=asked)},
        "permitted": needs.permitted(loaded, records),
        "hand_over": needs.hand_over(loaded, records, asked),
        "code_gaps": needs.code_gaps(loaded, records, asked),
    }


@pytest.fixture(autouse=True)
def no_model(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)


@pytest.mark.parametrize("customer_id", CASES)
@pytest.mark.parametrize("model", [None, "never_finds"])
def test_the_agent_reaches_the_same_guideline_outcomes_as_the_stand_in(customer_id, model):
    bank = BankStandin()

    def agent(request):
        return cash_flow(request, model=NeverFinds() if model else None, http=bank.client())

    ours, theirs = outcomes(agent, customer_id), outcomes(mock.cash_flow, customer_id)

    assert ours == theirs
    assert all(outcome == "answered" for *_, outcome in ours["asked"])
    assert ours["met"] == MET[customer_id]
    assert bank.requests, "the agent asked the Bank stub"
