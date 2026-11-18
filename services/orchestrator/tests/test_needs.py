"""Guideline needs, the actions they permit, and the gaps code finds (spec 1.6, 1.7, 2.A, 2.D.3, 5, 8.4).

The records are built by hand here, with the kinds, data keys and topics of spec 1.4 and section 4, from what
the data files say about each customer. The policy table test asserts the same rules on the stand-ins' answers.
"""

import copy
import json
import os
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import guidelines, policy
from app.agents import mock
from app.graph import causes, facts, figures, needs, plans
from app.guidelines import Guideline, Need
from contracts import InvestigateRequest

GUIDELINES = guidelines.load()
ROOT = Path(__file__).resolve().parents[3]
DATA_FILES = (ROOT / "data" / "shared_customers.yaml", ROOT / "eval" / "extra_customers.yaml")
# The test fixture's guidelines (tests/test_graph.py): no needs, so nothing to check.
NO_NEEDS = [
    {"id": "G-03", "when": "The fall is explained by payroll or tax.", "allows": ["keep_limit"]},
    {"id": "G-07", "when": "A repayment failed.", "allows": ["reduce_limit", "request_documents"]},
    {"id": "G-11", "when": "Cash cannot cover the balance owed.", "allows": ["pause_card", "reduce_limit"]},
    {"id": "G-12", "when": "The limit is very large.", "allows": ["needs_human"]},
]
TOOL = {topic: tool for tool, topics in causes.TOPICS.items() for topic in topics}

# What the data files hold for the customers used here, cut down to what the records below are made of.
# "inflow" is weekly inflows against the level they are compared with; "usual_top" the most a payee was usually paid.
PAID = ["paid", "paid", "paid"]
CUSTOMERS = {
    "c_001": {"fall": 40500000, "events": [("payroll", 28000000), ("tax", 12500000)], "trend": "steady"},
    "c_003": {"fall": 30000000, "events": [("income", 3000000)], "trend": "shrinking", "inflow": (3000000, 17000000),
              "repayments": ["paid", "paid", "late"]},
    "c_004": {"fall": 44000000, "events": [("tax", 22000000), ("income", 6000000)], "trend": "too_short_to_tell",
              "inflow": (6000000, 12000000)},
    "c_005": {"fall": 35000000, "events": [], "trend": "steady", "stale": True},
    "c_007": {"fall": 29400000, "events": [("vendor_known", 15000000)], "trend": "steady",
              "prior_year_out": 16000000, "recovery_bp": 10294},
    "c_008": {"fall": 49400000, "trend": "steady",
              "events": [("vendor_new", 15000000), ("vendor_new", 12000000), ("vendor_new", 11000000)]},
    "c_009": {"fall": 15200000, "events": [("vendor_known", 4500000)], "trend": "steady", "usual_top": 4500000},
    "c_010": {"fall": 23000000, "events": [("payroll", 15000000), ("tax", 7000000)], "trend": "steady",
              "repayments": ["paid", "paid", "failed"], "failed": {}},
    "c_011": {"fall": 17000000, "events": [("vendor_known", 17000000)], "trend": "steady", "usual_top": 4000000},
    "c_012": {"fall": 29000000, "events": [("vendor_known", 6000000), ("payroll", 3500000)], "trend": "steady",
              "unreconciled": True},
    "c_110": {"fall": 23000000, "events": [("payroll", 15000000), ("tax", 7000000)], "trend": "steady",
              "repayments": ["paid", "paid", "failed"], "failed": {"still_unpaid": True}},
    "c_111": {"fall": 23000000, "events": [("payroll", 15000000), ("tax", 7000000)], "trend": "steady",
              "repayments": ["paid", "paid", "failed"], "failed": {"repaid_after_days": 3}},
    # Seeded history (8.2): c_307's cash did not come back; c_314 was asked twice for weak income, c_304 twice
    # to reconnect a feed.
    "c_304": {"fall": 44000000, "events": [("tax", 22000000), ("income", 6000000)], "trend": "too_short_to_tell",
              "inflow": (6000000, 12000000), "requests": (2, 0)},
    "c_307": {"fall": 29400000, "events": [("vendor_known", 15000000)], "trend": "steady",
              "prior_year_out": 16000000, "recovery_bp": 7778},
    "c_314": {"fall": 44000000, "events": [("tax", 22000000), ("income", 6000000)], "trend": "too_short_to_tell",
              "inflow": (6000000, 12000000), "requests": (2, 2)},
    "c_002": {"fall": 72000000, "trend": "steady",
              "events": [("transfer_own_account", 40000000), ("payroll", 20000000), ("vendor_known", 12000000)]},
    "c_106": {"fall": 20200000, "events": [("income", 500000), ("transfer_own_account", 3000000)], "trend": "stopped",
              "inflow": (500000, 9500000)},
    # Not in the data. c_099: two payments to vendors paid before, so that a payee's history has two to choose from.
    "c_099": {"fall": 10000000, "events": [("vendor_known", 2000000), ("vendor_known", 3000000)], "trend": "steady"},
    # c_098: the same, and the history of the larger payment is far out of line.
    "c_098": {"fall": 10000000, "events": [("vendor_known", 4000000), ("vendor_known", 2000000)], "trend": "steady",
              "usual_top": 1000000},
    # c_097: c_002's transfer, alone, and a late repayment. c_095: c_004 with a late repayment.
    "c_097": {"fall": 72000000, "events": [("transfer_own_account", 40000000)], "trend": "steady",
              "repayments": ["paid", "paid", "late"]},
    "c_095": {"fall": 44000000, "events": [("tax", 22000000), ("income", 6000000)], "trend": "too_short_to_tell",
              "inflow": (6000000, 12000000), "repayments": ["paid", "paid", "late"]},
    # c_094: a payment to a vendor paid before, and a larger payroll run.
    "c_094": {"fall": 10000000, "events": [("vendor_known", 3000000), ("payroll", 4000000)], "trend": "steady"},
}
# The review window code writes into sig_cash when the case opens (1.4).
WINDOW = ("2026-11-03", "2026-11-10")


def bp(part: int, whole: int) -> int:
    return (10000 * part + whole // 2) // whole


def record(id: str, kind: str, **data) -> dict:
    return {"id": id, "kind": kind, "data": data}


def transactions(cid: str) -> list[dict]:
    customer = CUSTOMERS[cid]
    found = []
    for number, (category, amount) in enumerate(customer["events"], 1):
        data = {"amount_cents": amount if category == "income" else -amount, "category": category}
        if category != "income" and not customer.get("stale"):
            data["share_of_fall_bp"] = bp(amount, customer["fall"])
        found.append(record(f"txn_{cid}_{number:02d}", "bank_transaction", **data))
    return found


def answer(cid: str, step) -> list[dict]:
    """The records one question returns."""
    customer, (topic, record_id) = CUSTOMERS[cid], (step if isinstance(step, tuple) else (step, None))
    balance = record(f"bal_{cid}", "daily_balance", change_cents=-customer["fall"])
    if topic == "transactions":
        return [balance, *transactions(cid)]
    if topic == "income":
        data = {"income_trend": customer["trend"], "weeks": 13}
        if inflow := customer.get("inflow"):
            data |= {"compared_with": "usual", "vs_compared_bp": bp(*inflow)}
        return [record(f"agg_{cid}_inflows", "weekly_inflow_series", **data)]
    if topic == "payee_history":
        paid = next(txn for txn in transactions(cid) if txn["id"] == record_id)
        if "usual_top" not in customer or paid["data"]["category"] != "vendor_known":
            return []  # the payee's history is not in the data
        amount = -paid["data"]["amount_cents"]
        history = record(f"agg_{cid}_payee_history", "payee_history", about=record_id, amount_cents=amount,
                         usual_top_cents=customer["usual_top"], vs_usual_bp=bp(amount, customer["usual_top"]))
        return [paid, history]
    if topic == "accounts":
        return [record(f"acct_{cid}_a", "linked_account", active=True, days_stale=5 if customer.get("stale") else 0,
                       is_stale=bool(customer.get("stale")))]
    if topic == "reconciliation":
        agrees = not customer.get("unreconciled")
        return [record(f"agg_{cid}_reconciliation", "balance_reconciliation", reconciled=agrees), balance]
    if topic == "prior_year":
        if "prior_year_out" not in customer:
            return []
        out = sum(amount for category, amount in customer["events"] if category != "income")
        return [record(f"agg_{cid}_prior_year", "prior_year_comparison", period_days=14, window_days=7,
                       outflow_cents=customer["prior_year_out"], window_out_cents=out,
                       window_vs_prior_out_bp=bp(out, customer["prior_year_out"]))]
    if topic == "repayments":
        statuses = customer.get("repayments", PAID)
        found = []
        for number, status in enumerate(statuses, 1):
            data = {"sequence": number, "of": len(statuses), "status": status}
            if status == "failed":
                failed = customer["failed"]
                since = "yes" if "repaid_after_days" in failed else "no" if failed.get("still_unpaid") else "not_recorded"
                data |= {"amount_cents": 5000000, "paid_since": since, **failed}
            found.append(record(f"rep_{cid}_{number}", "card_repayment", **data))
        return found
    if topic == "paying_account":
        return [record(f"rep_{cid}_funding", "funding_account_change", changed=False)]
    return [record(f"bal_card_{cid}", "card_balance", used_share_bp=5000)]  # card_use


def own(cid: str) -> list[dict]:
    """The records code gives when the case opens (1.4, 7.3)."""
    customer = CUSTOMERS[cid]
    statuses = customer.get("repayments", PAID)
    requests, for_income = customer.get("requests", (0, 0))
    found = [
        record("sig_cash", "cash_change", window_start=WINDOW[0], window_end=WINDOW[1], fall_cents=customer["fall"],
               below_minimum=False),
        record("sig_limit", "limit_change", is_cut=True, very_large=False),
        record("sig_card", "card_position", used_share_bp=5000),
        record("sig_repayments", "repayment_statuses", statuses=statuses, all_paid=all(s == "paid" for s in statuses),
               any_late="late" in statuses, any_failed="failed" in statuses, latest_status=statuses[-1]),
        record("sig_accounts", "account_sync", any_behind=bool(customer.get("stale"))),
        record(f"hist_{cid}_summary", "review_history", earlier_reviews=requests, document_requests_in_window=requests,
               document_requests_for_income_in_window=for_income),
    ]
    if "recovery_bp" in customer:
        found.append(record(f"hist_{cid}_recovery", "cash_recovery", now_vs_then_bp=customer["recovery_bp"]))
    return found


def ledger(cid: str, *steps) -> list[dict]:
    """The case's own records and what the questions returned. One id, one record."""
    found = {item["id"]: item for item in own(cid)}
    for step in steps:
        for item in answer(cid, step):
            found.setdefault(item["id"], item)
    return list(found.values())


def sent(*steps, outcome: str = "answered", **focus) -> list[dict]:
    """`asked` entries for questions that went out (1.3). `focus` sets start, end, account_id or returned."""
    entries = []
    for step in steps:
        topic, record_id = step if isinstance(step, tuple) else (step, None)
        entry = {"tool": TOOL[topic], "agent": None, "topic": topic, "separates": ["revenue_loss"],
                 "tests": causes.TOPIC_TESTS[topic], "outcome": outcome, "returned": []} | focus
        if record_id:
            entry["record_id"] = record_id
        entries.append(entry)
    return entries


def case(cid: str, *steps) -> tuple[list[dict], list[dict]]:
    return ledger(cid, *steps), sent(*steps)


def gaps(cid: str, *steps, guidelines=GUIDELINES) -> list[tuple[str, str]]:
    found = needs.code_gaps(guidelines, *case(cid, *steps))
    return [(gap["rule"], gap["text"].split("Not yet asked: ")[1]) for gap in found]


def guideline(guideline_id: str, among=GUIDELINES) -> dict:
    return next(g for g in among if g["id"] == guideline_id)


def need(guideline_id: str, number: int = 1) -> dict:
    return guideline(guideline_id)["needs"][number - 1]


def by_id(records: list[dict], *ids: str) -> list[dict]:
    return [item for item in records if item["id"] in ids]


# Paths on which the gap rules are run in the invariant tests below.
PATHS = [
    ("c_001",), ("c_001", "transactions"), ("c_001", "transactions", "income"), ("c_003",), ("c_004", "transactions"),
    ("c_005", "transactions"), ("c_005", "transactions", "accounts"), ("c_007", "transactions", "income"),
    ("c_007", "transactions", "income", ("payee_history", "txn_c_007_01")), ("c_008", "transactions"),
    ("c_009", "transactions", "income"), ("c_010", "transactions"), ("c_010", "transactions", "repayments", "income"),
    ("c_011", "transactions"), ("c_011", "transactions", ("payee_history", "txn_c_011_01"), "income"),
    ("c_012", "transactions", "income"), ("c_099", "transactions", "income"), ("c_110", "transactions", "income"),
    ("c_307", "transactions", "income"), ("c_314",), ("c_314", "transactions", "income"),
]


def test_the_hand_built_records_have_the_shares_the_spec_works_with():
    # Guards the records below against a slip in this file: the spec's own figures (5.1, 5.2, 8.1, 9.5).
    shares = {item["id"]: item["data"]["share_of_fall_bp"] for item in transactions("c_008")}
    assert shares == {"txn_c_008_01": 3036, "txn_c_008_02": 2429, "txn_c_008_03": 2227}
    assert sum(shares.values()) == 7692
    assert transactions("c_004")[0]["data"]["share_of_fall_bp"] == 5000
    assert transactions("c_009")[0]["data"]["share_of_fall_bp"] == 2961
    assert transactions("c_307")[0]["data"]["share_of_fall_bp"] == 5102
    history = by_id(ledger("c_011", ("payee_history", "txn_c_011_01")), "agg_c_011_payee_history")[0]
    assert history["data"]["vs_usual_bp"] == 42500
    assert by_id(ledger("c_007", "prior_year"), "agg_c_007_prior_year")[0]["data"]["window_vs_prior_out_bp"] == 9375


# --- How a need is met -------------------------------------------------------------------------------------------


def test_a_need_is_not_met_by_a_record_of_the_right_kind_with_the_wrong_value():
    shrinking = need("G-06")

    assert not needs.meets(shrinking, ledger("c_001", "income"))  # steady
    assert needs.meets(shrinking, ledger("c_003", "income"))


def test_a_need_on_a_field_the_record_does_not_have_is_not_met():
    sharply = need("G-05")  # the trend and how far inflows fell against their comparison level
    no_level = record("agg_c_003_inflows", "weekly_inflow_series", income_trend="shrinking", weeks=13)

    assert not needs.meets(sharply, [no_level])
    assert needs.meets(sharply, [{**no_level, "data": {**no_level["data"], "vs_compared_bp": 1765}}])
    assert not needs.meets(need("G-01", 2), [record("agg_x_inflows", "weekly_inflow_series")])


def test_a_need_with_a_share_adds_up_the_matching_records_and_one_of_three_is_not_enough():
    new_payees = need("G-09")  # a large part of the fall: a quarter
    payments = transactions("c_008")

    assert not needs.meets(new_payees, by_id(payments, "txn_c_008_02"))  # 2429
    assert not needs.meets(new_payees, by_id(payments, "txn_c_008_03"))  # 2227
    assert needs.meets(new_payees, payments)  # 7692 together


def test_one_payment_to_a_new_payee_over_the_quarter_line_is_a_large_part():
    assert needs.meets(need("G-09"), by_id(transactions("c_008"), "txn_c_008_01"))  # 3036


@pytest.mark.parametrize("share", [None, "3036", "missing"])
def test_a_payment_without_a_share_of_the_fall_adds_nothing_to_a_share(share):
    # The stand-ins leave the share out when cash did not fall or an account is stale (4).
    data = {"amount_cents": -15000000, "category": "vendor_new"}
    if share != "missing":
        data["share_of_fall_bp"] = share

    assert not needs.meets(need("G-09"), [record("txn_x_01", "bank_transaction", **data)])


def test_a_share_of_exactly_the_line_meets_the_need():
    payroll_or_tax = need("G-01")  # most of the fall: a half
    tax = transactions("c_004")[0]

    assert tax["data"]["share_of_fall_bp"] == policy.EXPLAINS_BP
    assert needs.meets(payroll_or_tax, [tax])
    under = {**tax, "data": {**tax["data"], "share_of_fall_bp": policy.EXPLAINS_BP - 1}}
    assert not needs.meets(payroll_or_tax, [under])


def test_a_record_passed_twice_counts_once_toward_a_share():
    # A transaction comes back under transactions and again under payee_history. It is still one payment.
    most = {"kind": "bank_transaction", "where": {"category": "vendor_known"}, "share_at_least_bp": 5000, "says": "x"}
    payment = transactions("c_009")[0]  # 2961

    assert not needs.meets(most, [payment, copy.deepcopy(payment)])


def test_a_range_needs_both_ends():
    same_fall = need("G-03")  # the same fall: half to one and a half times last year's
    low, high = policy.SAME_FALL_BP

    def last_year(ratio):
        return [record("agg_c_007_prior_year", "prior_year_comparison", window_vs_prior_out_bp=ratio)]

    assert [needs.meets(same_fall, last_year(ratio)) for ratio in (low - 1, low, 9375, high, high + 1)] == [
        False, True, True, True, False,
    ]


def test_a_list_value_passes_an_in_test_when_it_holds_one_of_the_values():
    statuses = record("sig_repayments", "repayment_statuses", statuses=["paid", "paid", "failed"])

    assert needs.meets({"kind": "repayment_statuses", "where": {"statuses": {"in": ["failed"]}}}, [statuses])
    assert not needs.meets({"kind": "repayment_statuses", "where": {"statuses": {"in": ["late"]}}}, [statuses])


@pytest.mark.parametrize("value", [None, "1000", True, [1000]])
def test_a_bound_on_a_value_that_is_not_a_number_is_not_met(value):
    # A specialist is a black box: what it sends must fail the need, never break the check.
    sharply = {"kind": "weekly_inflow_series", "where": {"vs_compared_bp": {"lte": 2500}}}

    assert not needs.meets(sharply, [record("agg_x_inflows", "weekly_inflow_series", vs_compared_bp=value)])


def test_a_need_of_one_kind_is_not_met_by_a_record_of_another_with_the_same_field():
    stale = need("G-10a")  # a linked account with is_stale true

    assert not needs.meets(stale, [record("sig_accounts", "account_sync", is_stale=True)])


@pytest.mark.parametrize(
    ("guideline_id", "number", "data", "is_met"),
    [
        pytest.param("G-08a", 1, {"status": "failed", "paid_since": "yes", "repaid_after_days": policy.WITHIN_DAYS},
                     True, id="paid within days, on the line"),
        pytest.param("G-08a", 1, {"status": "failed", "paid_since": "yes", "repaid_after_days": policy.WITHIN_DAYS + 1},
                     False, id="paid a day too late"),
        pytest.param("G-08a", 1, {"status": "failed", "paid_since": "not_recorded", "repaid_after_days": 1},
                     False, id="days without a payment since"),
        pytest.param("G-07", 2, {"status": "failed"}, True, id="a failed repayment is trouble"),
        pytest.param("G-07", 2, {"status": "late"}, True, id="a late repayment is trouble"),
        pytest.param("G-07", 2, {"status": "paid"}, False, id="a paid repayment is not"),
        pytest.param("G-07", 1, {"income_trend": "stopped"}, True, id="stopped inflows are down"),
        pytest.param("G-07", 1, {"income_trend": "too_short_to_tell"}, False, id="a short dip is not"),
        pytest.param("G-14", 3, {"document_requests_for_income_in_window": policy.REPEAT_REQUESTS - 1}, True,
                     id="one request short of a repeat"),
        pytest.param("G-14", 3, {"document_requests_for_income_in_window": policy.REPEAT_REQUESTS}, False,
                     id="a repeat"),
        pytest.param("G-03", 2, {"now_vs_then_bp": policy.RECOVERED_BP}, True, id="cash recovered, on the line"),
        pytest.param("G-03", 2, {"now_vs_then_bp": policy.RECOVERED_BP - 1}, False, id="cash short of recovered"),
        pytest.param("G-05", 1, {"income_trend": "stopped", "vs_compared_bp": policy.SHARP_BP}, True,
                     id="sharp, on the line"),
        pytest.param("G-05", 1, {"income_trend": "stopped", "vs_compared_bp": policy.SHARP_BP + 1}, False,
                     id="not sharp enough"),
    ],
)
def test_each_line_of_a_need_sits_where_the_policy_puts_it(guideline_id, number, data, is_met):
    tested = need(guideline_id, number)

    assert needs.meets(tested, [record("x", tested["kind"], **data)]) is is_met


# --- What the records permit -------------------------------------------------------------------------------------


def test_a_recommendation_is_permitted_only_when_a_guideline_with_every_need_met_allows_it():
    assert needs.permitted(GUIDELINES, ledger("c_001", "transactions")) == {}  # G-01 still needs steady inflows
    assert needs.permitted(GUIDELINES, ledger("c_001", "transactions", "income")) == {"keep_limit": ["G-01"]}
    assert needs.permitted(GUIDELINES, ledger("c_003", "transactions", "income", "repayments")) == {
        "reduce_limit": ["G-06", "G-07"],
        "pause_card": ["G-07"],
        "request_documents": ["G-06"],
    }


def test_a_failed_repayment_keeps_a_guideline_that_does_not_cover_it_from_permitting_anything():
    without_card = ledger("c_010", "transactions", "income")

    assert all(needs.meets(n, without_card) for n in guideline("G-01")["needs"])
    assert needs.permitted(GUIDELINES, without_card) == {}
    assert needs.triggers(GUIDELINES, without_card) == [{**causes.MUST_ADDRESS[0], "record_ids": ["sig_repayments"]}]
    # With the repayments asked, the guideline that covers the failure carries its action. Keeping the limit is
    # still not permitted: G-01 alone passes over the failed repayment.
    assert needs.permitted(GUIDELINES, ledger("c_010", "transactions", "repayments", "income")) == {
        "pause_card": ["G-08b"]
    }


def test_two_guidelines_together_may_cover_what_one_does_not():
    records = ledger("c_111", "transactions", "repayments", "income")
    without_g08a = [g for g in GUIDELINES if g["id"] != "G-08a"]

    assert needs.permitted(GUIDELINES, records) == {"keep_limit": ["G-01", "G-08a"]}
    assert needs.permitted(without_g08a, records) == {}


@pytest.mark.parametrize("reconciled", [False, 0])
def test_books_that_do_not_match_keep_an_action_from_passing_over_them(reconciled):
    # c_001 meets G-01 in full. A reconciliation that disagrees must be covered, and only G-10b covers it.
    # Equality is not strict about bool: a 0 from a specialist still fires the trigger, so it cannot widen permitted.
    records = [
        {**item, "data": {"reconciled": reconciled}} if item["kind"] == "balance_reconciliation" else item
        for item in ledger("c_001", "transactions", "income", "reconciliation")
    ]

    assert all(needs.meets(n, records) for n in guideline("G-01")["needs"])
    assert needs.triggers(GUIDELINES, records) == [
        {**causes.MUST_ADDRESS[2], "record_ids": ["agg_c_001_reconciliation"]}
    ]
    assert needs.permitted(GUIDELINES, records) == {}


def test_stopped_inflows_ask_for_documents_and_are_no_reason_to_cut_the_limit():
    # c_106 (5.2): its trend label is stopped, so the shrinking of G-06 is not met and reduce_limit is not permitted.
    records = ledger("c_106", "transactions", "income")

    assert not needs.meets(need("G-06"), records)
    assert needs.meets(need("G-07"), records)  # down, for G-07: still nothing without a repayment in trouble
    assert needs.permitted(GUIDELINES, records) == {"request_documents": ["G-05"]}


def test_a_short_dip_with_a_late_repayment_asks_for_nothing():
    # c_004's records with one repayment late: "nothing else looks wrong" no longer holds.
    assert needs.permitted(GUIDELINES, ledger("c_095", "transactions", "income")) == {}


def test_a_transfer_to_the_customers_own_account_keeps_the_limit_only_when_it_is_most_of_the_fall():
    # c_002: the transfer is 5556, the payroll run 2778 (5.2).
    assert needs.permitted(GUIDELINES, ledger("c_002", "transactions", "income")) == {"keep_limit": ["G-04"]}
    transfer = transactions("c_002")[0]
    under = {**transfer, "data": {**transfer["data"], "share_of_fall_bp": policy.EXPLAINS_BP - 1}}
    elsewhere = {**transfer, "data": {**transfer["data"], "category": "transfer_other"}}
    assert needs.meets(need("G-04"), [transfer])
    assert not needs.meets(need("G-04"), [under])
    assert not needs.meets(need("G-04"), [elsewhere])


def test_triggers_apply_only_when_some_guideline_could_cover_them():
    records = ledger("c_010", "transactions")
    no_card_need = [g for g in GUIDELINES if all(n["kind"] != "card_repayment" for n in g["needs"])]

    assert needs.triggers(no_card_need, records) == []
    assert needs.triggers(GUIDELINES, records)[0]["record_ids"] == ["sig_repayments"]
    # A copy came back: the table itself is untouched.
    assert causes.MUST_ADDRESS[0].keys() == {"trigger", "covered_by", "says"}


def test_only_requests_for_weak_income_count_toward_a_repeat():
    # c_304 was asked twice to reconnect a feed, c_314 twice for weak income. Same records otherwise (8.1).
    assert needs.permitted(GUIDELINES, ledger("c_004", "transactions", "income")) == {"request_documents": ["G-14"]}
    assert needs.permitted(GUIDELINES, ledger("c_304", "transactions", "income")) == {"request_documents": ["G-14"]}
    assert needs.permitted(GUIDELINES, ledger("c_314", "transactions", "income")) == {}


def test_the_cited_records_that_miss_a_need_are_named_by_unmet():
    records, asked = case("c_010", "transactions", "repayments", "income")
    unpaid = guideline("G-08b")

    def unmet(g, cited):
        return needs.unmet(g, cited, guidelines=GUIDELINES, known=records, asked=asked)

    assert unmet(unpaid, []) == unpaid["needs"]
    assert unmet(unpaid, by_id(records, "rep_c_010_3")) == []
    # The payroll run alone is most of the fall, but nothing cited shows steady inflows.
    assert unmet(guideline("G-01"), by_id(records, "txn_c_010_01")) == [need("G-01", 2)]
    assert unmet(NO_NEEDS[0], []) == []


# --- None fits ---------------------------------------------------------------------------------------------------


def test_none_fits_is_met_only_when_nothing_acting_is_permitted_and_no_acting_guideline_has_an_open_need():
    nothing_fits = guideline("G-11")

    def fits(*steps):
        records, asked = case("c_314", *steps)
        return needs.unmet(nothing_fits, [], guidelines=GUIDELINES, known=records, asked=asked) == []

    # Nothing asked: transactions and income could still let a guideline act.
    assert not fits()
    assert needs.unsettled(GUIDELINES, *case("c_314")) == [
        {"tool": "AskCashFlow", "args": {"topic": "transactions"}},
        {"tool": "AskCashFlow", "args": {"topic": "income"}},
    ]
    assert not fits("transactions")
    assert needs.unsettled(GUIDELINES, *case("c_314", "transactions")) == [
        {"tool": "AskCashFlow", "args": {"topic": "income"}}
    ]
    assert fits("transactions", "income")
    assert needs.hand_over(GUIDELINES, *case("c_314", "transactions", "income")) == ["G-11"]
    assert needs.hand_over(GUIDELINES, *case("c_314", "transactions")) == []


def test_none_fits_is_not_met_while_an_action_is_permitted():
    records, asked = case("c_010", "transactions", "repayments", "income")

    assert needs.unsettled(GUIDELINES, records, asked) == []
    assert needs.unmet(guideline("G-11"), [], guidelines=GUIDELINES, known=records, asked=asked) == [need("G-11")]
    assert needs.hand_over(GUIDELINES, records, asked) == []


def test_an_open_need_of_a_payee_is_named_with_the_payment_to_ask_about():
    # The form rule 7b quotes (2.A): which question would settle it, in catalogue order.
    assert needs.unsettled(GUIDELINES, *case("c_011", "transactions")) == [
        {"tool": "AskCashFlow", "args": {"topic": "income"}},
        {"tool": "AskCashFlow", "args": {"topic": "payee_history", "record_id": "txn_c_011_01"}},
    ]


def test_a_topic_that_was_asked_and_failed_to_answer_settles_the_needs_that_rest_on_it():
    records = ledger("c_001", "transactions")
    asked = sent("transactions") + sent("income", outcome="no_answer")

    assert needs.state(need("G-01", 2), guideline("G-01"), records, asked) == "failed"
    assert needs.state(need("G-01", 2), guideline("G-01"), records, sent("transactions")) == "open"
    assert needs.unsettled(GUIDELINES, records, asked) == []
    # c_010: the repayments were asked and no answer came. G1 does not say they were not asked.
    no_repayments = sent("transactions", "income") + sent("repayments", outcome="no_answer")
    assert needs.code_gaps(GUIDELINES, ledger("c_010", "transactions", "income"), no_repayments) == []


def test_a_hand_over_guideline_with_every_need_met_carries_the_hand_over_next_to_none_fits():
    # c_012: the books do not match, and no guideline that allows an action fits (5.2).
    assert needs.hand_over(GUIDELINES, *case("c_012", "transactions", "income", "reconciliation")) == ["G-10b", "G-11"]
    assert needs.hand_over(NO_NEEDS, *case("c_012", "transactions", "income", "reconciliation")) == []


# --- Causes ------------------------------------------------------------------------------------------------------


def test_seasonal_dip_rests_on_this_years_record_and_never_on_an_earlier_review():
    history = [
        *own("c_007"),
        record("hist_seed_c_007_2025", "earlier_review", likely_cause="seasonal_dip", same_season=True, approved=True),
    ]
    alternatives = causes.CAUSE_NEEDS["seasonal_dip"]

    assert not any(needs.meets(n, history) for n in alternatives)
    assert any(needs.meets(n, ledger("c_007", "prior_year")) for n in alternatives)
    # No cause rests on a record code made: each comes from a question a specialist answered.
    assert {n["kind"] for alternatives in causes.CAUSE_NEEDS.values() for n in alternatives} <= set(causes.KIND_TOPIC)


def test_every_cause_but_no_clear_cause_rests_on_a_record():
    assert set(causes.CAUSE_NEEDS) == set(causes.CAUSES)
    assert "no_clear_cause" not in causes.CAUSE_NEEDS


# --- Switches ----------------------------------------------------------------------------------------------------

SWITCH_PATHS = [("c_010", "transactions"), ("c_010", "transactions", "repayments", "income"), ("c_005", "transactions"),
                ("c_314", "transactions", "income"), ("c_001",)]


def everything() -> dict[str, list]:
    found = {"permitted": [], "triggers": [], "unsettled": [], "hand_over": [], "code_gaps": []}
    for path in SWITCH_PATHS:
        records, asked = case(*path)
        found["permitted"].append(needs.permitted(GUIDELINES, records))
        found["triggers"].append(needs.triggers(GUIDELINES, records))
        found["unsettled"].append(needs.unsettled(GUIDELINES, records, asked))
        found["hand_over"].append(needs.hand_over(GUIDELINES, records, asked))
        found["code_gaps"].append(needs.code_gaps(GUIDELINES, records, asked))
    return found


@pytest.mark.parametrize(
    ("switch", "changes"),
    [
        # Permitted is what the check accepts, so the must-address rule reaches it through triggers.
        ("must_address", {"triggers", "permitted", "code_gaps"}),
        ("none_fits_is_checked", {"hand_over"}),
        # Rules 5 and 6 of 2.A live in the report check: nothing here reads them.
        ("every_cited_guideline_allows", set()),
        ("hand_over_cites_a_guideline", set()),
    ],
)
def test_a_switch_that_is_off_takes_its_rule_out_and_no_other(monkeypatch, switch, changes):
    on = everything()
    monkeypatch.setitem(policy.RULES, switch, False)
    off = everything()

    assert {name for name in on if on[name] != off[name]} == changes
    if switch == "must_address":
        assert all(gap["rule"] != "G1" for found in off["code_gaps"] for gap in found)
        # c_010: with the failed repayment no longer to be covered, G-01 keeps the limit next to G-08b's pause.
        assert off["permitted"][1] == {"keep_limit": ["G-01"], "pause_card": ["G-08b"]}
    if switch == "none_fits_is_checked":
        assert off["hand_over"][1] == ["G-11"]


# --- Code gaps ---------------------------------------------------------------------------------------------------


def test_code_finds_no_gap_when_the_guidelines_declare_no_needs():
    for path in PATHS:
        records, asked = case(*path)

        assert needs.code_gaps(NO_NEEDS, records, asked) == []
        assert needs.permitted(NO_NEEDS, records) == {}
        assert needs.triggers(NO_NEEDS, records) == []
        assert needs.unsettled(NO_NEEDS, records, asked) == []
        assert needs.hand_over(NO_NEEDS, records, asked) == []


def test_a_failed_repayment_nobody_asked_about_is_a_gap_that_names_the_card_side():
    found = needs.code_gaps(GUIDELINES, *case("c_010", "transactions"))

    assert found[0] == {
        "rule": "G1",
        "text": "A repayment failed (sig_repayments). Not yet asked: AskCreditBehavior, topic repayments.",
        "ask": {"tool": "AskCreditBehavior", "args": {"topic": "repayments"}},
    }
    # The payroll run and the tax payment half support G-01, which still needs the income.
    income = {"tool": "AskCashFlow", "args": {"topic": "income"}}
    assert [(gap["rule"], gap["ask"]) for gap in found[1:]] == [("G2", income)]


def test_a_linked_account_that_is_behind_is_a_gap_until_the_accounts_are_asked():
    assert gaps("c_005", "transactions")[0] == ("G1", "AskCashFlow, topic accounts.")
    assert needs.code_gaps(GUIDELINES, *case("c_005", "transactions"))[0]["text"].startswith(
        "A linked account is behind (sig_accounts)."
    )
    assert gaps("c_005", "transactions", "accounts") == []
    assert needs.permitted(GUIDELINES, ledger("c_005", "transactions", "accounts")) == {"request_documents": ["G-10a"]}


def test_a_half_supported_guideline_names_the_topic_that_was_not_asked():
    assert needs.code_gaps(GUIDELINES, *case("c_009", "transactions", "income")) == [
        {
            "rule": "G2",
            "text": "A guideline is partly supported. "
            "Not yet asked: AskCashFlow, topic payee_history, record_id txn_c_009_01.",
            "ask": {"tool": "AskCashFlow", "args": {"topic": "payee_history", "record_id": "txn_c_009_01"}},
        }
    ]
    # Once the history is in, the payment is in line with it and the limit can be kept.
    assert gaps("c_009", "transactions", "income", ("payee_history", "txn_c_009_01")) == []
    with_history = ledger("c_009", "transactions", "income", ("payee_history", "txn_c_009_01"))
    assert needs.permitted(GUIDELINES, with_history) == {"keep_limit": ["G-02"]}


def test_the_history_of_a_running_cost_does_not_settle_the_vendor_payment_g02_is_about():
    # Bank keeps a history for every payee now, and a running cost's has no vs_usual_bp. Asked about first, it must
    # leave G-02 open, and the gap must still name the vendor's payment. Before, it marked G-02 failed.
    records, asked = case("c_009", "transactions", "income")
    running_cost = record("txn_c_009_05", "bank_transaction", amount_cents=-1100000, category="operating",
                          share_of_fall_bp=bp(1100000, 15200000))
    its_history = record("agg_c_009_payee_history_05", "payee_history", about="txn_c_009_05", n_prior_payments=13,
                         amount_cents=1100000, usual_top_cents=1100000)
    records = [*records, running_cost, its_history]
    asked = [*asked, *sent(("payee_history", "txn_c_009_05"), returned=[running_cost["id"], its_history["id"]])]

    assert needs.state(need("G-02", 2), guideline("G-02"), records, asked) == "open"
    assert [gap["ask"] for gap in needs.code_gaps(GUIDELINES, records, asked)] == [
        {"tool": "AskCashFlow", "args": {"topic": "payee_history", "record_id": "txn_c_009_01"}}
    ]


def test_a_guideline_whose_only_met_need_is_the_cases_own_record_is_not_called_half_supported():
    records, asked = case("c_001")
    healthy = guideline("G-04")  # every repayment paid is met by sig_repayments before anything is asked

    assert [needs.state(n, healthy, records, asked) for n in healthy["needs"]] == ["open", "open", "met"]
    assert gaps("c_001") == [
        ("G3", "AskCashFlow, topic transactions."),
        ("G3", "AskCashFlow, topic income."),
        ("G3", "AskCashFlow, topic reconciliation."),
    ]


def test_no_gap_text_names_a_guideline():
    texts = [gap["text"] for path in PATHS for gap in needs.code_gaps(GUIDELINES, *case(*path))]

    assert texts  # the paths do find gaps
    assert [text for text in texts if re.search(r"G-\d", text)] == []


STAND_INS = {"AskCashFlow": mock.cash_flow, "AskCreditBehavior": mock.credit_behavior}


def stand_in_walk(customer_id: str, signal: dict, week: tuple) -> list[list[dict]]:
    """The code gaps of one case, from the real signal and the real stand-ins, before and after each question.

    Every topic in catalogue order, then the payee's history of every payment the bank side listed.
    """
    state = {"facts": [*facts.signal_facts(signal, {"start": str(week[0]), "end": str(week[1])}),
                       *facts.history_facts([], signal)], "answers": []}
    asked = []
    found = [needs.code_gaps(GUIDELINES, list(facts.ledger(state).values()), asked)]

    def put(tool: str, topic: str, **focus) -> None:
        request = InvestigateRequest(case_id="case_walk", customer_id=customer_id, question="What happened?",
                                     window_start=str(week[0]), window_end=str(week[1]), topic=topic, **focus)
        response = STAND_INS[tool](request).model_dump(mode="json")
        state["answers"].append({"agent": tool, "question": "What happened?", "response": response})
        asked.append({"tool": tool, "agent": tool, "topic": topic, "separates": ["revenue_loss"],
                      "tests": causes.TOPIC_TESTS[topic], "outcome": "answered",
                      "returned": [item["id"] for item in response["evidence"]], **focus})
        found.append(needs.code_gaps(GUIDELINES, list(facts.ledger(state).values()), asked))

    for tool, topics in causes.TOPICS.items():
        for topic in topics:
            if (tool, topic) not in causes.NEEDS_RECORD:
                put(tool, topic)
    for item in list(facts.ledger(state).values()):
        if item["kind"] == "bank_transaction":
            put("AskCashFlow", "payee_history", record_id=item["id"])
    return found


def test_no_gap_text_code_writes_states_a_figure_on_any_customer_s_walk(monkeypatch):
    # A gap text goes to the investigator, which may repeat it, so it holds no figure even in loose mode (0.3, 5).
    monkeypatch.setenv("SHARED_CUSTOMERS_FILE", os.pathsep.join(str(path) for path in DATA_FILES))
    monkeypatch.syspath_prepend(str(ROOT / "eval"))
    import signals

    today, customers = signals.customers()
    week = mock._data()["week"]
    refused, rules = {}, set()
    for customer_id, customer in customers.items():
        for gaps_found in stand_in_walk(customer_id, signals.signal_for(customer, today), week):
            for gap in gaps_found:
                rules.add(gap["rule"])
                if found := figures.not_backed(gap["text"], set(), loose=True):
                    refused[gap["text"]] = found

    assert customers
    assert rules == {"G1", "G2", "G3"}  # the walks reach every rule, so every kind of text is read
    assert refused == {}


def test_code_never_names_a_question_that_was_already_sent():
    for path in PATHS:
        records, asked = case(*path)
        for gap in needs.code_gaps(GUIDELINES, records, asked):
            args = gap["ask"]["args"]
            named = (gap["ask"]["tool"], args["topic"], args.get("record_id"))

            assert [e for e in asked if (e["tool"], e["topic"], e.get("record_id")) == named] == [], (path, gap)
            assert args["topic"] in causes.TOPICS[gap["ask"]["tool"]]
            if (gap["ask"]["tool"], args["topic"]) in causes.NEEDS_RECORD:
                assert by_id(records, args["record_id"])[0]["kind"] == "bank_transaction"


def test_two_rules_that_name_the_same_question_give_one_gap(monkeypatch):
    # c_110: the failed repayment is not covered (G1), and G-08a, half supported by steady inflows, needs it too (G2).
    assert gaps("c_110", "transactions", "income") == [("G1", "AskCreditBehavior, topic repayments.")]

    # G2 names it on its own once G1 is off. G-01 goes too: with nothing to cover it would keep the limit.
    monkeypatch.setitem(policy.RULES, "must_address", False)
    without_g01 = [g for g in GUIDELINES if g["id"] != "G-01"]
    assert gaps("c_110", "transactions", "income", guidelines=without_g01) == [
        ("G2", "AskCreditBehavior, topic repayments.")
    ]


def test_a_guideline_with_a_need_known_to_fail_asks_for_nothing():
    # c_007 and c_307 had the same review a year ago. For c_307 cash did not come back, so G-03 is dead and
    # nothing asks for last year's record. Both still ask for the reconciliation that could hand the case over.
    path = ("transactions", "income", ("payee_history", "txn_c_007_01"))
    assert gaps("c_007", *path) == [
        ("G3", "AskCashFlow, topic prior_year."),
        ("G3", "AskCashFlow, topic reconciliation."),
    ]
    assert gaps("c_307", "transactions", "income", ("payee_history", "txn_c_307_01")) == [
        ("G3", "AskCashFlow, topic reconciliation."),
    ]
    # c_097: the transfer meets G-04's first need, but a late repayment fails its third. It is not half supported.
    healthy = guideline("G-04")
    assert [needs.state(n, healthy, *case("c_097", "transactions")) for n in healthy["needs"]] == [
        "met", "open", "failed",
    ]
    assert gaps("c_097", "transactions") == [
        ("G3", "AskCashFlow, topic income."),
        ("G3", "AskCreditBehavior, topic repayments."),
        ("G3", "AskCashFlow, topic reconciliation."),
    ]
    # c_098: the history of the larger payment is out of line. One payee history fails G-02, and the smaller
    # payment is not asked about.
    after_history = ("transactions", "income", ("payee_history", "txn_c_098_01"))
    assert needs.state(need("G-02", 2), guideline("G-02"), *case("c_098", *after_history)) == "failed"
    assert gaps("c_098", *after_history) == [("G3", "AskCashFlow, topic reconciliation.")]


def test_the_signal_already_rules_out_a_repayment_need_when_every_repayment_was_paid():
    late_or_failed, failed_unpaid = need("G-07", 2), need("G-08b")
    all_paid, one_late = case("c_001"), case("c_003")

    assert needs.state(late_or_failed, guideline("G-07"), *all_paid) == "failed"
    assert needs.state(failed_unpaid, guideline("G-08b"), *all_paid) == "failed"
    assert needs.state(late_or_failed, guideline("G-07"), *one_late) == "open"
    assert needs.state(failed_unpaid, guideline("G-08b"), *one_late) == "failed"  # late is not failed
    # Likewise a stale account, when no account is behind.
    assert needs.state(need("G-10a"), guideline("G-10a"), *all_paid) == "failed"
    assert needs.state(need("G-10a"), guideline("G-10a"), *case("c_005")) == "open"


def test_a_topic_that_was_asked_and_gave_nothing_is_not_asked_for_again():
    # c_007's payee history is not in the data: asked once, it is not named again.
    asked_payee = gaps("c_007", "transactions", "income", ("payee_history", "txn_c_007_01"))
    assert all("payee_history" not in named for _, named in asked_payee)
    # An answer that never came settles the topic too.
    records = ledger("c_001", "transactions")
    no_income = needs.code_gaps(GUIDELINES, records, sent("transactions") + sent("income", outcome="no_answer"))
    assert [gap["ask"]["args"]["topic"] for gap in no_income] == ["reconciliation"]


def test_a_refused_question_does_not_count_as_asked():
    records = ledger("c_001", "transactions")
    refused = sent("transactions") + sent("income", outcome="refused")

    assert needs.code_gaps(GUIDELINES, records, refused) == needs.code_gaps(GUIDELINES, records, sent("transactions"))
    assert [gap["ask"]["args"]["topic"] for gap in needs.code_gaps(GUIDELINES, records, refused)] == ["income"]


def test_a_question_narrowed_to_other_dates_or_to_one_account_settles_nothing():
    # c_001: transactions asked for another span came back empty (the records cover the data week only, 4).
    records = ledger("c_001", "income", "reconciliation")
    asked = sent("transactions", start="2026-08-01", end=WINDOW[1]) + sent("income", "reconciliation")
    transactions = {"tool": "AskCashFlow", "args": {"topic": "transactions"}}

    assert needs.state(need("G-01"), guideline("G-01"), records, asked) == "open"
    assert needs.unsettled(GUIDELINES, records, asked) == [transactions]
    assert needs.hand_over(GUIDELINES, records, asked) == []  # a thin investigation is not "none fits"
    assert transactions in [gap["ask"] for gap in needs.code_gaps(GUIDELINES, records, asked)]

    # c_005: one of two linked accounts is behind, and the question named the other one.
    reporting = record("acct_c005_b", "linked_account", active=True, days_stale=0, is_stale=False)
    records = [*ledger("c_005", "transactions", "income"), reporting]
    asked = [
        *sent("transactions"),
        *sent("accounts", account_id="acct_c005_b", returned=["acct_c005_b"]),
        *sent("income", "reconciliation"),
    ]
    accounts = {"tool": "AskCashFlow", "args": {"topic": "accounts"}}

    assert needs.state(need("G-10a"), guideline("G-10a"), records, asked) == "open"
    assert needs.unsettled(GUIDELINES, records, asked) == [accounts]
    assert needs.hand_over(GUIDELINES, records, asked) == []
    assert [gap["ask"] for gap in needs.code_gaps(GUIDELINES, records, asked)] == [accounts]


def test_dates_that_are_the_review_window_ask_the_whole_topic():
    records = ledger("c_001", "income", "reconciliation")
    plain = sent("transactions", "income", "reconciliation")
    dated = sent("transactions", start=WINDOW[0], end=WINDOW[1]) + sent("income", "reconciliation")

    # Asked whole and nothing came back: the topic is settled either way.
    assert needs.unsettled(GUIDELINES, records, dated) == needs.unsettled(GUIDELINES, records, plain) == []
    assert needs.hand_over(GUIDELINES, records, dated) == needs.hand_over(GUIDELINES, records, plain) == ["G-11"]


def test_what_a_narrowed_question_returned_still_counts():
    # Asked about the account that stopped reporting, the answer meets the need on its own.
    stale = record("acct_c005_a", "linked_account", active=True, days_stale=5, is_stale=True)
    records = [*ledger("c_005", "transactions"), stale]
    asked = sent("transactions") + sent("accounts", account_id="acct_c005_a", returned=["acct_c005_a"])

    assert needs.permitted(GUIDELINES, records) == {"request_documents": ["G-10a"]}
    assert needs.code_gaps(GUIDELINES, records, asked) == []

    # A card question with dates still brought the repayments: nothing says they were not looked at.
    records = ledger("c_010", "transactions", "repayments")
    repayments = [item["id"] for item in answer("c_010", "repayments")]
    asked = sent("transactions") + sent("repayments", start="2026-08-01", end=WINDOW[1], returned=repayments)

    assert needs.code_gaps(GUIDELINES, records, asked) == []


def test_the_payee_gap_names_the_largest_vendor_payment_not_asked_about_yet():
    # c_099 is made up: $20,000 and then $30,000 to vendors paid before. Neither history is in the data.
    first, second = "txn_c_099_01", "txn_c_099_02"

    # Before the transactions there is no payment to ask about: the need is open, not failed, and asks nothing yet.
    assert needs.state(need("G-02", 2), guideline("G-02"), *case("c_099")) == "open"

    assert gaps("c_099", "transactions", "income") == [
        ("G2", f"AskCashFlow, topic payee_history, record_id {second}.")
    ]
    assert gaps("c_099", "transactions", "income", ("payee_history", second)) == [
        ("G2", f"AskCashFlow, topic payee_history, record_id {first}.")
    ]
    # Asked about both and neither came back: the need is settled and nothing names it again.
    after_both = gaps("c_099", "transactions", "income", ("payee_history", second), ("payee_history", first))
    assert all("payee_history" not in named for _, named in after_both)
    # c_094: the payroll run is larger, but only a payment to a vendor paid before is G-02's to ask a history about.
    assert gaps("c_094", "transactions", "income") == [
        ("G2", "AskCashFlow, topic payee_history, record_id txn_c_094_01.")
    ]


def test_code_stops_looking_once_an_action_is_permitted():
    assert needs.permitted(GUIDELINES, ledger("c_008", "transactions")) == {"request_documents": ["G-09"]}
    assert gaps("c_008", "transactions") == []
    # Here a guideline is still half supported, and code asks nothing all the same: G-07 needs the repayments
    # (c_003), G-02 the payee's history (c_007, whose seeded review lets G-03 keep the limit).
    assert needs.permitted(GUIDELINES, ledger("c_003", "transactions", "income")) == {
        "reduce_limit": ["G-06"],
        "request_documents": ["G-06"],
    }
    assert gaps("c_003", "transactions", "income") == []
    assert needs.permitted(GUIDELINES, ledger("c_007", "transactions", "income", "prior_year")) == {
        "keep_limit": ["G-03"]
    }
    assert gaps("c_007", "transactions", "income", "prior_year") == []


def test_when_nothing_fits_code_asks_for_what_could_still_decide_it_one_topic_per_gap_and_three_at_most():
    assert gaps("c_012", "transactions", "income") == [("G3", "AskCashFlow, topic reconciliation.")]
    assert needs.code_gaps(GUIDELINES, *case("c_012", "transactions", "income"))[0]["text"] == (
        "No guideline's needs are met yet. Not yet asked: AskCashFlow, topic reconciliation."
    )
    # c_003 before any question: the actions could still turn on four topics. The hand-over's comes last and is cut.
    assert gaps("c_003") == [
        ("G3", "AskCashFlow, topic transactions."),
        ("G3", "AskCashFlow, topic income."),
        ("G3", "AskCreditBehavior, topic repayments."),
    ]


@pytest.mark.parametrize(
    ("cid", "path", "carried_by"),
    [
        ("c_012", ("transactions", "income", "reconciliation"), ["G-10b", "G-11"]),
        ("c_011", ("transactions", ("payee_history", "txn_c_011_01"), "income", "reconciliation"), ["G-11"]),
        (
            "c_307",
            ("transactions", "income", ("payee_history", "txn_c_307_01"), "prior_year", "reconciliation"),
            ["G-11"],
        ),
        ("c_314", ("transactions", "income", "reconciliation"), ["G-11"]),
    ],
)
def test_with_no_open_need_left_code_finds_no_gap(cid, path, carried_by):
    # The listed paths of the hand-over cases (8.1): at the end code finds nothing, and the hand-over is carried.
    assert gaps(cid, *path) == []
    assert needs.hand_over(GUIDELINES, *case(cid, *path)) == carried_by
    assert needs.hand_over(GUIDELINES, *case(cid, *path[:1])) == []


@pytest.mark.parametrize("unpaid", [["no", "not_recorded"], ["no"]])
def test_c_010_after_transactions_repayments_and_income_has_no_code_gap_under_either_reading_of_unpaid(unpaid):
    read = copy.deepcopy(GUIDELINES)
    guideline("G-08b", read)["needs"][0]["where"]["paid_since"] = {"in": unpaid}
    records, asked = case("c_010", "transactions", "repayments", "income")

    assert needs.code_gaps(read, records, asked) == []
    # Decision 1: with "not recorded" read as unpaid the card is paused, otherwise a person decides (5.2).
    assert needs.permitted(read, records) == ({"pause_card": ["G-08b"]} if "not_recorded" in unpaid else {})
    assert needs.hand_over(read, records, asked) == ([] if "not_recorded" in unpaid else ["G-11"])


def test_after_transactions_alone_c_010_asks_for_the_repayments_and_the_income():
    # The walk of section 3, one question in.
    assert gaps("c_010", "transactions") == [
        ("G1", "AskCreditBehavior, topic repayments."),
        ("G2", "AskCashFlow, topic income."),
    ]


# --- The plan ----------------------------------------------------------------------------------------------------


def test_the_fixed_plan_is_every_topic_that_needs_no_focus():
    planned = [(step["tool"], step["args"]["topic"]) for step in plans.FIXED_PLAN]
    catalogue = [(tool, topic) for tool, topics in causes.TOPICS.items() for topic in topics]

    assert len(planned) == len(set(planned))
    assert set(planned) == set(catalogue) - causes.NEEDS_RECORD
    assert [tool for tool, _ in planned] == sorted((tool for tool, _ in planned), key=list(causes.TOPICS).index)
    for step in plans.FIXED_PLAN:
        args = step["args"]
        assert set(args) == {"topic", "question", "separates"}
        assert args["question"].strip()
        # A question claims to separate only causes its topic can bear on.
        assert args["separates"] and set(args["separates"]) <= set(causes.TOPIC_TESTS[args["topic"]])
    # The order and the claims of 8.4: the fixed arm asks exactly this, and fixed_rules adds at most four.
    assert [(step["tool"], step["args"]["topic"], step["args"]["separates"]) for step in plans.FIXED_PLAN] == [
        ("AskCashFlow", "transactions",
         ["payroll_or_tax", "planned_vendor_payment", "funds_moved_out_of_view", "unexplained_outflow"]),
        ("AskCashFlow", "income", ["revenue_loss"]),
        ("AskCashFlow", "reconciliation", ["insufficient_data", "unexplained_outflow"]),
        ("AskCashFlow", "accounts", ["insufficient_data"]),
        ("AskCashFlow", "prior_year", ["seasonal_dip"]),
        ("AskCreditBehavior", "repayments", ["revenue_loss"]),
        ("AskCreditBehavior", "paying_account", ["funds_moved_out_of_view"]),
        ("AskCreditBehavior", "card_use", ["revenue_loss"]),
    ]
    assert plans.MAX_FOLLOW_UPS == 4


def test_a_follow_up_is_a_whole_question_on_the_topic_the_gap_named():
    for tool, topic in [(tool, topic) for tool, topics in causes.TOPICS.items() for topic in topics]:
        ask = {"tool": tool, "args": {"topic": topic}}
        if (tool, topic) in causes.NEEDS_RECORD:
            ask["args"]["record_id"] = "txn_c_009_01"
        follow = plans.follow_up(ask)

        assert follow["tool"] == tool
        assert follow["args"]["topic"] == topic and follow["args"].get("record_id") == ask["args"].get("record_id")
        assert follow["args"]["question"].strip()
        assert follow["args"]["separates"] == causes.TOPIC_TESTS[topic]
        assert "question" not in ask["args"]  # the gap's own ask is left as it was

    plans.follow_up({"tool": "AskCashFlow", "args": {"topic": "income"}})["args"]["separates"].append("seasonal_dip")
    assert causes.TOPIC_TESTS["income"] == ["revenue_loss"]


# --- The guideline model -----------------------------------------------------------------------------------------


def test_every_guideline_declares_its_needs_and_they_go_to_the_graph_as_plain_data():
    assert [g["id"] for g in GUIDELINES if not g["needs"]] == []
    assert json.loads(json.dumps(GUIDELINES)) == GUIDELINES
    # A bool, not 1 or 0: True == 1 in Python, so only the identity tells them apart.
    assert need("G-10a")["where"]["is_stale"] is True
    assert need("G-10b")["where"]["reconciled"] is False
    assert need("G-08b")["where"]["paid_since"] == {"in": list(policy.UNPAID_MEANS)}


def test_a_reading_of_unpaid_set_in_policy_is_the_one_the_guidelines_use(monkeypatch):
    # The owner's other reading of "has not been paid" (decision 1), set after import as a test or a switch would.
    from tests.test_signals import SIGNAL  # c_010

    monkeypatch.setenv("SHARED_CUSTOMERS_FILE", os.pathsep.join(str(path) for path in DATA_FILES))
    request = InvestigateRequest(case_id="case_unpaid", customer_id="c_010", question="How were repayments made?",
                                 window_start=WINDOW[0], window_end=WINDOW[1], topic="repayments")
    repayments = [item.model_dump() for item in mock.credit_behavior(request).evidence]
    records = [*facts.signal_facts(SIGNAL, {"start": WINDOW[0], "end": WINDOW[1]}), *repayments]
    assert [item["data"]["paid_since"] for item in repayments if item["data"]["status"] == "failed"] == ["not_recorded"]
    assert needs.permitted(guidelines.load(), records) == {"pause_card": ["G-08b"]}
    before = next(g for g in guidelines.GUIDELINES if g.id == "G-08b").needs[0].where["paid_since"]
    assert before == {"in": ["no", "not_recorded"]}

    monkeypatch.setattr(policy, "UNPAID_MEANS", ("no",))
    read = guidelines.load()

    assert guideline("G-08b", read)["needs"][0]["where"]["paid_since"] == {"in": ["no"]}
    assert needs.permitted(read, records) == {}
    assert next(g for g in guidelines.GUIDELINES if g.id == "G-08b").needs[0].where["paid_since"] == {"in": ["no"]}


def test_a_share_line_set_in_policy_is_the_one_the_guidelines_use(monkeypatch):
    tax = transactions("c_004")[0]  # exactly the line: half the fall
    assert needs.meets(guideline("G-01", guidelines.load())["needs"][0], [tax])
    assert guidelines.GUIDELINES[0].needs[0].share_at_least_bp == 5000

    monkeypatch.setattr(policy, "EXPLAINS_BP", policy.EXPLAINS_BP + 1)
    read = guidelines.load()

    assert [guideline(g, read)["needs"][0]["share_at_least_bp"] for g in ("G-01", "G-04")] == [5001, 5001]
    assert not needs.meets(guideline("G-01", read)["needs"][0], [tax])
    assert guidelines.GUIDELINES[0].needs[0].share_at_least_bp == 5001


# Every policy value the guidelines are built from: a value no default holds, and where it must show.
# (name, value set, guideline, need number, field of the need, what that field must then hold)
POLICY_IN_GUIDELINES = [
    ("EXPLAINS_BP", 4321, "G-01", 1, "share_at_least_bp", 4321),
    ("EXPLAINS_BP", 4321, "G-04", 1, "share_at_least_bp", 4321),
    ("LARGE_PART_BP", 4322, "G-02", 1, "share_at_least_bp", 4322),
    ("LARGE_PART_BP", 4322, "G-09", 1, "share_at_least_bp", 4322),
    ("IN_LINE_BP", 4323, "G-02", 2, "where", {"vs_usual_bp": {"lte": 4323}}),
    ("SAME_FALL_BP", (4324, 14325), "G-03", 1, "where", {"window_vs_prior_out_bp": {"gte": 4324, "lte": 14325}}),
    ("RECOVERED_BP", 4326, "G-03", 2, "where", {"now_vs_then_bp": {"gte": 4326}}),
    ("SHARP_BP", 4327, "G-05", 1, "where",
     {"income_trend": {"in": ["stopped", "shrinking"]}, "vs_compared_bp": {"lte": 4327}}),
    ("WITHIN_DAYS", 43, "G-08a", 1, "where",
     {"status": "failed", "paid_since": "yes", "repaid_after_days": {"lte": 43}}),
    ("UNPAID_MEANS", ("unpaid_sentinel",), "G-08b", 1, "where",
     {"status": "failed", "paid_since": {"in": ["unpaid_sentinel"]}}),
    ("REPEAT_REQUESTS", 44, "G-14", 3, "where", {"document_requests_for_income_in_window": {"lte": 43}}),
]


def test_the_policy_table_above_covers_every_policy_value_the_guidelines_are_built_from():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(guidelines._built))
    read = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "policy"
    }

    assert read == {name for name, *_ in POLICY_IN_GUIDELINES}


@pytest.mark.parametrize(("name", "value", "guideline_id", "number", "field", "holds"), POLICY_IN_GUIDELINES,
                         ids=[f"{row[0]} in {row[2]}" for row in POLICY_IN_GUIDELINES])
def test_every_policy_value_is_read_when_the_guidelines_are_built(monkeypatch, name, value, guideline_id, number,
                                                                  field, holds):
    monkeypatch.setattr(policy, name, value)

    loaded = guideline(guideline_id, guidelines.load())["needs"][number - 1]
    built = next(g for g in guidelines.GUIDELINES if g.id == guideline_id).needs[number - 1].model_dump()

    assert loaded[field] == built[field] == holds


def test_every_build_of_the_guidelines_owns_its_needs(monkeypatch):
    # A need two guidelines share (steady inflows, every repayment paid) is made anew for each build:
    # a change to one build's objects never reaches the next.
    first = {g.id: g for g in guidelines.GUIDELINES}
    monkeypatch.setitem(first["G-01"].needs[1].where, "income_trend", "changed")
    monkeypatch.setitem(first["G-04"].needs[2].where, "all_paid", False)

    again = {g.id: g for g in guidelines.GUIDELINES}
    assert again["G-08a"].needs[1].where == {"income_trend": "steady"}
    assert again["G-14"].needs[1].where == {"all_paid": True}
    assert guideline("G-01", guidelines.load())["needs"][1]["where"] == {"income_trend": "steady"}


def test_the_only_guideline_that_rests_on_no_record_is_g_11():
    assert [g["id"] for g in GUIDELINES for n in g["needs"] if n["kind"] == "none_fits"] == ["G-11"]
    assert guideline("G-11")["allows"] == ["needs_human"]


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param({"where": {"category": {"in": "payroll"}}}, id="in on a string, which would match by substring"),
        pytest.param({"where": {"share_of_fall_bp": {"gte": "5000"}}}, id="a bound that is not a number"),
        pytest.param({"where": {"share_of_fall_bp": {"gte": True}}}, id="a bound that is a bool"),
        pytest.param({"where": {"category": ["payroll", "tax"]}}, id="a list to equal"),
        pytest.param({"where": {"is_stale": {}}}, id="a test that tests nothing"),
        pytest.param({"share_at_least_bp": 0}, id="a share of nothing"),
        pytest.param({"share_at_least_bp": 10001}, id="a share over the whole"),
        pytest.param({"says": ""}, id="no words"),
        pytest.param({"threshold": 5000}, id="a field the model does not have"),
    ],
)
def test_a_need_code_could_not_check_cannot_be_written(broken):
    good = Guideline.model_validate(guidelines.GUIDELINES[0].model_dump()).needs[0].model_dump()

    Need.model_validate(good)
    with pytest.raises(ValidationError):
        Need.model_validate({**good, **broken})


def test_what_a_need_or_a_guideline_says_states_no_figure():
    # The model is shown these words. A threshold in them could be copied into a report as a fact (1.6, 6.1).
    from app.graph import figures

    texts = [n["says"] for g in GUIDELINES for n in g["needs"]]
    texts += [g[field] for g in GUIDELINES for field in ("title", "when", "note")]
    texts += [n["says"] for alternatives in causes.CAUSE_NEEDS.values() for n in alternatives]
    texts += [entry["says"] for entry in causes.MUST_ADDRESS]
    texts += [step["args"]["question"] for step in plans.FIXED_PLAN]
    texts += [plans.follow_up({"tool": "AskCashFlow", "args": {"topic": "payee_history"}})["args"]["question"]]

    assert {text: found for text in texts if (found := figures.figures(text))} == {}
