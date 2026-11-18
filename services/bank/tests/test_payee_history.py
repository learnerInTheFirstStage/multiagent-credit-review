"""payee_history: asked with the id of one transaction, as the Orchestrator sends it."""

from tests.conftest import call, evidence_by_id, team_customer, write_customers


def _ask(client, headers, cid, record_id):
    return call(client, headers, cid, "payee_history", record_id=record_id)


def test_c011_the_payment_is_four_times_the_most_it_was_usually_paid(client, auth_headers):
    """Regression: the tool took a counterparty id and found one payment, the payee "new"."""
    body = _ask(client, auth_headers, "c_011", "txn_c_011_01").json()
    assert [r["id"] for r in body["evidence"]] == ["agg_c_011_payee_history", "txn_c_011_01"]
    assert evidence_by_id(body)["agg_c_011_payee_history"]["data"] == {
        "n_prior_payments": 18,
        "typical_amount_cents_min": 800_000,
        "typical_amount_cents_max": 4_000_000,
        "first_seen_months_ago": 24,
        "about": "txn_c_011_01",
        "amount_cents": 17_000_000,
        "usual_top_cents": 4_000_000,
        "vs_usual_bp": 42_500,
    }
    assert body["data"] == {"record_id": "txn_c_011_01", "paid_before": True}


def test_c009_the_payment_is_what_it_always_was(client, auth_headers):
    history = evidence_by_id(_ask(client, auth_headers, "c_009", "txn_c_009_01").json())["agg_c_009_payee_history"]
    assert history["data"]["typical_amount_cents"] == 4_500_000 and history["data"]["vs_usual_bp"] == 10_000
    assert "typical_amount_cents_min" not in history["data"]


def test_the_transaction_comes_back_whole(client, auth_headers):
    """Regression: the tool re-emitted txn ids with two keys where transactions gave five."""
    listed = evidence_by_id(call(client, auth_headers, "c_011", "transactions").json())["txn_c_011_01"]
    assert evidence_by_id(_ask(client, auth_headers, "c_011", "txn_c_011_01").json())["txn_c_011_01"] == listed


def test_a_payee_never_paid_before_has_no_history_record(client, auth_headers):
    body = _ask(client, auth_headers, "c_008", "txn_c_008_02").json()
    assert [r["id"] for r in body["evidence"]] == ["txn_c_008_02"]
    assert body["data"]["paid_before"] is False


def test_money_coming_in_is_not_a_payment(client, auth_headers):
    body = _ask(client, auth_headers, "c_003", "txn_c_003_01").json()
    assert [r["id"] for r in body["evidence"]] == ["txn_c_003_01"]
    assert body["data"]["not_available"] == ["It is money coming in, not a payment."]


def test_an_earlier_payment_gets_its_own_id(client, auth_headers):
    body = _ask(client, auth_headers, "c_011", "txn_c_011_p18").json()
    history = evidence_by_id(body)["agg_c_011_payee_history_p18"]["data"]
    assert history["about"] == "txn_c_011_p18" and history["n_prior_payments"] == 17


def test_another_customers_transaction_or_a_made_up_one_is_404(client, auth_headers):
    assert _ask(client, auth_headers, "c_009", "txn_c_011_01").status_code == 404
    assert _ask(client, auth_headers, "c_011", "txn_c_011_99").status_code == 404
    assert call(client, auth_headers, "c_011", "payee_history").status_code == 422


def test_a_second_payroll_run_never_takes_the_plain_id_from_the_vendor(seeded_with, tmp_path, mint):
    """Regression: payroll shares one payee, so a second run had history and took the plain id from the vendor."""
    twin = team_customer("c_009")
    twin["id"], twin["snapshot"]["bank"]["linked_accounts"][0]["id"] = "c_909", "acct_c909_a"
    payroll = {"category": "payroll", "amount_cents": 500_000, "counterparty_kind": "payroll_vendor"}
    twin["signal_window"]["bank_events"] = [payroll, dict(payroll), *twin["signal_window"]["bank_events"]]
    twin["snapshot"]["bank"]["cash_now_cents"] -= 1_000_000
    with seeded_with(write_customers(tmp_path, "twin.yaml", [twin])) as client:
        headers = {"Authorization": f"Bearer {mint(customers=['c_909'])}"}
        second_run = _ask(client, headers, "c_909", "txn_c_909_02").json()
        vendor = _ask(client, headers, "c_909", "txn_c_909_03").json()
    # The first run of the window and both runs' six earlier ones went to the same payee.
    assert [r["id"] for r in second_run["evidence"]] == ["agg_c_909_payee_history_02", "txn_c_909_02"]
    assert evidence_by_id(second_run)["agg_c_909_payee_history_02"]["data"]["n_prior_payments"] == 13
    assert evidence_by_id(vendor)["agg_c_909_payee_history"]["data"]["about"] == "txn_c_909_03"


def test_a_running_cost_has_the_thirteen_weeks_before_it_as_history(client, auth_headers):
    """Regression: only a vendor's payee had history, so a running cost paid every week read "never paid before"."""
    body = _ask(client, auth_headers, "c_203", "txn_c_203_03").json()
    assert [r["id"] for r in body["evidence"]] == ["agg_c_203_payee_history_03", "txn_c_203_03"]
    assert evidence_by_id(body)["agg_c_203_payee_history_03"]["data"] == {
        "n_prior_payments": 13,
        "typical_amount_cents": 5_500_000,
        "first_seen_months_ago": 3,
        "about": "txn_c_203_03",
        "amount_cents": 5_500_000,
        "usual_top_cents": 5_500_000,
    }
    assert body["data"] == {"record_id": "txn_c_203_03", "paid_before": True}


def test_payroll_and_tax_have_their_earlier_runs_as_history(client, auth_headers):
    """Regression: no earlier payroll run or tax payment was on record, so each read "never paid before"."""
    payroll = evidence_by_id(_ask(client, auth_headers, "c_001", "txn_c_001_01").json())["agg_c_001_payee_history_01"]
    tax = evidence_by_id(_ask(client, auth_headers, "c_001", "txn_c_001_02").json())["agg_c_001_payee_history_02"]
    assert (payroll["data"]["n_prior_payments"], payroll["data"]["typical_amount_cents"]) == (6, 28_000_000)
    assert (tax["data"]["n_prior_payments"], tax["data"]["typical_amount_cents"]) == (1, 12_500_000)


def test_only_a_vendors_history_is_set_against_the_usual(client, auth_headers):
    """G-02 reads vs_usual_bp on any payee history: a running cost in line with its own would pass for the vendor's.
    c_007's running costs are at most what last year's weeks paid, which would meet it."""
    for cid, txn in (("c_007", "txn_c_007_08"), ("c_011", "txn_c_011_05"), ("c_001", "txn_c_001_01")):
        history = evidence_by_id(_ask(client, auth_headers, cid, txn).json())[f"agg_{cid}_payee_history_{txn[-2:]}"]
        assert "vs_usual_bp" not in history["data"] and history["data"]["usual_top_cents"] > 0, txn


def test_the_plain_id_stays_on_the_first_vendor_payment_with_history(client, auth_headers):
    """c_009's history keeps its id and content. c_007's vendor has no history, and a running cost paid on the
    window's first day (txn_c_007_08) does not take the plain id in its place."""
    body = _ask(client, auth_headers, "c_009", "txn_c_009_01").json()
    assert evidence_by_id(body)["agg_c_009_payee_history"]["data"] == {
        "n_prior_payments": 2,
        "typical_amount_cents": 4_500_000,
        "first_seen_months_ago": 24,
        "about": "txn_c_009_01",
        "amount_cents": 4_500_000,
        "usual_top_cents": 4_500_000,
        "vs_usual_bp": 10_000,
    }
    listed = call(client, auth_headers, "c_007", "transactions").json()["data"]["transaction_ids"]
    for txn in listed:
        ids = [r["id"] for r in _ask(client, auth_headers, "c_007", txn).json()["evidence"]]
        assert "agg_c_007_payee_history" not in ids, txn
    assert [r["id"] for r in _ask(client, auth_headers, "c_007", "txn_c_007_01").json()["evidence"]] == ["txn_c_007_01"]


def test_a_renamed_twin_has_the_same_history(seeded_with, tmp_path, mint):
    """Regression: the payee was picked by customer id (c_009, c_011), so a twin had none."""
    twin = team_customer("c_011")
    twin["id"], twin["snapshot"]["bank"]["linked_accounts"][0]["id"] = "c_911", "acct_c911_a"
    with seeded_with(write_customers(tmp_path, "twin.yaml", [twin])) as client:
        headers = {"Authorization": f"Bearer {mint(customers=['c_911'])}"}
        body = _ask(client, headers, "c_911", "txn_c_911_01").json()
    assert evidence_by_id(body)["agg_c_911_payee_history"]["data"]["vs_usual_bp"] == 42_500
