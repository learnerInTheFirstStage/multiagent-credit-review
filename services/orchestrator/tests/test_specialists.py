import copy
import os
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
    assert stale.data == {"active": True, "days_stale": 5, "last_synced": "2026-11-05"}
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
    assert latest.data == {"sequence": 3, "of": 3, "status": "failed", "amount_cents": 5000000}
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
