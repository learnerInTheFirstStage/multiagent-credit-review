"""Code-written findings stay on the topic asked (each fails on the code before the fix).

A preflight may fetch records of another topic's kinds to support its own (paying_account fetches the
repayments, to show which account paid each). Those records are still returned as evidence, but the
finding about them belongs to their own topic: otherwise the same sentence is written under several
topics of one case. Scripted models only, Card answering from tests/fixtures/card_tools.json.
"""

from __future__ import annotations

import pytest

from app.agents.card_service import agent as agent_module
from app.agents.card_service import card_service
from app.graph.causes import KIND_TOPIC
from contracts import InvestigateRequest
from tests._card_service_helpers import RECORDED, CardStub, ScriptedChatModel, answer, recorded, request

CUSTOMERS = sorted({key.split("/")[0] for key in RECORDED})
FAILED = "rep_c_010_202611"


def ask(customer_id: str, topic: str | None, model=None):
    return card_service(InvestigateRequest(**request(customer_id, topic)), model=model,
                        http=CardStub(customer_id).client())


def kinds_of(response) -> dict[str, str]:
    return {e.id: e.kind for e in response.evidence}


def test_the_topic_kinds_agree_with_the_orchestrators_mapping():
    ours = {kind: topic for topic, kinds in agent_module.TOPIC_KINDS.items() for kind in kinds}
    theirs = {kind: topic for kind, (tool, topic) in KIND_TOPIC.items() if tool == "AskCreditBehavior"}
    assert set(agent_module.TOPIC_KINDS) == set(agent_module.TOPICS)
    assert all(agent_module.TOPIC_KINDS.values())
    # Every kind the Orchestrator maps to a card topic is in that topic here; repayment_summary is Card's own.
    assert {k: ours.get(k) for k in theirs} == theirs
    assert ours["repayment_summary"] == "repayments" and set(ours) - set(theirs) == {"repayment_summary"}


def test_the_repayment_counts_are_still_written_under_repayments():
    response = ask("c_010", "repayments")
    assert any("agg_b640a9b7" in f.evidence_ids and f.claim.startswith("Of the last") for f in response.findings)


@pytest.mark.parametrize("model", ["none", "finds_nothing"])
def test_records_of_other_topics_only_say_the_topics_record_did_not_come_back(model):
    body = recorded("c_010", "paying_account")
    body["evidence"] = [e for e in body["evidence"] if e["kind"] == "card_repayment"]
    stub = CardStub("c_010").when("paying_account", body)
    scripted = ScriptedChatModel(script=[answer()]) if model == "finds_nothing" else None
    response = card_service(InvestigateRequest(**request("c_010", "paying_account")), model=scripted,
                            http=stub.client())
    assert response.findings == []
    assert FAILED in kinds_of(response)
    assert any("no paying_account record" in reason for reason in response.could_not_determine)
    assert not any("these findings were written" in reason for reason in response.could_not_determine)


@pytest.mark.parametrize("customer_id", CUSTOMERS)
@pytest.mark.parametrize("topic", agent_module.TOPICS)
@pytest.mark.parametrize("model", ["none", "finds_nothing"])
def test_no_code_written_finding_rests_only_on_another_topics_records(customer_id, topic, model):
    # With no model every finding is code-written; with a model that finds nothing, every one is a hard fact.
    scripted = ScriptedChatModel(script=[answer()]) if model == "finds_nothing" else None
    response = ask(customer_id, topic, scripted)
    kinds = kinds_of(response)
    for finding in response.findings:
        assert any(kinds[i] in agent_module.TOPIC_KINDS[topic] for i in finding.evidence_ids), finding.claim


def test_c010_paying_account_does_not_repeat_the_failed_repayment_but_returns_the_repayments():
    response = ask("c_010", "paying_account")
    assert not any("failed" in f.claim for f in response.findings)
    assert all(FAILED not in f.evidence_ids for f in response.findings)
    kinds = kinds_of(response)
    assert kinds[FAILED] == "card_repayment"  # still evidence: the Orchestrator's rules read it
    assert sum(kind == "card_repayment" for kind in kinds.values()) == 3
    assert any(kind == "funding_account_change" for kind in kinds.values())
    assert [f.evidence_ids for f in response.findings] == [["rep_c_010_funding"]]


def test_c010_the_failed_repayment_is_still_a_hard_fact_on_its_own_topic_and_with_no_topic():
    for topic in ("repayments", None):
        response = ask("c_010", topic, ScriptedChatModel(script=[answer()]))
        assert any(f.evidence_ids == [FAILED] and "failed" in f.claim for f in response.findings)


def test_the_fallback_after_a_failed_graph_is_scoped_too(monkeypatch):
    class Broken:
        def invoke(self, *args, **kwargs):
            # Fetch what paying_account fetches, then fail.
            context = kwargs["context"]
            context["tools"].call("card_paying_account", count=3)
            raise RuntimeError("boom")

    monkeypatch.setattr(agent_module, "GRAPH", Broken())
    response = ask("c_010", "paying_account")
    assert FAILED in kinds_of(response)
    assert all(FAILED not in f.evidence_ids for f in response.findings)
    assert response.findings, "the paying-account change is still written"
