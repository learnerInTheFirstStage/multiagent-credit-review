"""transactions endpoint tests."""


def _get(client, headers, customer, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(
        f"/customers/{customer}/tools/transactions?{qs}",
        headers=headers,
    )


def test_c002_returns_three_window_events(client, auth_headers):
    response = _get(client, auth_headers, "c_002", start="2026-11-03", end="2026-11-10")
    assert response.status_code == 200
    data = response.json()["data"]
    # c_002's three yaml events already sum to the balance delta (-$72M), so
    # the seeder added no filler. Other customers (e.g. c_003) do have fillers.
    assert len(data["transactions"]) == 3
    assert data["truncated"] is False


def test_category_filter(client, auth_headers):
    response = _get(client, auth_headers, "c_002",
                    start="2026-11-03", end="2026-11-10", category="payroll")
    txns = response.json()["data"]["transactions"]
    assert len(txns) == 1
    assert all(t["category"] == "payroll" for t in txns)


def test_min_abs_amount_filter(client, auth_headers):
    # c_002's transfer is 40M; payroll 20M; vendor_known 12M; filler is ~720K.
    response = _get(client, auth_headers, "c_002",
                    start="2026-11-03", end="2026-11-10",
                    min_abs_amount_cents=15_000_000)
    txns = response.json()["data"]["transactions"]
    assert len(txns) == 2
    assert all(abs(t["amount_cents"]) >= 15_000_000 for t in txns)


def test_c009_injection_memo_is_returned_verbatim(client, auth_headers):
    response = _get(client, auth_headers, "c_009", start="2026-11-03", end="2026-11-10")
    txns = response.json()["data"]["transactions"]
    injected = [t for t in txns if t["memo"] and "IGNORE PREVIOUS INSTRUCTIONS" in t["memo"]]
    assert len(injected) == 1


def test_c008_three_distinct_unknown_payees(client, auth_headers):
    response = _get(client, auth_headers, "c_008", start="2026-11-03", end="2026-11-10")
    txns = response.json()["data"]["transactions"]
    new_vendor_txns = [t for t in txns if t["category"] == "vendor_new"]
    assert len(new_vendor_txns) == 3
    payees = {t["counterparty_display"] for t in new_vendor_txns}
    assert len(payees) == 3


def test_every_transaction_appears_in_evidence(client, auth_headers):
    response = _get(client, auth_headers, "c_002", start="2026-11-03", end="2026-11-10")
    body = response.json()
    txn_ids = {t["id"] for t in body["data"]["transactions"]}
    evidence_ids = {e["id"] for e in body["evidence"]}
    assert txn_ids <= evidence_ids
