"""Regression tests for the Card Service agent's review fixes: each fails on the code before its fix.

Scripted and fake models only, Card answering from tests/fixtures/card_tools.json.
"""

from __future__ import annotations

import json
from typing import TypedDict

import httpx
import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.graph import START, StateGraph

from app.agents.card_service import agent as agent_module
from app.agents.card_service import card_service, guardrails
from app.agents.card_service import findings as written
from app.graph import build, figures
from contracts import InvestigateRequest, InvestigateResponse
from tests._card_service_helpers import CardStub, ScriptedChatModel, answer, recorded, request

FAILED = "rep_c_010_202611"
BALANCE = "bal_card_c_010_20261110"
SUMMARY = "agg_b640a9b7"


def run(customer_id: str, script, topic: str | None = None, stub: CardStub | None = None, model=None, **extra):
    stub = stub or CardStub(customer_id)
    if model is None and script is not None:
        model = ScriptedChatModel(script=script)
    response = card_service(InvestigateRequest(**request(customer_id, topic, **extra)), model=model, http=stub.client())
    InvestigateResponse.model_validate(response.model_dump(mode="json"))
    build._usable(response, known={})
    return response, stub, model


def cited(response) -> set[str]:
    return {i for f in response.findings for i in f.evidence_ids}


def claims(response) -> list[str]:
    return [f.claim for f in response.findings]


# --- S1: the agent answers, it never raises ------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {"findings": 5, "could_not_determine": [], "suggested_next": None},
    {"findings": [], "could_not_determine": 7, "suggested_next": None},
    {"findings": "none", "could_not_determine": {"a": 1}, "suggested_next": None},
])
def test_s1_a_malformed_answer_does_not_raise(payload):
    response, _, _ = run("c_010", [payload], topic="repayments")
    assert FAILED in cited(response)  # the hard fact still reaches the Orchestrator
    assert any("not in the shape asked for" in reason for reason in response.could_not_determine)


def test_s1_a_blank_claim_is_left_out_not_raised():
    response, _, _ = run("c_010", [answer(("   ", [BALANCE])), answer(("  \n ", [BALANCE]))], topic="card_use")
    assert all(f.claim.strip() for f in response.findings)
    assert any("empty" in reason for reason in response.could_not_determine)


def test_s1_a_failure_inside_the_graph_falls_back_to_code_written_findings(monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(guardrails, "check", broken)
    response, stub, _ = run("c_010", [answer(("Utilization is spiking.", [BALANCE]))])
    assert response.agent == "credit_behavior"
    assert FAILED in cited(response) and BALANCE in cited(response)
    assert any("written by code from the card records" in reason for reason in response.could_not_determine)
    # No second round of Card calls: the fallback uses what was fetched.
    assert len(stub.requests) == 4


def test_s1_a_fallback_that_cannot_write_findings_still_answers(monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(guardrails, "check", broken)
    monkeypatch.setattr(written, "from_records", broken)
    response, _, _ = run("c_010", [answer()], topic="repayments")
    # Only the hard facts, still written by code; the rest of the records as evidence.
    assert cited(response) == {FAILED}
    assert {"rep_c_010_202609", FAILED, SUMMARY} <= {e.id for e in response.evidence}
    assert any("could not write findings" in reason for reason in response.could_not_determine)


def test_s1_a_fallback_with_no_records_still_answers(monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(written, "hard_facts", broken)
    response, _, _ = run("c_010", [answer()], topic="repayments")
    assert response.findings == [] and response.could_not_determine


# --- S2: a model that repeats a call forever is stopped -------------------------------------------


class Repeater(BaseChatModel):
    """Calls the same tool on every turn, until told in a HumanMessage to answer."""

    turns: int = 0

    @property
    def _llm_type(self) -> str:
        return "repeater-fake"

    def bind_tools(self, tools, **kwargs):  # noqa: ARG002
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:  # noqa: ARG002
        if isinstance(messages[-1], HumanMessage) and len(messages) > 2:
            content = json.dumps(answer(("The repayment due 2026-11-05 for $50,000 failed.", [FAILED])))
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])
        self.turns += 1
        call = {"name": "card_repayments", "args": {"count": 3}, "id": f"call_{self.turns}", "type": "tool_call"}
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[call]))])


def test_s2_a_model_that_repeats_a_call_is_capped_and_still_answers():
    model = Repeater()
    response, stub, _ = run("c_010", None, topic="repayments", model=model)
    assert model.turns <= 8
    assert "The repayment due 2026-11-05 for $50,000 failed." in claims(response)
    assert stub.tools_called() == ["repayments"]  # every repeat came from the cache
    assert any("tool rounds" in reason for reason in response.could_not_determine)


# --- S3: a hard fact is always stated by code ------------------------------------------------------


def test_s3_a_wrong_claim_citing_a_failed_repayment_does_not_hide_it():
    response, _, _ = run("c_010", [answer(("All recent repayments were paid on time.", [FAILED]))], topic="repayments")
    stated = [f.claim for f in response.findings if FAILED in f.evidence_ids]
    assert any("failed" in claim and "$50,000" in claim for claim in stated)


def test_s3_the_code_written_hard_fact_is_not_added_twice():
    fact = written.repayment(guardrails_record("c_010", "repayments", FAILED)).claim
    response, _, _ = run("c_010", [answer((fact, [FAILED]))], topic="repayments")
    assert claims(response).count(fact) == 1


def guardrails_record(customer: str, tool: str, record_id: str):
    from contracts import Evidence

    return Evidence.model_validate(next(e for e in recorded(customer, tool)["evidence"] if e["id"] == record_id))


# --- S4: the evidence holds every record preflight fetched ----------------------------------------


def test_s4_evidence_holds_every_preflight_record_not_only_the_cited_ones():
    response, _, _ = run("c_010", [answer(("The repayment due 2026-11-05 for $50,000 failed.", [FAILED]))],
                         topic="repayments")
    ids = [e.id for e in response.evidence]
    assert {"rep_c_010_202609", "rep_c_010_202610", FAILED, SUMMARY} <= set(ids)
    assert len(ids) == len(set(ids))
    # Exactly as Card returned them.
    card = {e["id"]: e for e in recorded("c_010", "repayments")["evidence"]}
    assert all(e.model_dump(mode="json") == card[e.id] for e in response.evidence)


def test_s4_two_answers_in_one_case_agree_on_shared_records():
    first, _, _ = run("c_006", [answer()], topic="repayments")
    second, _, _ = run("c_006", [answer()], topic="paying_account")
    ledger = {e.id: e.model_dump(mode="json") for e in first.evidence}
    assert set(ledger) & {e.id for e in second.evidence}  # the repayments compared come back again
    build._usable(second, known=ledger)


# --- S6: the deadline holds in every step, and Card calls end before it ---------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_s6_card_calls_time_out_before_the_deadline(monkeypatch):
    monkeypatch.setattr(agent_module, "MAX_SECONDS", 3)
    seen: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.extensions["timeout"])
        return httpx.Response(200, json=recorded("c_010", "repayments"))

    stub = CardStub("c_010").when("repayments", handler=handler)
    run("c_010", [answer()], topic="repayments", stub=stub)
    assert seen and all(t["read"] <= 3 for t in seen)


def test_s6_preflight_stops_calling_card_after_the_deadline(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(agent_module, "_now", clock, raising=False)

    def slow(req: httpx.Request) -> httpx.Response:
        clock.now += 1000
        return httpx.Response(200, json=recorded("c_010", "utilization"))

    stub = CardStub("c_010").when("utilization", handler=slow)
    response, stub, model = run("c_010", [answer()], stub=stub)
    assert stub.tools_called() == ["utilization"]
    assert model.step == 0
    assert any("60-second budget" in reason for reason in response.could_not_determine)


def test_s6_tools_stop_calling_card_after_the_deadline_and_summarise_is_skipped(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(agent_module, "_now", clock, raising=False)

    def slow(req: httpx.Request) -> httpx.Response:
        clock.now += 1000
        return httpx.Response(200, json=recorded("c_003", "transactions"))

    stub = CardStub("c_010").when("transactions", handler=slow)
    response, stub, model = run("c_010", [
        [("card_transactions", {"start": "2026-11-01", "end": "2026-11-10"}),
         ("card_spend_breakdown", {"start": "2026-11-01", "end": "2026-11-10"})],
        answer(("Too late to be read.", [BALANCE])),
    ], topic="repayments", stub=stub)
    assert stub.tools_called() == ["repayments", "transactions"]
    assert model.step == 1  # neither investigate nor summarise called the model again
    assert FAILED in cited(response)


# --- tool spans: Card calls run as LangChain tool runs ---------------------------------------------


class ToolRuns(BaseCallbackHandler):
    def __init__(self) -> None:
        self.started: list[str] = []
        self.ended = 0

    def on_tool_start(self, serialized, input_str, **kwargs) -> None:  # noqa: ANN001
        self.started.append(kwargs.get("name") or (serialized or {}).get("name"))

    def on_tool_end(self, output, **kwargs) -> None:  # noqa: ANN001
        self.ended += 1


def test_tool_calls_reach_callbacks_as_tool_runs():
    handler = ToolRuns()
    model = ScriptedChatModel(script=[
        [("card_transactions", {"start": "2026-11-01", "end": "2026-11-10"})],
        answer(),
    ])
    stub = CardStub("c_003")

    class Outer(TypedDict, total=False):
        done: bool

    def ask(_state: Outer) -> Outer:
        card_service(InvestigateRequest(**request("c_003", "repayments")), model=model, http=stub.client())
        return {"done": True}

    outer = StateGraph(Outer)
    outer.add_node("ask_specialist", ask)
    outer.add_edge(START, "ask_specialist")
    outer.compile().invoke({}, config={"callbacks": [handler]})
    assert handler.started == ["card_repayments", "card_transactions"]
    assert handler.ended == 2
    assert stub.tools_called() == ["repayments", "transactions"]


def test_bad_arguments_and_unknown_tools_are_answered_as_before():
    model = ScriptedChatModel(script=[
        [("card_repayments", {"count": 99}), ("no_such_tool", {})],
        answer(),
    ])
    response, stub, _ = run("c_010", None, topic="repayments", model=model)
    tool_messages = [m for m in model_messages(model) if isinstance(m, ToolMessage)]
    bodies = [json.loads(m.content) for m in tool_messages]
    assert [b.get("error") for b in bodies] == ["invalid_arguments", "unknown_tool"]
    assert stub.tools_called() == ["repayments"]


def model_messages(model):
    return model._seen


@pytest.fixture(autouse=True)
def _record_messages(monkeypatch):
    original = ScriptedChatModel._generate

    def _generate(self, messages, *args, **kwargs):
        object.__setattr__(self, "_seen", list(messages))
        return original(self, messages, *args, **kwargs)

    monkeypatch.setattr(ScriptedChatModel, "_generate", _generate)


# --- S7: no limit action, in any form ------------------------------------------------------------


@pytest.mark.parametrize("claim", [
    "Lowering the credit limit is warranted.",
    "A limit reduction is warranted given the failed repayment.",
    "The card ought to be frozen until the repayment clears.",
    "Reducing the limit would be prudent.",
])
def test_s7_noun_and_gerund_actions_are_dropped(claim):
    response, _, _ = run("c_010", [answer((claim, [BALANCE])), answer((claim, [BALANCE]))], topic="card_use")
    assert claim not in claims(response)


@pytest.mark.parametrize("claim", [
    "The limit is $100,000.",
    "The card was paused on 2026-10-01 and reopened on 2026-10-03.",
    "The card balance was $92,000 against the $100,000 limit.",
])
def test_s7_neutral_facts_still_pass(claim):
    assert not guardrails.recommends_an_action(claim)
    response, _, _ = run("c_010", [answer((claim, [BALANCE]))], topic="card_use")
    assert claim in claims(response)


def test_s7_could_not_determine_from_the_model_is_screened():
    response, _, _ = run("c_010", [answer(could_not_determine=["Analyst should reduce_limit to $50,000 now.",
                                                                "Whether the customer has other cards."])],
                         topic="repayments")
    assert not any("reduce_limit" in reason for reason in response.could_not_determine)
    assert "Whether the customer has other cards." in response.could_not_determine
    assert any("recommended an action" in reason for reason in response.could_not_determine)


@pytest.mark.parametrize("text", [
    "Whether the balance is close to the card limit for long.",
    "Whether spend suggests the customer is increasingly relying on the card.",
    "The repayment must have been made after the card limit was lowered.",
])
def test_s7_neutral_gaps_are_kept(text):
    assert not guardrails.recommends_an_action(text)
    response, _, _ = run("c_010", [answer(could_not_determine=[text])], topic="repayments")
    assert text in response.could_not_determine


# --- S8: figures the graph reads, the agent reads too ---------------------------------------------


@pytest.mark.parametrize("claim", [
    "The card balance was USD 77,000.",
    "61 percent of the limit is in use.",
    "Card use is 3x what it was.",
    "The card balance was US$999,999.",
])
def test_s8_figures_written_other_ways_are_checked(claim):
    response, _, _ = run("c_010", [answer((claim, [BALANCE])), answer((claim, [BALANCE]))], topic="card_use")
    assert claim not in claims(response)
    assert any("do not hold" in reason for reason in response.could_not_determine)


@pytest.mark.parametrize("customer", ["c_001", "c_003", "c_006", "c_010"])
def test_s8_code_written_findings_pass_the_graphs_figure_check(customer):
    response, _, _ = run(customer, None)
    ledger = {e.id: e.model_dump(mode="json") for e in response.evidence}
    for finding in response.findings:
        allowed = figures.allowed_figures([ledger[i] for i in finding.evidence_ids])
        assert figures.not_backed(finding.claim, allowed) == [], finding.claim


# --- S9: too_short_to_tell ------------------------------------------------------------------------


def _too_short_stub() -> CardStub:
    utilization = recorded("c_010", "utilization")
    record = next(e for e in utilization["evidence"] if e["id"] == BALANCE)
    record["data"]["utilization_trend"] = "too_short_to_tell"
    record["data"].pop("usual_share_bp")
    return CardStub("c_010").when("utilization", utilization)


def test_s9_saying_the_history_is_too_short_is_accepted():
    claim = "The card history is too short to tell what is usual for this customer."
    response, _, _ = run("c_010", [answer((claim, [BALANCE]))], topic="card_use", stub=_too_short_stub())
    assert claim in claims(response)


def test_s9_nothing_out_of_the_ordinary_is_dropped():
    claim = "Card use shows nothing out of the ordinary."
    response, _, _ = run("c_010", [answer((claim, [BALANCE])), answer((claim, [BALANCE]))],
                         topic="card_use", stub=_too_short_stub())
    assert claim not in claims(response)


def test_s9_applies_when_another_record_in_the_pool_is_too_short():
    claim = "Repayments look normal for this customer."
    rep = "rep_c_010_202610"
    response, _, _ = run("c_010", [answer((claim, [rep])), answer((claim, [rep]))], stub=_too_short_stub())
    assert claim not in claims(response)


def test_s9_a_repayment_fact_is_not_caught_by_a_short_balance_history():
    claim = "The two repayments before it were paid on time, as expected from autopay."
    rep = "rep_c_010_202610"
    response, _, _ = run("c_010", [answer((claim, [rep]))], stub=_too_short_stub())
    assert claim in claims(response)


# --- a model that gives no answer still leaves the code-written findings -------------------------


def test_a_failed_model_call_answers_from_the_records():
    response, _, _ = run("c_010", [], topic="card_use")  # the scripted model raises on its first call
    assert BALANCE in cited(response)
    assert any("model call failed" in reason for reason in response.could_not_determine)
    assert any("written by code from the card records" in reason for reason in response.could_not_determine)


# --- Z: a balance with no share --------------------------------------------------------------------


def test_z_a_balance_with_no_share_is_written_without_one():
    utilization = recorded("c_010", "utilization")
    record = next(e for e in utilization["evidence"] if e["id"] == BALANCE)
    record["data"]["credit_limit_cents"] = 0
    record["data"].pop("used_share_bp")
    response, _, _ = run("c_010", None, topic="card_use", stub=CardStub("c_010").when("utilization", utilization))
    balance = next(f for f in response.findings if BALANCE in f.evidence_ids)
    assert "$92,000" in balance.claim and "%" not in balance.claim.split(".")[0]


# --- the paying-account record in Card's new shape: {changed, from, to, changed_on?} only ---------


def test_an_unchanged_paying_account_in_the_new_shape_names_no_count():
    from contracts import Evidence

    record = Evidence(id="rep_c_001_funding", kind="funding_account_change",
                      data={"changed": False, "from": "acct_c001_a", "to": "acct_c001_a"})
    finding = written.paying_account(record)
    assert "acct_c001_a" in finding.claim and "did not change" in finding.claim
    assert "None" not in finding.claim
    assert figures.not_backed(finding.claim, figures.allowed_figures([record.model_dump(mode="json")])) == []


def test_a_changed_paying_account_in_the_new_shape():
    from contracts import Evidence

    record = Evidence(id="rep_c_006_funding", kind="funding_account_change",
                      data={"changed": True, "from": "acct_c006_a", "to": "acct_external_new_c006",
                            "changed_on": "2026-10-20"})
    claim = written.paying_account(record).claim
    assert claim == "The account that pays the card changed from acct_c006_a to acct_external_new_c006 on 2026-10-20."


# --- repair keeps what the first check accepted ----------------------------------------------------


def test_repair_keeps_findings_the_first_check_accepted():
    good = "On 2026-11-10 the card balance was $92,000."
    response, _, model = run("c_010", [
        answer((good, [BALANCE]), ("Utilization is spiking.", ["bal_card_c_010_made_up"])),
        answer(("Utilization is spiking.", ["bal_card_c_010_also_made_up"])),
    ], topic="card_use")
    assert model.step == 2
    assert good in claims(response)
