"""GET /snapshots (KAN-27): raw numbers per customer, checked against data/shared_customers.yaml."""

from datetime import date

import pytest

from app.db.seed import load
from contracts.signal import CardSnapshot
from tests.conftest import bearer, mint_token

DATA = load()
AS_OF = DATA["clock"]["signal_date"]
CUSTOMERS = {c["id"]: c for c in DATA["customers"]}


@pytest.fixture(scope="module")
def body(seeded_client):
    headers = bearer(mint_token(sub="detection-job", scope="snapshots:read"))
    response = seeded_client.get("/snapshots", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_every_customer_on_the_signal_date(body):
    assert body["as_of"] == AS_OF
    assert [c["customer_id"] for c in body["customers"]] == sorted(CUSTOMERS)


def test_numbers_match_the_files_card_snapshot(body):
    for row in body["customers"]:
        card = CUSTOMERS[row["customer_id"]]["snapshot"]["card"]
        cid = row["customer_id"]
        assert row["credit_limit_cents"] == card["credit_limit_cents"], cid
        assert row["balance_cents"] == round(card["credit_limit_cents"] * card["utilization_pct"]), cid
        assert row["used_share_bp"] == round(10_000 * card["utilization_pct"]), cid
        assert [r["status"] for r in row["repayments"]] == card["last_3_repayments"], cid
        assert [r["due"] for r in row["repayments"]] == ["2026-09-05", "2026-10-05", "2026-11-05"], cid


def test_trouble_repayments_carry_the_files_amounts(body):
    rows = {row["customer_id"]: row for row in body["customers"]}
    assert rows["c_003"]["repayments"][-1] == {"due": "2026-11-05", "amount_cents": 4_000_000, "status": "late"}
    assert rows["c_010"]["repayments"][-1] == {"due": "2026-11-05", "amount_cents": 5_000_000, "status": "failed"}


def test_c006_is_paid_from_the_new_account(body):
    rows = {row["customer_id"]: row for row in body["customers"]}
    assert rows["c_006"]["funding_account_id"] == "acct_external_new_c006"
    assert rows["c_001"]["funding_account_id"] == "acct_c001_a"


def test_each_row_fits_the_signal_contract(body):
    # A snapshot row drops straight into a signal's snapshot.card.
    for row in body["customers"]:
        CardSnapshot.model_validate({"balance_cents": row["balance_cents"], "repayments": row["repayments"]})


def test_one_customer(seeded_client, snapshot_headers):
    response = seeded_client.get("/snapshots", params={"customer_id": "c_010"}, headers=snapshot_headers)
    assert response.status_code == 200
    assert [c["customer_id"] for c in response.json()["customers"]] == ["c_010"]


def test_an_earlier_day_reads_the_ledger_as_it_was(seeded_client, snapshot_headers):
    # On Nov 4 the Nov 5 repayments were not due yet, and c_010's statement was still owed on top of new spend.
    response = seeded_client.get("/snapshots", params={"customer_id": "c_010", "as_of": "2026-11-04"},
                                 headers=snapshot_headers)
    row = response.json()["customers"][0]
    assert response.json()["as_of"] == "2026-11-04"
    assert [r["due"] for r in row["repayments"]] == ["2026-08-05", "2026-09-05", "2026-10-05"]
    assert all(r["status"] == "paid" for r in row["repayments"])
    assert row["balance_cents"] < 9_200_000


def test_unknown_customer_is_404(seeded_client, snapshot_headers):
    response = seeded_client.get("/snapshots", params={"customer_id": "c_999"}, headers=snapshot_headers)
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "not_found"


def test_a_day_after_the_data_is_refused(seeded_client, snapshot_headers):
    response = seeded_client.get("/snapshots", params={"as_of": "2026-11-11"}, headers=snapshot_headers)
    assert response.status_code == 422


def test_a_malformed_customer_id_is_refused(seeded_client, snapshot_headers):
    response = seeded_client.get("/snapshots", params={"customer_id": "c_1; drop"}, headers=snapshot_headers)
    assert response.status_code == 422
