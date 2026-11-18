"""cash_summary: the balance and the payment summary of a window, without the list of transactions."""

from tests.conftest import call, evidence_by_id


def test_c002_summary_is_the_payment_summary_the_rules_read(client, auth_headers):
    """Regression: the kind was cash_summary under a 4-hex id the rules never read."""
    body = call(client, auth_headers, "c_002", "cash_summary").json()
    assert [r["id"] for r in body["evidence"]] == ["bal_c_002", "agg_c_002_payments"]
    summary = evidence_by_id(body)["agg_c_002_payments"]
    assert summary["kind"] == "payment_summary"
    # Every payment of the week, running costs included, and the week's income beside it.
    assert summary["data"]["listed_out_cents"] == 87_000_000 and summary["data"]["listed_in_cents"] == 15_000_000
    assert "not_listed_cents" not in summary["data"]
    assert summary["data"]["by_category"]["transfer_own_account"] == {"count": 1, "out_cents": 40_000_000, "share_bp": 5556}


def test_c010_payroll_and_tax_are_summed(client, auth_headers):
    summary = evidence_by_id(call(client, auth_headers, "c_010", "cash_summary").json())["agg_c_010_payments"]["data"]
    assert (summary["payroll_and_tax_cents"], summary["payroll_and_tax_share_bp"]) == (22_000_000, 9565)


def test_the_same_records_as_transactions_returns(client, auth_headers):
    listed = evidence_by_id(call(client, auth_headers, "c_008", "transactions").json())
    summary = call(client, auth_headers, "c_008", "cash_summary").json()
    assert all(record == listed[record["id"]] for record in summary["evidence"])


def test_start_after_end_is_422(client, auth_headers):
    assert call(client, auth_headers, "c_002", "cash_summary", start="2026-11-10", end="2026-11-03").status_code == 422
