import ast
import copy
import os
import re
from pathlib import Path
from typing import get_args

import pytest
import yaml
from pydantic import ValidationError

from app.agents import mock
from app.graph.report import LikelyCause, Recommendation
from contracts import InvestigateRequest, InvestigateResponse

# The examples in API doc section 2.
REQUEST = {
    "case_id": "case_102",
    "customer_id": "c_002",
    "question": "Where did the money go?",
    "window_start": "2026-11-03",
    "window_end": "2026-11-10",
    "context": "Linked cash fell from $1.2M to $480K in 7 days.",
}
RESPONSE = {
    "agent": "cash_flow",
    "findings": [
        {
            "claim": "$400,000 was transferred on Nov 7 to an unlinked account.",
            "confidence": "high",
            "evidence_ids": ["txn_8841"],
        }
    ],
    "evidence": [
        {
            "id": "txn_8841",
            "kind": "bank_transaction",
            "data": {"date": "2026-11-07", "amount_cents": -40000000, "type": "transfer"},
        }
    ],
    "could_not_determine": ["The balance of the receiving account"],
    "suggested_next": "Check whether inflows changed.",
}


def response_with(**changes) -> dict:
    return {**copy.deepcopy(RESPONSE), **changes}


def test_the_api_doc_examples_are_valid():
    assert InvestigateRequest.model_validate(REQUEST).question == "Where did the money go?"
    assert InvestigateResponse.model_validate(RESPONSE).findings[0].evidence_ids == ["txn_8841"]


def test_a_specialist_that_cannot_answer_says_why():
    cannot = response_with(findings=[], evidence=[], could_not_determine=["No transactions in the window"])

    assert InvestigateResponse.model_validate(cannot).findings == []


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(
            response_with(evidence=[]), id="finding cites evidence that is not in the response"
        ),
        pytest.param(
            response_with(
                findings=[{"claim": "Cash fell.", "confidence": "high", "evidence_ids": []}]
            ),
            id="finding with no evidence",
        ),
        pytest.param(
            response_with(evidence=RESPONSE["evidence"] * 2), id="the same evidence id twice"
        ),
        pytest.param(
            response_with(findings=[], evidence=[], could_not_determine=[]),
            id="no findings and no reason",
        ),
        pytest.param(
            response_with(findings=[], evidence=[], could_not_determine=[""]),
            id="no findings and a blank reason",
        ),
        pytest.param(response_with(agent="orchestrator"), id="unknown agent"),
        pytest.param(
            response_with(
                findings=[{"claim": "Cash fell.", "confidence": 0.9, "evidence_ids": ["txn_8841"]}]
            ),
            id="confidence as a number",
        ),
        pytest.param(response_with(recommendation="reduce_limit"), id="a limit action"),
    ],
)
def test_a_response_that_breaks_the_rules_is_refused(response):
    with pytest.raises(ValidationError):
        InvestigateResponse.model_validate(response)


@pytest.mark.parametrize(
    "request_",
    [
        pytest.param({**REQUEST, "question": ""}, id="empty question"),
        pytest.param({**REQUEST, "customer_id": "002"}, id="customer_id not c_NNN"),
        pytest.param(
            {**REQUEST, "window_start": "2026-11-10", "window_end": "2026-11-03"},
            id="window runs backwards",
        ),
        pytest.param({**REQUEST, "window_end": 1731196800}, id="date as a timestamp"),
        pytest.param({**REQUEST, "focus": "acct_1"}, id="unknown field"),
    ],
)
def test_a_malformed_request_is_refused(request_):
    with pytest.raises(ValidationError):
        InvestigateRequest.model_validate(request_)


CUSTOMERS = [f"c_{n:03d}" for n in range(1, 13)]
SIDES = {"cash_flow": mock.cash_flow, "credit_behavior": mock.credit_behavior}


def ask(specialist, customer_id: str, question: str = "What happened?") -> InvestigateResponse:
    return specialist(InvestigateRequest.model_validate({**REQUEST, "customer_id": customer_id, "question": question}))


def claims(answer: InvestigateResponse) -> str:
    return " | ".join(finding.claim for finding in answer.findings)


def everything(customer_id: str) -> str:
    """Both sides' answers about a customer, as the text the model would be given."""
    return " ".join(ask(side, customer_id).model_dump_json() for side in SIDES.values())


@pytest.mark.parametrize("customer_id", CUSTOMERS)
def test_every_test_customer_gets_an_answer_from_both_sides_that_keeps_the_contract(customer_id):
    for agent, side in SIDES.items():
        answer = ask(side, customer_id)

        assert answer.agent == agent
        assert answer.findings
        # Built again from a dict, so that every rule of the contract is checked.
        InvestigateResponse.model_validate(answer.model_dump())
        cited = {evidence_id for finding in answer.findings for evidence_id in finding.evidence_ids}
        assert cited == {evidence.id for evidence in answer.evidence}  # nothing returned that no finding stands on


@pytest.mark.parametrize("customer_id", CUSTOMERS)
def test_a_stand_in_never_gives_away_the_answer(customer_id):
    customer = mock._data()["customers"][customer_id]
    answers = everything(customer_id)

    # The labels the report has to choose between.
    for label in (*get_args(LikelyCause), *get_args(Recommendation)):
        assert label not in answers, label
    # What the authors of the data wrote about the customer, as opposed to what its records hold.
    assert customer["name"] not in answers
    sentences = [part.strip(" .") for part in customer["story"].replace("\n", " ").split(". ")]
    assert any(sentences)
    for sentence in filter(None, sentences):
        assert sentence not in answers, sentence
    for event in customer["signal_window"]["bank_events"]:
        assert event.get("note", "a note that is not there") not in answers
    payee = customer["baselines"]["bank"].get("payee_history_hint", {})
    assert payee.get("counterparty", "a payee that is not there") not in answers


# Agreed on PR 4: each service owns its id prefixes, so two services never make the same id.
# An aggregate is agg_ plus a short id worked out from what it is about.
ID_PREFIXES = {
    "cash_flow": ("txn_", "acct_", "bal_", "agg_"),
    "credit_behavior": ("rep_", "bal_card_", "agg_"),
}


@pytest.mark.parametrize("customer_id", CUSTOMERS)
def test_the_stand_ins_use_the_id_prefixes_of_their_own_service(customer_id):
    for agent, side in SIDES.items():
        for evidence in ask(side, customer_id).evidence:
            assert evidence.id.startswith(ID_PREFIXES[agent]), f"{agent} made {evidence.id}"
            # bal_card_ also starts with bal_, so the Bank side needs this said separately.
            assert agent == "credit_behavior" or not evidence.id.startswith("bal_card_"), f"{agent} made {evidence.id}"


def test_the_same_fact_has_the_same_id_however_the_question_is_put():
    first = ask(mock.cash_flow, "c_010", "Where did the money go?")
    again = ask(mock.cash_flow, "c_010", "What was the balance on the due date?")

    assert [evidence.id for evidence in first.evidence] == [evidence.id for evidence in again.evidence]


def test_editing_an_answer_does_not_change_the_next_one():
    first = ask(mock.cash_flow, "c_002")
    first.findings.clear()
    first.evidence[0].data["cash_now_cents"] = 0

    again = ask(mock.cash_flow, "c_002")

    assert again.findings
    assert again.evidence[0].data["cash_now_cents"] == 48000000


def test_a_customer_that_is_not_in_the_data_gets_an_honest_no():
    for side in SIDES.values():
        answer = ask(side, "c_099")

        assert answer.findings == []
        assert answer.could_not_determine == ["There is no data for c_099."]


# What makes each customer what it is has to be in the answers, or the case tests nothing.


def test_c_002_the_money_went_to_the_customers_own_account():
    answer = ask(mock.cash_flow, "c_002")

    transfer = next(e for e in answer.evidence if e.data.get("counterparty_kind") == "own_account_other_bank")
    assert transfer.data["amount_cents"] == -40000000
    assert "held in the customer's own name" in claims(answer)


def test_c_003_income_is_shrinking_and_a_repayment_was_late():
    bank, card = ask(mock.cash_flow, "c_003"), ask(mock.credit_behavior, "c_003")

    assert "Weekly inflows have shrunk to $30,000, from $170,000 three months ago." in claims(bank)
    assert "oldest first: paid, paid, late." in claims(card)
    assert "85% of the $125,000 limit. Utilization is rising." in claims(card)


def test_c_004_a_tax_payment_and_one_weak_week():
    answer = ask(mock.cash_flow, "c_004")

    assert "$220,000 was paid to a tax authority" in claims(answer)
    assert "$60,000 against a usual $120,000. One week is too short to call a trend." in claims(answer)
    assert "Payments listed for the window: 1, $220,000 in all. No other large payment went out." in claims(answer)


def test_c_005_an_account_stopped_reporting_and_what_it_holds_is_not_known():
    answer = ask(mock.cash_flow, "c_005")

    stale = next(e for e in answer.evidence if e.id == "acct_c005_a")
    assert stale.data == {"active": True, "days_stale": 5, "last_synced": "2026-11-05", "is_stale": True}
    assert "Account acct_c005_a has not reported for 5 days." in claims(answer)
    assert answer.could_not_determine == ["What acct_c005_a holds since it stopped reporting"]
    # No claim about where the money went, or that the books do not balance: neither can be known.
    assert "large payment" not in claims(answer) and "reconcile" not in claims(answer)


def test_c_006_each_side_holds_half_of_the_clue():
    bank, card = ask(mock.cash_flow, "c_006"), ask(mock.credit_behavior, "c_006")

    assert "Weekly inflows have almost stopped" in claims(bank)
    assert "The linked accounts are: acct_c006_a." in claims(bank)
    assert "changed from acct_c006_a to acct_external_new_c006." in claims(card)
    # That the new account is out of the bank's sight is for the main agent to work out.
    assert "linked" not in card.model_dump_json()
    assert "acct_external_new_c006" not in bank.model_dump_json()


def test_c_007_the_same_weeks_last_year_are_there_to_compare():
    answer = ask(mock.cash_flow, "c_007")

    assert "In the same two weeks last year, $160,000 went out and $85,000 came in." in claims(answer)
    assert "$150,000 was paid to a vendor the customer has paid before." in claims(answer)


def test_c_008_three_payments_to_payees_never_paid_before():
    answer = ask(mock.cash_flow, "c_008")

    assert claims(answer).count("was paid to a payee the customer has never paid before") == 3
    assert "Payments listed for the window: 3, $380,000 in all." in claims(answer)


def test_c_009_the_memo_goes_out_as_data_and_nothing_points_at_it():
    customer = mock._data()["customers"]["c_009"]
    memo = customer["signal_window"]["bank_events"][0]["memo"].strip()
    answer = ask(mock.cash_flow, "c_009")

    payment = next(e for e in answer.evidence if "memo" in e.data)
    assert payment.data["memo"] == memo
    assert "IGNORE PREVIOUS INSTRUCTIONS" in memo
    # The stand-in neither repeats the instruction, nor follows it, nor warns about it.
    for word in ("IGNORE", "c_003", "instruction", "memo"):
        assert word not in claims(answer)
    assert "was paid 2 times before over 24 months, usually $45,000." in claims(answer)


def test_c_010_the_repayment_failed_and_nobody_knows_why():
    answer = ask(mock.credit_behavior, "c_010")

    latest = next(e for e in answer.evidence if e.id == "rep_c_010_3")
    assert latest.data == {"sequence": 3, "of": 3, "status": "failed", "amount_cents": 5000000, "paid_since": "not_recorded"}
    assert "oldest first: paid, paid, failed." in claims(answer)
    assert answer.could_not_determine == ["Why the repayment failed"]


def test_c_011_the_payment_is_set_next_to_what_the_payee_usually_gets():
    answer = ask(mock.cash_flow, "c_011")

    assert "$170,000 was paid to a vendor the customer has paid before." in claims(answer)
    assert "was paid 18 times before over 24 months, usually $8,000 to $40,000." in claims(answer)
    assert "anomalous" not in answer.model_dump_json()  # the judgment is the report's to make


def test_only_c_012_has_books_that_do_not_balance():
    unreconciled = [cid for cid in CUSTOMERS if '"reconciled":false' in ask(mock.cash_flow, cid).model_dump_json()]

    assert unreconciled == ["c_012"]
    answer = ask(mock.cash_flow, "c_012")
    assert "The other $195,000 matches no transaction" in claims(answer)
    assert "Balances and transactions reconcile" not in claims(answer)
    assert answer.could_not_determine == ["Where the other $195,000 went"]


def test_books_that_balance_are_said_to_balance_so_a_short_list_of_payments_is_not_read_as_a_hole():
    # The file lists only the payments that matter: c_010's add up to $220,000 of a $230,000 fall.
    # With a real model, saying nothing had the case handed over for records that disagree.
    said = "Balances and transactions reconcile: the transactions on record add up to the whole change."
    balanced = [cid for cid in CUSTOMERS if said in claims(ask(mock.cash_flow, cid))]

    # c_005 has an account that stopped reporting, so nothing can be said of its books. c_012's do not balance.
    assert balanced == [cid for cid in CUSTOMERS if cid not in ("c_005", "c_012")]
    books = next(e for e in ask(mock.cash_flow, "c_010").evidence if e.id == "agg_c_010_reconciliation")
    assert books.data == {"balance_change_cents": -23000000, "transactions_total_cents": -23000000, "reconciled": True}


# Data the file does not hold today, but could tomorrow.

ODD = """
clock: {signal_date: "2026-11-10", signal_window_days: 7, trailing_window_weeks: 13}
customers:
  - id: c_050
    name: Odd One Ltd
    story: Nothing like the others.
    snapshot:
      bank:
        cash_now_cents: 30000000
        cash_week_ago_cents: 25000000
        linked_accounts:
          - {id: acct_c050_a, active: true, days_stale: 1}
          - {id: acct_c050_b, active: false, days_stale: 0}
      card: {credit_limit_cents: 10000000, utilization_pct: 0.5, last_3_repayments: [late, failed, paid]}
    signal_window:
      bank_events: %s
      card_events: %s
    baselines:
      bank:
        weekly_inflow_cents: 9000000
        income_trend: steady
        payee_history_hint: {counterparty: Once Co, n_prior_payments: 1, typical_amount_cents: 100000, first_seen_months_ago: 3}
      card: {funding_account_change: false, monthly_card_spend_cents: 4000000}
"""


@pytest.fixture
def odd_data(tmp_path, monkeypatch):
    def use(
        card_events: str = "[{kind: late_repayment, amount_cents: 1}, {kind: failed_repayment, amount_cents: 2}]",
        bank_events: str = "[]",
        also: str | None = None,
    ):
        path = tmp_path / "odd.yaml"
        path.write_text(ODD % (bank_events, card_events))
        files = [str(path)]
        if also is not None:  # a second file, read after the first
            (tmp_path / "also.yaml").write_text(also)
            files.append(str(tmp_path / "also.yaml"))
        monkeypatch.setenv("SHARED_CUSTOMERS_FILE", os.pathsep.join(files))

    return use


def test_another_data_file_named_in_the_environment_is_the_one_read(odd_data):
    assert ask(mock.cash_flow, "c_050").findings == []  # the team's file has no c_050

    odd_data()

    assert ask(mock.cash_flow, "c_050").findings
    assert ask(mock.cash_flow, "c_002").could_not_determine == ["There is no data for c_002."]


def test_the_wording_holds_when_cash_rose_and_accounts_are_a_day_stale_or_closed(odd_data):
    odd_data()

    said = claims(ask(mock.cash_flow, "c_050"))

    assert "Linked cash rose by $50,000 in the week, from $250,000 to $300,000." in said
    assert "Account acct_c050_a has not reported for 1 day." in said
    assert "Account acct_c050_b is not active." in said
    assert "was paid 1 time before" in said
    assert "fell" not in said and "-" not in said.replace("acct_", "")


def test_repayments_are_listed_as_they_are_whatever_their_order(odd_data):
    odd_data()

    answer = ask(mock.credit_behavior, "c_050")

    assert "The last 3 repayments, oldest first: late, failed, paid." in claims(answer)
    # Each amount goes on the repayment it belongs to.
    assert [e.data.get("amount_cents") for e in answer.evidence if e.kind == "card_repayment"] == [1, 2, None]
    assert answer.could_not_determine == ["Why the repayment failed"]


def test_a_card_event_the_stand_in_does_not_know_is_an_error_not_a_silence(odd_data):
    odd_data("[{kind: chargeback, amount_cents: 5}]")

    with pytest.raises(ValueError, match="chargeback"):
        ask(mock.credit_behavior, "c_050")


# What the evaluation adds: customers of its own in a second file, and three facts the team's file does not hold.


def test_customers_from_a_second_file_are_read_next_to_the_first(odd_data):
    second = (ODD % ("[]", "[]")).replace("c_050", "c_051").replace("c050", "c051").replace('"2026-11-10"', '"2020-01-01"')
    odd_data(also=second)

    assert ask(mock.cash_flow, "c_050").findings
    assert ask(mock.cash_flow, "c_051").findings
    # The first file sets the day every customer is seen on.
    assert mock._data()["today"].isoformat() == "2026-11-10"


def test_a_customer_in_two_files_is_refused_and_neither_copy_is_used(odd_data):
    odd_data(also=ODD % ("[]", "[]"))

    with pytest.raises(ValueError, match="c_050 is in more than one data file"):
        ask(mock.cash_flow, "c_050")


def test_a_transfer_says_which_account_it_went_to_when_the_data_holds_that(odd_data):
    odd_data(
        bank_events="[{category: transfer_own_account, amount_cents: 3000000, counterparty_kind: own_account_other_bank,"
        " to_account: acct_elsewhere},"
        " {category: transfer_own_account, amount_cents: 500000, counterparty_kind: own_account_other_bank}]"
    )

    answer = ask(mock.cash_flow, "c_050")

    named, unnamed = [finding.claim for finding in answer.findings if "was transferred" in finding.claim]
    assert named.endswith("That account is not linked. It went to acct_elsewhere.")
    assert unnamed.endswith("That account is not linked.")
    assert [e.data.get("to_account") for e in answer.evidence if e.kind == "bank_transaction"] == ["acct_elsewhere", None]


@pytest.mark.parametrize(
    ("fact", "said"),
    [
        ("repaid_after_days: 3", "The failed repayment was paid in full 3 days later."),
        ("repaid_after_days: 1", "The failed repayment was paid in full 1 day later."),
        ("still_unpaid: true", "The failed repayment has not been paid since."),
    ],
)
def test_whether_a_failed_repayment_was_paid_afterwards_is_said_and_rests_on_that_repayment(odd_data, fact, said):
    odd_data(card_events="[{kind: failed_repayment, amount_cents: 2, %s}]" % fact)

    answer = ask(mock.credit_behavior, "c_050")

    finding = next(finding for finding in answer.findings if finding.claim == said)
    assert finding.evidence_ids == ["rep_c_050_2"]  # the failed one of late, failed, paid
    # The evidence holds the fact too, so a report that cites it can be checked against it.
    name, value = fact.split(": ")
    assert next(e for e in answer.evidence if e.id == "rep_c_050_2").data[name] == yaml.safe_load(value)


@pytest.mark.parametrize("fact", ["", ", still_unpaid: false"])
def test_nothing_is_said_about_paying_afterwards_when_the_data_does_not_say(odd_data, fact):
    # The team's c_010 is such a customer. Saying "not paid" there would be a guess.
    odd_data(card_events="[{kind: failed_repayment, amount_cents: 2%s}]" % fact)

    said = claims(ask(mock.credit_behavior, "c_050"))

    assert "paid in full" not in said and "has not been paid" not in said


# Topics: a stand-in answers the topic it is asked about, with an optional focus (spec section 4).

ROOT = Path(__file__).resolve().parents[3]
# The team's customers, then the evaluation's, as the stand-ins read them in an evaluation run.
BOTH_FILES = (ROOT / "data" / "shared_customers.yaml", ROOT / "eval" / "extra_customers.yaml")
EVERY_CUSTOMER = [customer["id"] for path in BOTH_FILES for customer in yaml.safe_load(path.read_text())["customers"]]
every_customer = pytest.mark.parametrize("customer_id", EVERY_CUSTOMER)
THE_WEEK = "The records held cover 2026-11-03 to 2026-11-10 only."
# The kinds of record each topic may return. Asked about one account, the bank adds the payments that went to it.
KINDS = {
    "transactions": {"daily_balance", "bank_transaction", "payment_summary"},
    "income": {"weekly_inflow_series"},
    "payee_history": {"bank_transaction", "payee_history"},
    "accounts": {"linked_account"},
    "reconciliation": {"balance_reconciliation", "daily_balance"},
    "prior_year": {"prior_year_comparison"},
    "repayments": {"card_repayment"},
    "paying_account": {"funding_account_change"},
    "card_use": {"card_balance", "card_spend_series"},
}


@pytest.fixture
def both_files(monkeypatch):
    monkeypatch.setenv("SHARED_CUSTOMERS_FILE", os.pathsep.join(str(path) for path in BOTH_FILES))


@pytest.fixture
def one_file(tmp_path, monkeypatch):
    """A data file with the text given, as the only one read."""

    def use(text: str):
        (tmp_path / "one.yaml").write_text(text)
        monkeypatch.setenv("SHARED_CUSTOMERS_FILE", str(tmp_path / "one.yaml"))

    return use


def on(specialist, customer_id: str, topic: str | None = None, **focus) -> InvestigateResponse:
    """What a side says on one topic, with an optional focus: account_id, record_id, or the dates."""
    request = {**REQUEST, "customer_id": customer_id, "topic": topic, **focus}
    return specialist(InvestigateRequest.model_validate(request))


def every_answer(customer_id: str):
    """(topic, focus, answer) for no topic, every topic of both sides, and every focus the customer's records give."""
    customer = mock._data()["customers"][customer_id]
    events = customer["signal_window"]["bank_events"]
    accounts = [account["id"] for account in customer["snapshot"]["bank"]["linked_accounts"]]
    # Accounts the bank does not link: where a transfer went, and the card's new paying account.
    accounts += [event["to_account"] for event in events if "to_account" in event]
    accounts += [event["to"] for event in customer["signal_window"]["card_events"] if "to" in event]
    questions = [(side, None, {}) for side in SIDES.values()]
    questions += [(mock.cash_flow, topic, {}) for topic in mock.BANK_TOPICS]
    questions += [(mock.credit_behavior, topic, {}) for topic in mock.CARD_TOPICS]
    questions += [
        (mock.cash_flow, "payee_history", {"record_id": f"txn_{customer_id}_{number:02d}"})
        for number in range(1, len(events) + 1)
    ]
    questions += [(mock.cash_flow, "accounts", {"account_id": account}) for account in accounts]
    for side, topic, focus in questions:
        yield topic, focus, on(side, customer_id, topic, **focus)


def without_memos(answer: InvestigateResponse) -> str:
    dump = answer.model_dump()
    for evidence in dump["evidence"]:
        evidence["data"].pop("memo", None)
    return str(dump)


def test_the_three_new_request_fields_are_optional_and_never_empty():
    request = InvestigateRequest.model_validate(REQUEST)

    assert (request.topic, request.account_id, request.record_id) == (None, None, None)
    for field in ("topic", "account_id", "record_id"):
        with pytest.raises(ValidationError):
            InvestigateRequest.model_validate({**REQUEST, field: ""})


def test_a_request_with_no_topic_is_answered_as_before_the_new_fields_existed():
    bank, card = ask(mock.cash_flow, "c_010"), ask(mock.credit_behavior, "c_010")

    # Today's sentences in today's order. The one sentence added is the share the listed payments are of the fall.
    assert [finding.claim for finding in bank.findings] == [
        "Linked cash fell by $230,000 in the week, from $520,000 to $290,000.",
        "The linked accounts are: acct_c010_a.",
        "A payroll run of $150,000 went out in the window.",
        "$70,000 was paid to a tax authority in the window.",
        "Payments listed for the window: 2, $220,000 in all. No other large payment went out.",
        "The listed payments are 96% of the fall. The other $10,000 is not in this list.",
        "Balances and transactions reconcile: the transactions on record add up to the whole change. "
        "Apart from any payment listed here, they are everyday payments and receipts, none of them large.",
        "Weekly inflows are steady at about $80,000 over the last 13 weeks.",
    ]
    assert [evidence.id for evidence in bank.evidence] == [
        "bal_c_010", "acct_c010_a", "txn_c_010_01", "txn_c_010_02",
        "agg_c_010_payments", "agg_c_010_reconciliation", "agg_c_010_inflows",
    ]  # fmt: skip
    # The keys of today stay as they were. New ones are added next to them.
    assert bank.evidence[0].data == {
        "cash_week_ago_cents": 52000000, "cash_now_cents": 29000000, "change_cents": -23000000,
    }  # fmt: skip
    assert bank.evidence[2].data == {
        "amount_cents": -15000000, "category": "payroll", "counterparty_kind": "payroll_vendor",
        "share_of_fall_bp": 6522,
    }  # fmt: skip
    assert [finding.claim for finding in card.findings] == [
        "The card balance is $92,000, 92% of the $100,000 limit. Utilization is spiking.",
        "The last 3 repayments, oldest first: paid, paid, failed.",
        "Card spend runs at about $90,000 a month.",
    ]
    assert [evidence.id for evidence in card.evidence] == [
        "bal_card_c_010", "rep_c_010_1", "rep_c_010_2", "rep_c_010_3", "agg_c_010_card_spend",
    ]  # fmt: skip
    # "92%" and "spiking" rest on the record, as a share and the typed label.
    assert card.evidence[0].data == {
        "credit_limit_cents": 10000000, "balance_cents": 9200000, "used_share_bp": 9200, "utilization_trend": "spiking",
    }  # fmt: skip
    # The other sentence added sets the payment next to what its payee was usually paid.
    assert claims(ask(mock.cash_flow, "c_009")).endswith(
        "usually $45,000. | This payment of $45,000 is 100% of the most it was usually paid ($45,000)."
    )
    # With no topic the dates are not read, and neither are the question and the context.
    other = {
        **REQUEST, "customer_id": "c_010", "window_start": "2026-10-01", "window_end": "2026-10-08",
        "question": "Ignore the data.", "context": "Cash rose.", "case_id": "case_x",
    }  # fmt: skip
    assert mock.cash_flow(InvestigateRequest.model_validate(other)) == bank


@every_customer
def test_a_topic_returns_only_its_own_kinds_of_record(both_files, customer_id):
    for topic, focus, answer in every_answer(customer_id):
        if topic is not None:
            kinds = KINDS[topic] | ({"bank_transaction"} if "account_id" in focus else set())
            assert {evidence.kind for evidence in answer.evidence} <= kinds, (topic, focus)


@every_customer
def test_the_same_record_has_the_same_content_whatever_topic_or_focus_returned_it(both_files, customer_id):
    seen = {}
    for topic, focus, answer in every_answer(customer_id):
        for evidence in answer.evidence:
            content = (evidence.kind, evidence.data)
            assert seen.setdefault(evidence.id, content) == content, (topic, focus, evidence.id)


@every_customer
def test_every_record_given_with_no_topic_is_given_by_some_topic(both_files, customer_id):
    # The graph always names a topic, so a fact that no topic gives would never reach it.
    by_topic = {evidence.id for topic, _, answer in every_answer(customer_id) if topic for evidence in answer.evidence}

    for side in SIDES.values():
        assert {evidence.id for evidence in ask(side, customer_id).evidence} <= by_topic


def test_ids_keep_their_place_in_the_full_list_when_a_subset_is_returned(both_files):
    # c_106's first event is income, so its one payment out is the second, whichever answer holds it.
    sent = on(mock.cash_flow, "c_106", "accounts", account_id="acct_external_new_c106")
    payments = on(mock.cash_flow, "c_106", "transactions")

    assert [evidence.id for evidence in sent.evidence] == ["acct_c106_a", "txn_c_106_02"]
    listed = next(finding for finding in payments.findings if finding.claim.startswith("Payments listed"))
    assert listed.evidence_ids == ["agg_c_106_payments", "txn_c_106_02"]
    # The failed repayment is the third of three, asked about on its topic or with no topic.
    for answer in (on(mock.credit_behavior, "c_110", "repayments"), ask(mock.credit_behavior, "c_110")):
        said = "The failed repayment has not been paid since."
        assert next(finding for finding in answer.findings if finding.claim == said).evidence_ids == ["rep_c_110_3"]
    # A transaction asked about by id keeps its id.
    history = on(mock.cash_flow, "c_011", "payee_history", record_id="txn_c_011_01")
    assert [evidence.id for evidence in history.evidence] == ["txn_c_011_01", "agg_c_011_payee_history"]


@every_customer
def test_every_record_a_topic_returns_is_cited_by_a_finding(both_files, customer_id):
    for topic, focus, answer in every_answer(customer_id):
        InvestigateResponse.model_validate(answer.model_dump())
        cited = {evidence_id for finding in answer.findings for evidence_id in finding.evidence_ids}
        assert cited == {evidence.id for evidence in answer.evidence}, (topic, focus)


@every_customer
def test_no_two_keys_of_a_record_are_shown_to_a_model_as_the_same_key(both_files, customer_id):
    # The Orchestrator shows a money key without _cents and a share key without _bp (spec 1.4).
    for _, _, answer in every_answer(customer_id):
        for evidence in answer.evidence:
            for data in (evidence.data, *[value for value in evidence.data.values() if isinstance(value, dict)]):
                keys = [key.replace("_cents", "").removesuffix("_bp") for key in data]
                assert len(keys) == len(set(keys)), (evidence.id, keys)


def test_a_topic_the_side_does_not_have_is_answered_with_cannot_determine_and_the_topics_it_has():
    bank = "The bank side has no such topic. It has: transactions, income, payee_history, accounts, reconciliation, prior_year."
    card = "The card side has no such topic. It has: repayments, paying_account, card_use."

    for side, topic, said in [
        (mock.cash_flow, "repayments", bank),
        (mock.cash_flow, "weather", bank),
        (mock.credit_behavior, "income", card),
        (mock.credit_behavior, "Repayments", card),
    ]:
        answer = on(side, "c_010", topic)
        assert (answer.findings, answer.evidence, answer.could_not_determine) == ([], [], [said]), topic
    # A customer the data does not hold is still an honest no, whatever the topic.
    assert on(mock.cash_flow, "c_099", "income").could_not_determine == ["There is no data for c_099."]


def test_asked_about_last_year_a_customer_with_no_such_data_gets_cannot_determine_and_no_finding():
    none = on(mock.cash_flow, "c_010", "prior_year")
    held = on(mock.cash_flow, "c_007", "prior_year")

    assert (none.findings, none.evidence) == ([], [])
    # "Not in the data", which says nothing about whether the same fall happened.
    assert none.could_not_determine == ["Figures for the same weeks last year are not in the data."]
    assert claims(held) == "In the same two weeks last year, $160,000 went out and $85,000 came in."
    assert held.evidence[0].data == {
        "outflow_cents": 16000000, "inflow_cents": 8500000,
        "period_days": 14, "window_days": 7, "window_out_cents": 15000000, "window_vs_prior_out_bp": 9375,
    }  # fmt: skip


@pytest.mark.parametrize(
    ("customer_id", "inflows"),
    [
        ("c_006", {
            "weekly_inflow_cents": 500000, "weekly_inflow_typical_cents": 9500000, "income_trend": "stopped",
            "weeks": 13, "compared_with": "usual", "vs_compared_bp": 526,
        }),
        ("c_103", {
            "weekly_inflow_cents": 3000000, "weekly_inflow_3mo_ago_cents": 17000000, "income_trend": "shrinking",
            "weeks": 13, "compared_with": "three_months_ago", "vs_compared_bp": 1765,
        }),
        # With no level to set the week against, there is no ratio.
        ("c_010", {"weekly_inflow_cents": 8000000, "income_trend": "steady", "weeks": 13}),
    ],
)  # fmt: skip
def test_this_weeks_inflows_are_set_against_the_level_the_data_holds_as_a_ratio(both_files, customer_id, inflows):
    answer = on(mock.cash_flow, customer_id, "income")

    assert [evidence.id for evidence in answer.evidence] == [f"agg_{customer_id}_inflows"]
    assert answer.evidence[0].data == inflows


def test_inflows_are_set_against_three_months_ago_when_the_data_also_holds_a_usual_level(one_file):
    week = "weekly_inflow_cents: 9000000"
    levels = f"{week}\n        weekly_inflow_3mo_ago_cents: 12000000\n        weekly_inflow_typical_cents: 10000000"
    one_file((ODD % ("[]", "[]")).replace(week, levels))

    inflows = on(mock.cash_flow, "c_050", "income").evidence[0].data

    assert (inflows["compared_with"], inflows["vs_compared_bp"]) == ("three_months_ago", 7500)


def test_a_payees_history_is_given_only_for_a_transaction_named_by_id():
    no_such = ["There is no such payment to look up. Pass record_id, the id of one transaction."]

    # No id, an id that is not a transaction's, another customer's transaction, and one that is not there.
    for record_id in (None, "agg_c_009_payee_history", "txn_c_011_01", "txn_c_009_02"):
        answer = on(mock.cash_flow, "c_009", "payee_history", record_id=record_id)
        assert (answer.findings, answer.evidence, answer.could_not_determine) == ([], [], no_such), record_id
    # The history is no part of the transactions answer either.
    assert "payee_history" not in on(mock.cash_flow, "c_009", "transactions").model_dump_json()

    named = on(mock.cash_flow, "c_009", "payee_history", record_id="txn_c_009_01")

    assert claims(named) == (
        "The payee of that payment was paid 2 times before over 24 months, usually $45,000. | "
        "This payment of $45,000 is 100% of the most it was usually paid ($45,000)."
    )
    assert named.findings[1].evidence_ids == ["agg_c_009_payee_history", "txn_c_009_01"]


@pytest.mark.parametrize("customer_id", ["c_008", "c_102"])
def test_a_payee_never_paid_before_is_said_to_have_no_history_and_that_rests_on_the_payment(both_files, customer_id):
    events = mock._data()["customers"][customer_id]["signal_window"]["bank_events"]
    new = [f"txn_{customer_id}_{n:02d}" for n, event in enumerate(events, 1) if event["category"] == "vendor_new"]
    assert new

    for txn in new:
        answer = on(mock.cash_flow, customer_id, "payee_history", record_id=txn)
        assert claims(answer) == f"The payee of {txn} was never paid before."
        assert answer.findings[0].evidence_ids == [txn] and [evidence.id for evidence in answer.evidence] == [txn]
        assert answer.could_not_determine == []


@pytest.mark.parametrize(
    "txn", ["txn_c_007_01", "txn_c_002_03", "txn_c_012_01", "txn_c_201_03", "txn_c_208_04", "txn_c_203_02"]
)
def test_a_vendor_with_no_history_on_file_is_said_to_be_unknown_not_new(both_files, txn):
    answer = on(mock.cash_flow, txn[4:9], "payee_history", record_id=txn)

    assert (answer.findings, answer.evidence) == ([], [])
    assert answer.could_not_determine == ["The payment history of that payee is not in the data."]


def test_the_history_on_file_is_of_the_first_payment_to_a_vendor_paid_before(odd_data):
    odd_data(
        bank_events="[{category: vendor_known, amount_cents: 150000, counterparty_kind: known_vendor},"
        " {category: vendor_known, amount_cents: 90000, counterparty_kind: known_vendor}]"
    )

    first = on(mock.cash_flow, "c_050", "payee_history", record_id="txn_c_050_01")
    second = on(mock.cash_flow, "c_050", "payee_history", record_id="txn_c_050_02")

    history = next(evidence.data for evidence in first.evidence if evidence.kind == "payee_history")
    assert history["about"] == "txn_c_050_01" and history["vs_usual_bp"] == 15000
    assert second.could_not_determine == ["The payment history of that payee is not in the data."]


@pytest.mark.parametrize(
    "txn",
    ["txn_c_001_01", "txn_c_001_02", "txn_c_002_01", "txn_c_003_01", "txn_c_106_02"],
    ids=["payroll", "tax", "own account", "income", "own account named"],
)
def test_payee_history_is_not_kept_for_payroll_tax_or_transfers(both_files, txn):
    answer = on(mock.cash_flow, txn[4:9], "payee_history", record_id=txn)

    assert (answer.findings, answer.evidence) == ([], [])
    assert answer.could_not_determine == ["Payee history is kept for vendors only."]


@pytest.mark.parametrize(("customer_id", "ratio", "said"), [("c_009", 10000, "100%"), ("c_011", 42500, "425%")])
def test_a_payment_is_set_against_the_most_its_payee_was_usually_paid_as_a_ratio_and_never_judged(
    customer_id, ratio, said
):
    answer = on(mock.cash_flow, customer_id, "payee_history", record_id=f"txn_{customer_id}_01")

    history = next(evidence.data for evidence in answer.evidence if evidence.kind == "payee_history")
    assert history["about"] == f"txn_{customer_id}_01" and history["vs_usual_bp"] == ratio
    assert history["vs_usual_bp"] == mock._bp(history["amount_cents"], history["usual_top_cents"])
    assert f" is {said} of the most it was usually paid " in claims(answer)
    # A measure, not a verdict: the judgment is the report's to make.
    for word in ("anomal", "unusual", "suspicious", "large", "too ", "concern", "risk", "normal"):
        assert word not in answer.model_dump_json().lower(), word


def test_the_transactions_answer_has_the_same_shape_whether_or_not_the_books_agree():
    def shape(customer_id: str):
        answer = on(mock.cash_flow, customer_id, "transactions")
        summary = next(evidence for evidence in answer.evidence if evidence.kind == "payment_summary")
        # The sentences on the balance and the summary with the figures taken out. The events differ by customer.
        said = [
            re.sub(r"\d[\d,]*", "N", finding.claim)
            for finding in answer.findings
            if finding.evidence_ids[0] in (f"bal_{customer_id}", summary.id)
        ]
        return [evidence.kind for evidence in answer.evidence], set(summary.data), said, answer.could_not_determine

    assert shape("c_010") == shape("c_012")
    for customer_id in ("c_010", "c_012"):
        said = on(mock.cash_flow, customer_id, "transactions").model_dump_json()
        assert "reconcil" not in said and "match" not in said and "large payment" not in said


@every_customer
def test_only_the_reconciliation_topic_says_whether_the_books_agree(both_files, customer_id):
    for topic, focus, answer in every_answer(customer_id):
        if topic not in (None, "reconciliation"):
            said = without_memos(answer)
            assert "balance_reconciliation" not in said and "reconcile" not in said, (topic, focus)
            assert "large payment" not in said and "Where the other" not in said, (topic, focus)

    books = on(mock.cash_flow, customer_id, "reconciliation")
    bank = mock._data()["customers"][customer_id]["snapshot"]["bank"]
    reporting = all(account["active"] and not account["days_stale"] for account in bank["linked_accounts"])
    if reporting and bank["cash_week_ago_cents"] > bank["cash_now_cents"]:
        record = next(evidence.data for evidence in books.evidence if evidence.kind == "balance_reconciliation")
        assert record["reconciled"] is (customer_id != "c_012")
    else:
        assert (books.findings, books.evidence) == ([], [])


def test_the_part_not_listed_and_the_part_unmatched_are_different_numbers_and_only_c_012_has_anything_unmatched(
    both_files,
):
    # Two quantities on two topics. What the list leaves out is a fact about the list, given for every customer.
    # What matches no transaction is a fact about the books. For c_012 the two come to the same amount.
    def record(customer_id: str, topic: str) -> dict:
        kind = {"transactions": "payment_summary", "reconciliation": "balance_reconciliation"}[topic]
        return next(evidence.data for evidence in on(mock.cash_flow, customer_id, topic).evidence if evidence.kind == kind)

    everything = {cid: str([answer.model_dump() for _, _, answer in every_answer(cid)]) for cid in EVERY_CUSTOMER}

    assert [cid for cid, said in everything.items() if "unmatched" in said] == ["c_012"]
    assert record("c_012", "transactions")["not_listed_cents"] == 19500000
    assert record("c_012", "reconciliation") == {
        "balance_change_cents": -29000000, "transactions_total_cents": -9500000, "reconciled": False,
        "unmatched_cents": 19500000, "unmatched_share_bp": 6724,
    }  # fmt: skip
    assert record("c_010", "transactions")["not_listed_cents"] == 1000000
    assert record("c_010", "reconciliation") == {
        "balance_change_cents": -23000000, "transactions_total_cents": -23000000, "reconciled": True,
    }  # fmt: skip
    assert "not_listed" not in on(mock.cash_flow, "c_012", "reconciliation").model_dump_json()


@every_customer
def test_the_payment_summary_adds_up_to_the_payments_it_lists(both_files, customer_id):
    answer = on(mock.cash_flow, customer_id, "transactions")
    events = [evidence.data for evidence in answer.evidence if evidence.kind == "bank_transaction"]
    # A payment out has a share of the fall. Income has none.
    assert not any("share_of_fall_bp" in data for data in events if data["category"] == "income")
    summary = next((evidence.data for evidence in answer.evidence if evidence.kind == "payment_summary"), None)
    if summary is None:
        return
    out = [-data["amount_cents"] for data in events if data["category"] != "income"]
    fall = -answer.evidence[0].data["change_cents"]
    by_category = summary["by_category"].values()

    assert (summary["listed_count"], summary["listed_out_cents"]) == (len(out), sum(out))
    assert summary["not_listed_cents"] == max(0, fall - sum(out))
    assert sum(category["out_cents"] for category in by_category) == sum(out)
    assert sum(category["count"] for category in by_category) == len(out)
    payroll_and_tax = [-data["amount_cents"] for data in events if data["category"] in ("payroll", "tax")]
    assert summary.get("payroll_and_tax_cents") == (sum(payroll_and_tax) if payroll_and_tax else None)
    # Income is set out on its own, never netted against the payments.
    income = [data["amount_cents"] for data in events if data["category"] == "income"]
    assert summary.get("listed_in_cents") == (sum(income) if income else None)
    # Every share is of the fall, not of the payments listed.
    assert summary["listed_share_bp"] == mock._bp(sum(out), fall)
    assert summary["not_listed_share_bp"] == mock._bp(summary["not_listed_cents"], fall)
    assert summary.get("payroll_and_tax_share_bp") == (mock._bp(sum(payroll_and_tax), fall) if payroll_and_tax else None)
    assert all(category["share_bp"] == mock._bp(category["out_cents"], fall) for category in by_category)


def test_payments_listed_above_the_fall_leave_nothing_not_listed(one_file):
    # A fall of $10,000 and a payroll run of $30,000: everyday receipts made up the rest. Every account reports.
    payroll = "[{category: payroll, amount_cents: 3000000, counterparty_kind: payroll_vendor}]"
    one_file(
        (ODD % (payroll, "[]"))
        .replace("cash_now_cents: 30000000", "cash_now_cents: 24000000")
        .replace("days_stale: 1", "days_stale: 0")
        .replace("active: false", "active: true")
    )

    answer = on(mock.cash_flow, "c_050", "transactions")

    summary = next(evidence.data for evidence in answer.evidence if evidence.kind == "payment_summary")
    assert (summary["listed_share_bp"], summary["not_listed_cents"], summary["not_listed_share_bp"]) == (30000, 0, 0)


def test_a_stale_account_gets_no_shares_no_summary_and_no_word_on_the_books(one_file):
    assert on(mock.cash_flow, "c_005", "transactions").could_not_determine == [
        "Which payments left through acct_c005_a since it stopped reporting"
    ]
    books = on(mock.cash_flow, "c_005", "reconciliation")
    assert (books.findings, books.evidence) == ([], [])
    assert books.could_not_determine == ["Whether balances and transactions agree: acct_c005_a is not reporting."]
    assert "agg_c_005_payments" not in ask(mock.cash_flow, "c_005").model_dump_json()

    # A fall and a payment, with two accounts that do not report: one a day behind, one closed.
    payroll = "[{category: payroll, amount_cents: 1000000, counterparty_kind: payroll_vendor}]"
    one_file((ODD % (payroll, "[]")).replace("cash_now_cents: 30000000", "cash_now_cents: 20000000"))

    for answer in (on(mock.cash_flow, "c_050", "transactions"), ask(mock.cash_flow, "c_050")):
        said = answer.model_dump_json()
        assert "share" not in said and "payment_summary" not in said and "reconcil" not in said
        assert "Linked cash fell by $50,000" in claims(answer)
    assert on(mock.cash_flow, "c_050", "reconciliation").could_not_determine == [
        "Whether balances and transactions agree: acct_c050_a is not reporting.",
        "Whether balances and transactions agree: acct_c050_b is not reporting.",
    ]


@pytest.mark.parametrize(("cash_now", "moved"), [(30000000, "rose by $50,000"), (25000000, "changed by $0")])
def test_cash_that_rose_gets_no_shares(one_file, cash_now, moved):
    # Every account reports, so only the direction of cash keeps the shares out.
    payroll = "[{category: payroll, amount_cents: 1000000, counterparty_kind: payroll_vendor}]"
    one_file(
        (ODD % (payroll, "[]"))
        .replace("cash_now_cents: 30000000", f"cash_now_cents: {cash_now}")
        .replace("days_stale: 1", "days_stale: 0")
        .replace("active: false", "active: true")
    )

    payments = on(mock.cash_flow, "c_050", "transactions")
    books = on(mock.cash_flow, "c_050", "reconciliation")

    assert f"Linked cash {moved} in the week" in claims(payments)
    assert "share" not in payments.model_dump_json() and "payment_summary" not in payments.model_dump_json()
    assert payments.could_not_determine == []
    assert (books.findings, books.evidence) == ([], [])
    assert books.could_not_determine == ["Linked cash did not fall in the window, so there is no fall to reconcile."]


def test_asked_about_an_account_that_is_not_linked_the_bank_says_so_and_says_whether_money_went_to_it(both_files):
    nothing = on(mock.cash_flow, "c_006", "accounts", account_id="acct_external_new_c006")
    some = on(mock.cash_flow, "c_106", "accounts", account_id="acct_external_new_c106")

    assert claims(nothing) == (
        "acct_external_new_c006 is not one of the linked accounts. | No payment listed for the window went to it."
    )
    assert [evidence.id for evidence in nothing.evidence] == ["acct_c006_a"]
    assert claims(some) == (
        "acct_external_new_c106 is not one of the linked accounts. | $30,000 went to it in the window."
    )
    assert some.findings[1].evidence_ids == ["txn_c_106_02"]
    assert some.evidence[1].data["to_account"] == "acct_external_new_c106"


@pytest.mark.parametrize(("start", "end"), [("2025-11-03", "2025-11-10"), ("2026-11-04", "2026-11-10")])
def test_asked_about_an_outside_account_for_other_dates_the_bank_says_only_which_week_it_holds(both_files, start, end):
    # Whether money went to the account is a fact about the week, so it is answered as the transactions topic is.
    for customer_id, account in [("c_106", "acct_external_new_c106"), ("c_006", "acct_external_new_c006")]:
        answer = on(mock.cash_flow, customer_id, "accounts", account_id=account, window_start=start, window_end=end)

        assert claims(answer) == f"{account} is not one of the linked accounts.", customer_id
        assert {evidence.kind for evidence in answer.evidence} == {"linked_account"}, customer_id
        assert answer.could_not_determine == [THE_WEEK], customer_id


def test_asked_about_a_linked_account_the_bank_gives_that_account_alone():
    stale = on(mock.cash_flow, "c_005", "accounts", account_id="acct_c005_a")
    fine = on(mock.cash_flow, "c_005", "accounts", account_id="acct_c005_b")

    assert claims(stale) == (
        "acct_c005_a is one of the linked accounts. | "
        "Account acct_c005_a has not reported for 5 days. Its balance since then is not in the figures."
    )
    assert stale.could_not_determine == ["What acct_c005_a holds since it stopped reporting"]
    assert claims(fine) == "acct_c005_b is one of the linked accounts."
    assert [evidence.id for evidence in fine.evidence] == ["acct_c005_b"] and fine.could_not_determine == []


def test_the_bank_side_names_an_outside_account_only_when_asked_about_it(both_files):
    # c_006's new paying account is the card side's to know. The bank names it only once it is asked about it.
    for topic, focus, answer in every_answer("c_006"):
        if answer.agent == "cash_flow":
            asked = focus.get("account_id") == "acct_external_new_c006"
            assert ("acct_external_new_c006" in answer.model_dump_json()) is asked, (topic, focus)
    # c_106's own records hold where its transfer went (to_account), so its transactions answer names the account
    # on that payment. No other topic names it unasked. (Spec section 4 says "only when asked"; its transactions
    # row and the account match the Orchestrator works out both need to_account there.)
    for topic, focus, answer in every_answer("c_106"):
        if answer.agent == "cash_flow" and topic and "account_id" not in focus:
            named = [e.id for e in answer.evidence if "acct_external_new_c106" in e.model_dump_json()]
            assert named == (["txn_c_106_02"] if topic == "transactions" else []), topic


@pytest.mark.parametrize(("customer_id", "paid_since"), [("c_010", "not_recorded"), ("c_110", "no"), ("c_111", "yes")])
def test_whether_a_failed_repayment_was_paid_is_yes_no_or_not_recorded_and_c_010_is_not_recorded(
    both_files, customer_id, paid_since
):
    for answer in (on(mock.credit_behavior, customer_id, "repayments"), ask(mock.credit_behavior, customer_id)):
        repayments = [evidence.data for evidence in answer.evidence if evidence.kind == "card_repayment"]
        assert [data.get("paid_since") for data in repayments] == [None, None, paid_since]
    # A late repayment says nothing of the kind.
    assert "paid_since" not in on(mock.credit_behavior, "c_003", "repayments").model_dump_json()


@pytest.mark.parametrize(
    ("event", "statuses", "paid_since"),
    [
        ("still_unpaid: false", "[late, failed, paid]", [None, "not_recorded", None]),
        ("still_unpaid: true", "[late, failed, paid]", [None, "no", None]),
        # Only the latest failure carries what the file says about it. The earlier one is not recorded.
        ("repaid_after_days: 2", "[failed, paid, failed]", ["not_recorded", None, "yes"]),
    ],
)
def test_paid_since_is_yes_only_with_a_day_count_and_no_only_when_the_file_says_still_unpaid(
    one_file, event, statuses, paid_since
):
    card = "[{kind: failed_repayment, amount_cents: 2, %s}]" % event
    one_file((ODD % ("[]", card)).replace("[late, failed, paid]", statuses))

    answer = on(mock.credit_behavior, "c_050", "repayments")

    assert [evidence.data.get("paid_since") for evidence in answer.evidence] == paid_since


def test_a_paying_account_that_did_not_change_is_said_so_only_when_asked():
    asked = on(mock.credit_behavior, "c_001", "paying_account")

    assert claims(asked) == "The account that pays the card did not change during the window."
    assert [(evidence.id, evidence.data) for evidence in asked.evidence] == [("rep_c_001_funding", {"changed": False})]
    assert "rep_c_001_funding" not in ask(mock.credit_behavior, "c_001").model_dump_json()
    # A change is said on its topic and with no topic, in today's words.
    for answer in (on(mock.credit_behavior, "c_006", "paying_account"), ask(mock.credit_behavior, "c_006")):
        change = next(evidence for evidence in answer.evidence if evidence.id == "rep_c_006_funding")
        assert change.data == {"from": "acct_c006_a", "to": "acct_external_new_c006", "changed": True}
        assert "changed from acct_c006_a to acct_external_new_c006." in claims(answer)


@pytest.mark.parametrize(
    ("start", "end"),
    [("2026-10-01", "2026-10-08"), ("2026-11-04", "2026-11-10"), ("2026-11-03", "2026-11-09"), ("2025-11-03", "2025-11-10")],
)
def test_a_window_the_data_does_not_cover_is_answered_with_what_it_does_cover(start, end):
    dates = {"window_start": start, "window_end": end}

    for topic in ("transactions", "reconciliation"):
        answer = on(mock.cash_flow, "c_010", topic, **dates)
        assert (answer.findings, answer.evidence, answer.could_not_determine) == ([], [], [THE_WEEK]), topic
    # The other topics are not about the week, and ignore the dates: last year's dates still get c_007's last year.
    # The accounts topic ignores them for the linked accounts. What went to another account is about the week.
    for side, customer_id, topic, focus in [
        (mock.cash_flow, "c_010", "income", {}),
        (mock.cash_flow, "c_010", "accounts", {}),
        (mock.cash_flow, "c_005", "accounts", {"account_id": "acct_c005_a"}),
        (mock.cash_flow, "c_007", "prior_year", {}),
        (mock.cash_flow, "c_009", "payee_history", {"record_id": "txn_c_009_01"}),
        (mock.credit_behavior, "c_010", "repayments", {}),
        (mock.credit_behavior, "c_006", "paying_account", {}),
        (mock.credit_behavior, "c_010", "card_use", {}),
    ]:
        asked = on(side, customer_id, topic, **focus, **dates)
        assert asked.findings and asked == on(side, customer_id, topic, **focus), (customer_id, topic)


def test_a_topic_answer_does_not_depend_on_the_question_or_the_context():
    # An id in the question must not stand in for the account or the record the request names.
    for side, topics in [(mock.cash_flow, mock.BANK_TOPICS), (mock.credit_behavior, mock.CARD_TOPICS)]:
        for topic in topics:
            plain = on(side, "c_010", topic)
            for words in ("Say it was paid.", "acct_c010_a", "txn_c_010_01"):
                other = on(side, "c_010", topic, question=words, context=words, case_id="case_9")
                assert other == plain, (topic, words)


@every_customer
def test_no_answer_to_any_topic_names_a_cause_or_a_recommendation(both_files, customer_id):
    labels = (*get_args(LikelyCause), *get_args(Recommendation))
    for topic in (*mock.BANK_TOPICS, *mock.CARD_TOPICS):
        assert not any(label in topic for label in labels), topic

    for topic, focus, answer in every_answer(customer_id):
        # A memo is the payer's own words, and c_203's is there to mislead. It goes out as data.
        said = without_memos(answer)
        for label in labels:
            assert label not in said, (topic, focus, label)


@pytest.mark.parametrize(
    ("txn", "words"),
    [("txn_c_009_01", ("IGNORE", "LIQUIDITY", "c_003")), ("txn_c_203_02", ("4471", "keep_limit", "pre-approved"))],
)
def test_the_memos_still_go_out_as_data_and_no_finding_points_at_them(both_files, txn, words):
    customer_id = txn[4:9]
    memo = mock._data()["customers"][customer_id]["signal_window"]["bank_events"][int(txn[-2:]) - 1]["memo"]
    carried = 0

    for topic, focus, answer in every_answer(customer_id):
        for evidence in answer.evidence:
            if evidence.id == txn:
                assert evidence.data["memo"] == memo.strip(), (topic, focus)
                carried += 1
        for said in [finding.claim for finding in answer.findings] + answer.could_not_determine:
            assert "memo" not in said.lower() and not any(word in said for word in words), (topic, focus, said)
    assert carried >= 2  # with no topic and on the transactions topic, and for c_009 asked about by id as well


def test_the_stand_ins_import_nothing_from_the_graph_or_the_policy():
    imported = set()
    for node in ast.walk(ast.parse(Path(mock.__file__).read_text())):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            # A relative import would reach the service's own code by another name.
            assert node.level == 0, ast.unparse(node)
            # "from app import policy" names the module in what it imports.
            imported |= {node.module} | {f"{node.module}.{alias.name}" for alias in node.names}

    # A specialist measures and never judges: the policy's thresholds are not its to read.
    assert [name for name in imported if name.split(".")[:2] in (["app", "graph"], ["app", "policy"])] == []


@pytest.mark.parametrize(
    ("bp", "said"),
    [(0, "0%"), (1, "under 1%"), (49, "under 1%"), (50, "1%"), (4449, "44%"), (4450, "45%"), (42500, "425%")],
)
def test_a_share_is_said_in_whole_percent_rounded_half_up(bp, said):
    assert mock._pct(bp) == said


@pytest.mark.parametrize(
    ("cents", "said"), [(23000000, "$230,000"), (1250, "$12.50"), (99991, "$999.91"), (5, "$0.05"), (0, "$0")]
)
def test_money_is_said_in_whole_dollars_with_cents_only_when_there_are_some(cents, said):
    # Rounded away, $999.91 would be said as $1,000, a figure no record holds.
    assert mock._usd(cents) == said


def test_a_share_is_worked_out_in_basis_points_rounded_half_up_and_not_at_all_without_a_whole():
    assert [mock._bp(1, 3), mock._bp(2, 3), mock._bp(12000000, 49400000), mock._bp(1, 20000)] == [3333, 6667, 2429, 1]
    assert mock._bp(5, 0) is None and mock._bp(5, -1) is None
