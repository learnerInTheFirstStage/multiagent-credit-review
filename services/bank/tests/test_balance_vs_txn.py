"""balance_vs_txn: whether the balances moved by exactly what the window's transactions add up to."""

from tests.conftest import call, evidence_by_id, team_customer, write_customers


def test_c002_reconciles(client, auth_headers):
    body = call(client, auth_headers, "c_002", "balance_vs_txn").json()
    assert [r["id"] for r in body["evidence"]] == ["agg_c_002_reconciliation", "bal_c_002"]
    assert evidence_by_id(body)["agg_c_002_reconciliation"] == {
        "id": "agg_c_002_reconciliation",
        "kind": "balance_reconciliation",
        "data": {"balance_change_cents": -72_000_000, "transactions_total_cents": -72_000_000, "reconciled": True},
    }


def test_c012_shows_the_deliberate_gap_to_the_cent(client, auth_headers):
    """Regression: the kind was balance_vs_txn_reconciliation and a $100K tolerance decided "reconciled"."""
    books = evidence_by_id(call(client, auth_headers, "c_012", "balance_vs_txn").json())["agg_c_012_reconciliation"]
    assert books["data"] == {
        "balance_change_cents": -29_000_000,
        "transactions_total_cents": -9_500_000,
        "reconciled": False,
        "unmatched_cents": 19_500_000,
        "unmatched_share_bp": 6724,
    }


def test_one_cent_off_does_not_reconcile(seeded_with, tmp_path, mint):
    off = team_customer("c_012")
    off["id"], off["snapshot"]["bank"]["linked_accounts"][0]["id"] = "c_912", "acct_c912_a"
    off["snapshot"]["bank"]["cash_now_cents"] = 54_499_999  # the events explain 9.5M of a 9_500_001 fall
    off["signal_window"]["intended_gap"] = {"amount_cents": 1, "reason": "books_do_not_reconcile"}
    with seeded_with(write_customers(tmp_path, "off.yaml", [off])) as client:
        headers = {"Authorization": f"Bearer {mint(customers=['c_912'])}"}
        books = evidence_by_id(call(client, headers, "c_912", "balance_vs_txn").json())["agg_c_912_reconciliation"]
    assert books["data"]["reconciled"] is False and books["data"]["unmatched_cents"] == 1


def test_a_stale_account_gives_no_verdict(client, auth_headers):
    """Regression: c_005 came back reconciled=false with a $350K discrepancy, an insufficient_data verdict."""
    body = call(client, auth_headers, "c_005", "balance_vs_txn").json()
    assert body["evidence"] == []
    assert "acct_c005_a" in body["data"]["not_available"][0]


def test_another_window_gets_its_own_id(client, auth_headers):
    body = call(client, auth_headers, "c_012", "balance_vs_txn", start="2026-11-03", end="2026-11-05").json()
    ids = [r["id"] for r in body["evidence"]]
    assert ids[0].startswith("agg_c_012_reconciliation_") and ids[1].startswith("bal_c_012_")
    assert evidence_by_id(body)[ids[0]]["data"]["reconciled"] is False
