"""transactions: the cash at both ends of the window, its itemised transactions, and the payment summary."""

import pytest

from tests.conftest import call, evidence_by_id


def _ask(client, headers, cid, **params):
    response = call(client, headers, cid, "transactions", **params)
    assert response.status_code == 200, response.text
    return response.json()


def test_c002_lists_every_event_of_its_week_with_the_balance_and_the_summary(client, auth_headers):
    body = _ask(client, auth_headers, "c_002")
    events = [f"txn_c_002_{n:02d}" for n in range(1, 12)]
    assert sorted(body["data"]["transaction_ids"]) == events
    assert body["data"]["truncated"] is False
    evidence = [r["id"] for r in body["evidence"]]
    assert evidence[0] == "bal_c_002" and sorted(evidence[1:-1]) == events and evidence[-1] == "agg_c_002_payments"
    assert evidence_by_id(body)["bal_c_002"]["data"] == {
        "cash_week_ago_cents": 120_000_000, "cash_now_cents": 48_000_000, "change_cents": -72_000_000,
    }


def test_the_weeks_before_list_receipts_and_running_costs_never_a_filler(client, auth_headers):
    """Regression: the seeder's "other" filler was listed, and the rules read it as an unexplained outflow."""
    body = _ask(client, auth_headers, "c_003", start="2026-08-01", end="2026-11-10", limit=500)
    listed = [r for r in body["evidence"] if r["kind"] == "bank_transaction"]
    earlier = [r for r in listed if r["id"].startswith("txn_c_003_w")]
    assert earlier and len(listed) == 7 + len(earlier)
    assert {r["data"]["category"] for r in earlier} == {"income", "operating"}
    # A payment before the review window has no share of its fall.
    assert not [r for r in earlier if "share_of_fall_bp" in r["data"]]


def test_the_payment_summary_sets_every_payment_against_the_fall(client, auth_headers):
    """Regression: hidden everyday rows left c_003's whole fall to no listed payment. Now every payment is
    listed, and what went out is more than the fall because income came in."""
    summary = evidence_by_id(_ask(client, auth_headers, "c_003"))["agg_c_003_payments"]["data"]
    assert summary == {
        "listed_count": 6,
        "listed_out_cents": 17_000_000,
        "listed_share_bp": 12143,
        "listed_in_cents": 3_000_000,
        "by_category": {"operating": {"count": 6, "out_cents": 17_000_000, "share_bp": 12143}},
    }


def test_a_transaction_carries_its_kind_its_account_and_its_share(client, auth_headers):
    """Regression: counterparty_kind and to_account were never emitted."""
    record = evidence_by_id(_ask(client, auth_headers, "c_106"))["txn_c_106_02"]
    assert record["data"] == {
        "date": record["data"]["date"],
        "amount_cents": -3_000_000,
        "category": "transfer_own_account",
        "counterparty_kind": "own_account_other_bank",
        "to_account": "acct_external_new_c106",
        "share_of_fall_bp": 2500,
    }


def test_no_payee_name_and_no_authors_note_reach_the_evidence(client, auth_headers):
    for cid in ("c_007", "c_009", "c_011"):
        for record in _ask(client, auth_headers, cid)["evidence"]:
            assert "counterparty_display" not in record["data"]
    c_007 = evidence_by_id(_ask(client, auth_headers, "c_007"))["txn_c_007_01"]["data"]
    assert "memo" not in c_007
    c_009 = evidence_by_id(_ask(client, auth_headers, "c_009"))["txn_c_009_01"]["data"]
    assert "IGNORE PREVIOUS INSTRUCTIONS" in c_009["memo"]


def test_a_stale_account_means_no_shares_and_no_summary(client, auth_headers):
    body = _ask(client, auth_headers, "c_005")
    ids = [r["id"] for r in body["evidence"]]
    assert ids[0] == "bal_c_005" and sorted(ids[1:]) == [f"txn_c_005_{n:02d}" for n in range(1, 9)]
    assert not [r for r in body["evidence"] if "share_of_fall_bp" in r["data"]]
    assert evidence_by_id(body)["bal_c_005"]["data"]["change_cents"] == -35_000_000
    assert "did not report" in body["data"]["not_available"][0]


def test_filters_choose_what_is_listed_never_what_a_record_says(client, auth_headers):
    whole = evidence_by_id(_ask(client, auth_headers, "c_002"))
    payroll = _ask(client, auth_headers, "c_002", category="payroll")
    assert payroll["data"]["transaction_ids"] == ["txn_c_002_02"]
    large = _ask(client, auth_headers, "c_002", min_abs_amount_cents=15_000_000)
    assert large["data"]["transaction_ids"] == ["txn_c_002_01", "txn_c_002_02"]
    operating = _ask(client, auth_headers, "c_002", category="operating")
    assert sorted(operating["data"]["transaction_ids"]) == [f"txn_c_002_{n:02d}" for n in range(7, 12)]
    one = _ask(client, auth_headers, "c_002", limit=1)
    assert one["data"]["transaction_ids"] == ["txn_c_002_01"] and one["data"]["truncated"] is True
    for body in (payroll, large, operating, one):
        for record in body["evidence"]:
            assert record == whole[record["id"]]


def test_another_window_gets_ids_of_its_own(client, auth_headers):
    # c_002's eleven events fall on the 4th to the 10th, then the 4th to the 7th; this window holds the 4th and 5th.
    body = _ask(client, auth_headers, "c_002", start="2026-11-03", end="2026-11-05")
    ids = [r["id"] for r in body["evidence"]]
    assert ids[0].startswith("bal_c_002_") and len(ids[0]) == len("bal_c_002_") + 8
    assert ids[1:5] == ["txn_c_002_01", "txn_c_002_08", "txn_c_002_02", "txn_c_002_09"]
    assert ids[-1].startswith("agg_c_002_payments_") and len(ids) == 6
    summary = evidence_by_id(body)[ids[-1]]["data"]
    assert (summary["start"], summary["end"]) == ("2026-11-03", "2026-11-05")
    # Shares on a transaction stay those of the review window.
    assert evidence_by_id(body)["txn_c_002_01"]["data"]["share_of_fall_bp"] == 5556


def test_a_window_with_no_balance_says_so(client, auth_headers):
    body = _ask(client, auth_headers, "c_002", start="2026-01-01", end="2026-01-08")
    assert body["evidence"] == []
    assert body["data"]["not_available"] == ["No balance was reported on 2026-01-01.", "No balance was reported on 2026-01-08."]


def test_start_after_end_is_422(client, auth_headers):
    assert call(client, auth_headers, "c_002", "transactions", start="2026-11-10", end="2026-11-03").status_code == 422


@pytest.mark.parametrize("tool", ["transactions", "cash_summary", "balance_vs_txn"])
def test_a_one_day_window_is_that_day(client, auth_headers, tool):
    """Regression: start == end, which the contract and the Orchestrator allow, was a 422. It is the day
    before to that day: the same window, so the same records and ids, as asking for it so."""
    one_day = call(client, auth_headers, "c_002", tool, start="2026-11-10", end="2026-11-10")
    assert one_day.status_code == 200
    body = one_day.json()
    assert (body["data"]["start"], body["data"]["end"]) == ("2026-11-09", "2026-11-10")
    # One id, one content: the window's own records get the variant ids (not the review window's), the
    # same ones the explicit window gets, with the same content.
    own = [r["id"] for r in body["evidence"] if r["kind"] in ("daily_balance", "payment_summary", "balance_reconciliation")]
    assert not {"bal_c_002", "agg_c_002_payments", "agg_c_002_reconciliation"} & set(own)
    # balance_vs_txn has no verdict for a day cash did not fall on, and says why.
    assert own if tool != "balance_vs_txn" else (own or body["data"]["not_available"])
    same = call(client, auth_headers, "c_002", tool, start="2026-11-09", end="2026-11-10").json()
    assert body["evidence"] == same["evidence"]


def test_unknown_customer_is_404(client, mint):
    headers = {"Authorization": f"Bearer {mint(customers=['c_999'])}"}
    assert call(client, headers, "c_999", "transactions").status_code == 404
