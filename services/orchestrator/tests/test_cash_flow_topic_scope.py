"""Code-written findings stay on the topic asked (each fails on the code before the fix).

Two preflights may fetch the same record: transactions (cash_summary) and reconciliation (balance_vs_txn)
both return the daily balance. The record is still returned as evidence on both, but the finding about it
belongs to its own topic, or the same sentence is written under several topics of one case.
Bank answers from tests/fixtures/bank_tools_standin.json; no model, or a scripted one.
"""

from __future__ import annotations

import pytest

from app.agents.cash_flow import agent as agent_module
from app.agents.cash_flow import cash_flow, findings as written, tools as bank_tools
from app.graph.causes import KIND_TOPIC
from tests._bank_standin import BankStandin, recorded
from tests._cash_flow_helpers import ScriptedChatModel
from tests.test_cash_flow_regressions import answer, ask

RESPONSES = recorded()
CUSTOMERS = sorted({key.split("/")[0] for key in RESPONSES})
CASH_FALL = "Linked cash fell"
NOTHING_WENT_TO_IT = "No payment listed for the window went to it."


def payments(customer_id: str) -> list[str]:
    return [key.rsplit("/", 1)[-1] for key in RESPONSES if key.startswith(f"{customer_id}/payee_history/")]


def requests_of(customer_id: str):
    """One request per topic, and for the topics about one record or account, one per record or account."""
    for topic in bank_tools.TOPICS:
        if topic == "payee_history":
            for txn in payments(customer_id):
                yield ask(customer_id, topic, record_id=txn)
        elif topic == "accounts":
            yield ask(customer_id, topic)
            yield ask(customer_id, topic, account_id="acct_elsewhere")
        else:
            yield ask(customer_id, topic)


def test_the_topic_kinds_agree_with_the_orchestrators_mapping():
    ours = {kind: topic for topic, kinds in bank_tools.TOPIC_KINDS.items() for kind in kinds}
    theirs = {kind: topic for kind, (tool, topic) in KIND_TOPIC.items() if tool == "AskCashFlow"}
    assert set(bank_tools.TOPIC_KINDS) == set(bank_tools.TOPICS)
    assert ours == theirs


def on_the_topic(record: dict, request) -> bool:
    """The topic's kinds (the Orchestrator's mapping); for payee_history the payment asked about when Bank
    keeps no history for it because it went to the customer's own account elsewhere or to a new payee; for
    the accounts topic about one account the payments that went to it."""
    if record["kind"] in bank_tools.TOPIC_KINDS[request.topic]:
        return True
    if request.topic == "payee_history":
        return record["id"] == request.record_id and record["data"]["category"] in ("transfer_own_account",
                                                                                    "vendor_new")
    return (request.topic == "accounts" and request.account_id is not None
            and record["data"].get("to_account") == request.account_id)


@pytest.mark.parametrize("customer_id", CUSTOMERS)
@pytest.mark.parametrize("model", ["none", "finds_nothing"])
def test_no_code_written_finding_rests_only_on_another_topics_records(customer_id, model):
    # With no model every finding is code-written; with a model that finds nothing, every one is a hard fact.
    for request in requests_of(customer_id):
        scripted = ScriptedChatModel(script=[answer()]) if model == "finds_nothing" else None
        response = cash_flow(request, model=scripted, http=BankStandin().client())
        records = {e.id: e.model_dump(mode="json") for e in response.evidence}
        for finding in response.findings:
            assert any(on_the_topic(records[i], request) for i in finding.evidence_ids), (request.topic,
                                                                                          finding.claim)
        # And not vacuously: every record on the topic that code writes about is cited (with no model), every
        # hard fact on it (with a model that finds nothing). A payee never paid before is such a hard fact.
        cited = {i for finding in response.findings for i in finding.evidence_ids}
        for record in records.values():
            expected = (written.is_hard_fact(record) if model == "finds_nothing"
                        else written.write(record, records) is not None)
            if on_the_topic(record, request) and expected:
                assert record["id"] in cited, (request.topic, request.record_id, record["id"])
        assert not any("about other topics" in reason for reason in response.could_not_determine), request


@pytest.mark.parametrize("model", [None, "finds_nothing"])
def test_c010_reconciliation_does_not_repeat_the_cash_fall_written_under_transactions(model):
    def run(topic):
        scripted = ScriptedChatModel(script=[answer()]) if model else None
        return cash_flow(ask("c_010", topic), model=scripted, http=BankStandin().client())

    transactions, reconciliation = run("transactions"), run("reconciliation")
    if model is None:
        assert any(f.claim.startswith(CASH_FALL) for f in transactions.findings)
    said_before = {f.claim for f in transactions.findings}
    assert not any(f.claim in said_before for f in reconciliation.findings)
    assert not any(f.claim.startswith(CASH_FALL) for f in reconciliation.findings)
    # The daily balance is still returned: the Orchestrator's rules read it.
    assert {e.kind for e in reconciliation.evidence} == {"daily_balance", "balance_reconciliation"}


def test_with_no_topic_every_kind_is_still_written():
    response = cash_flow(ask("c_010", None), model=None, http=BankStandin().client())
    cited = {e.kind for e in response.evidence if any(e.id in f.evidence_ids for f in response.findings)}
    assert {"daily_balance", "balance_reconciliation", "weekly_inflow_series", "linked_account"} <= cited


def with_transfer_to(account: str) -> BankStandin:
    """c_002's transfer to its own account elsewhere, naming the account it went to: Bank's account_reporting
    for that id returns the linked accounts and the transfer."""
    responses = recorded()
    transfer = next(item for item in responses["c_002/transactions"]["evidence"] if item["id"] == "txn_c_002_01")
    transfer["data"]["to_account"] = account
    linked = responses["c_002/account_reporting"]["evidence"]
    responses[f"c_002/account_reporting/{account}"] = {"data": {}, "evidence": [*linked, transfer]}
    return BankStandin(responses)


@pytest.mark.parametrize("model", [None, "finds_nothing"])
def test_the_accounts_topic_about_one_account_states_the_transfer_that_went_to_it(model):
    def run(**focus):
        scripted = ScriptedChatModel(script=[answer()]) if model else None
        return cash_flow(ask("c_002", "accounts", **focus), model=scripted, http=with_transfer_to("acct_x").client())

    about_it = run(account_id="acct_x")
    assert any(f.evidence_ids == ["txn_c_002_01"] and "acct_x" in f.claim for f in about_it.findings)
    assert not any(f.claim == NOTHING_WENT_TO_IT for f in about_it.findings)
    # Asked about another account, the transfer is the transactions topic's finding, not this one's.
    elsewhere = run(account_id="acct_y")
    assert all("txn_c_002_01" not in f.evidence_ids for f in elsewhere.findings)


def test_an_account_that_is_not_linked_and_was_sent_nothing_is_said_to_have_been_sent_nothing():
    # Bank's account_reporting for an id that is not linked returns every payment of the review window to it:
    # none came back, so none went. The preflight no longer fetches transactions, which the line waited for.
    response = cash_flow(ask("c_002", "accounts", account_id="acct_elsewhere"), model=None,
                         http=BankStandin().client())
    claims = [f.claim for f in response.findings]
    assert claims[0] == "acct_elsewhere is not one of the linked accounts."
    assert NOTHING_WENT_TO_IT in claims
    assert next(f for f in response.findings if f.claim == NOTHING_WENT_TO_IT).evidence_ids == ["acct_c002_a"]


@pytest.mark.parametrize("model", [None, "finds_nothing"])
def test_records_of_other_topics_only_say_the_topics_record_did_not_come_back(model):
    responses = recorded()
    responses["c_010/balance_vs_txn"]["evidence"] = [
        e for e in responses["c_010/balance_vs_txn"]["evidence"] if e["kind"] == "daily_balance"]
    scripted = ScriptedChatModel(script=[answer()]) if model else None
    response = cash_flow(ask("c_010", "reconciliation"), model=scripted, http=BankStandin(responses).client())
    assert response.findings == []
    assert "bal_c_010" in {e.id for e in response.evidence}
    assert any("no reconciliation record" in reason for reason in response.could_not_determine)
    assert not any("these findings were written" in reason for reason in response.could_not_determine)


NO_HISTORY = "No earlier payment to this payee is on record."
NO_FALL = "Linked cash did not fall in the window, so there is no fall to share out or reconcile."


@pytest.mark.parametrize("model", [None, "finds_nothing"])
@pytest.mark.parametrize(("customer_id", "txn", "fact"), [
    ("c_008", "txn_c_008_01", "$150,000 was paid to a payee the customer has never paid before."),
    ("c_002", "txn_c_002_01", "$400,000 was transferred to an account at another bank held in the customer's own name."),
])
def test_a_payee_never_paid_before_keeps_its_hard_fact_and_says_bank_has_no_history(model, customer_id, txn, fact):
    # Bank returns only the payment and data {paid_before: false, not_available: [...]}. The payment was taken
    # for another topic's record: the hard fact was lost and the answer said Bank returned nothing on payees.
    scripted = ScriptedChatModel(script=[answer()]) if model else None
    response = cash_flow(ask(customer_id, "payee_history", record_id=txn), model=scripted,
                         http=BankStandin().client())
    assert any(f.claim.startswith(fact) and f.evidence_ids == [txn] for f in response.findings)
    assert NO_HISTORY in response.could_not_determine
    assert not any("about other topics" in reason for reason in response.could_not_determine)


@pytest.mark.parametrize("customer_id", CUSTOMERS)
@pytest.mark.parametrize("model", [None, "finds_nothing"])
def test_a_payees_answer_repeats_only_the_hard_facts_of_the_transactions_answer(customer_id, model):
    # The payment asked about is on the payee topic only for its hard fact: payroll, income or a vendor paid
    # before is the transactions topic's sentence, and the payee's answer is its history, or Bank's reason.
    def run(topic, **focus):
        scripted = ScriptedChatModel(script=[answer()]) if model else None
        return cash_flow(ask(customer_id, topic, **focus), model=scripted, http=BankStandin().client())

    transactions = run("transactions")
    for txn in payments(customer_id):
        payee = run("payee_history", record_id=txn)
        records = {e.id: e.model_dump(mode="json") for e in payee.evidence}
        for finding in payee.findings:
            if finding.claim in {f.claim for f in transactions.findings}:
                assert all(written.is_hard_fact(records[i]) for i in finding.evidence_ids), (txn, finding.claim)


@pytest.mark.parametrize(("txn", "said"), [("txn_c_012_01", NO_HISTORY),
                                           ("txn_c_003_01", "It is money coming in, not a payment.")])
def test_a_payment_with_no_history_and_no_hard_fact_gets_banks_reason_alone(txn, said):
    response = cash_flow(ask(txn[4:9], "payee_history", record_id=txn), model=None, http=BankStandin().client())
    assert (response.findings, response.could_not_determine) == ([], [said])


def test_paid_before_false_without_bank_saying_why_is_still_said():
    responses = recorded()
    del responses["c_008/payee_history/txn_c_008_01"]["data"]["not_available"]
    response = cash_flow(ask("c_008", "payee_history", record_id="txn_c_008_01"), model=None,
                         http=BankStandin(responses).client())
    assert "The Bank Service has no earlier payment to this payee on record." in response.could_not_determine


@pytest.mark.parametrize("model", [None, "finds_nothing"])
@pytest.mark.parametrize("kinds", [("daily_balance",), ()], ids=["daily_balance_only", "nothing"])
def test_a_window_where_cash_did_not_fall_says_what_bank_said(model, kinds):
    # Bank's balance_vs_txn has no reconciliation when cash did not fall, and says so in data.not_available.
    # The answer said it returned only other topics' records, or with nothing back, gave no reason.
    responses = recorded()
    body = responses["c_010/balance_vs_txn"]
    body["evidence"] = [e for e in body["evidence"] if e["kind"] in kinds]
    body["data"]["not_available"] = [NO_FALL]
    scripted = ScriptedChatModel(script=[answer()]) if model else None
    response = cash_flow(ask("c_010", "reconciliation"), model=scripted, http=BankStandin(responses).client())
    assert response.findings == []
    assert response.could_not_determine == [NO_FALL]


def test_the_fallback_after_a_failed_graph_says_what_bank_said_about_the_topics_own_calls(monkeypatch):
    class Broken:
        def invoke(self, *args, **kwargs):
            tools = kwargs["context"]["tools_by_name"]
            tools["payee_history"].invoke({"record_id": "txn_c_008_01"})  # what the topic's preflight asks
            tools["prior_year_compare"].invoke({})  # a call the model chose: Bank has no prior year for c_008
            raise RuntimeError("boom")

    monkeypatch.setattr(agent_module, "_GRAPH", Broken())
    response = cash_flow(ask("c_008", "payee_history", record_id="txn_c_008_01"), model=None,
                         http=BankStandin().client())
    assert any(f.evidence_ids == ["txn_c_008_01"] for f in response.findings)
    assert NO_HISTORY in response.could_not_determine
    assert not any("last year" in reason for reason in response.could_not_determine)


def test_the_fallback_finds_what_bank_said_about_a_windows_call_too(monkeypatch):
    responses = recorded()
    responses["c_010/balance_vs_txn"] |= {"evidence": [], "data": {"not_available": [NO_FALL]}}

    class Broken:
        def invoke(self, *args, **kwargs):
            kwargs["context"]["tools_by_name"]["balance_vs_txn"].invoke({"start": "2026-11-03", "end": "2026-11-10"})
            raise RuntimeError("boom")

    monkeypatch.setattr(agent_module, "_GRAPH", Broken())
    response = cash_flow(ask("c_010", "reconciliation"), model=None, http=BankStandin(responses).client())
    assert NO_FALL in response.could_not_determine


def test_bank_answering_nothing_on_the_topic_still_says_so_as_before():
    bank = BankStandin().when("balance_vs_txn", {"detail": {"error": "unavailable", "message": "down"}},
                              status_code=503)
    response = cash_flow(ask("c_010", "reconciliation"), model=None, http=bank.client())
    assert response.findings == []
    assert any("balance_vs_txn" in reason for reason in response.could_not_determine)


def test_the_fallback_after_a_failed_graph_is_scoped_too(monkeypatch):
    class Broken:
        def invoke(self, *args, **kwargs):
            # Fetch what reconciliation fetches, then fail.
            kwargs["context"]["tools_by_name"]["balance_vs_txn"].invoke({"start": "2026-11-03", "end": "2026-11-10"})
            raise RuntimeError("boom")

    monkeypatch.setattr(agent_module, "_GRAPH", Broken())
    response = cash_flow(ask("c_010", "reconciliation"), model=None, http=BankStandin().client())
    assert "bal_c_010" in {e.id for e in response.evidence}
    assert not any(f.claim.startswith(CASH_FALL) for f in response.findings)
    assert response.findings, "the reconciliation is still written"
