"""payee_history endpoint tests."""

from sqlalchemy import select


def _get(client, headers, customer, counterparty_id):
    return client.get(
        f"/customers/{customer}/tools/payee_history?counterparty_id={counterparty_id}",
        headers=headers,
    )


def _counterparty_id(display: str) -> int:
    # Open a fresh session that does NOT reset the schema; the `client`
    # fixture ran lifespan+seed before this is called.
    from app.db import SessionLocal
    from app.db.models import Counterparty

    with SessionLocal() as s:
        row = s.scalar(select(Counterparty).where(Counterparty.display_name == display))
        assert row is not None, f"{display} not seeded"
        return row.id


def test_single_payment_payee_is_marked_new(client, auth_headers):
    cp_id = _counterparty_id("Routine Supplier Ltd")
    body = _get(client, auth_headers, "c_011", cp_id).json()
    assert body["data"]["n_payments"] == 1
    assert body["data"]["is_new"] is True


def test_unknown_counterparty_is_404(client, auth_headers):
    response = _get(client, auth_headers, "c_002", 999_999)
    assert response.status_code == 404


def test_evidence_lists_every_matching_transaction(client, auth_headers):
    cp_id = _counterparty_id("Annual Vendor Co")
    body = _get(client, auth_headers, "c_009", cp_id).json()
    bank_txns = [e for e in body["evidence"] if e["kind"] == "bank_transaction"]
    assert len(bank_txns) == body["data"]["n_payments"]
