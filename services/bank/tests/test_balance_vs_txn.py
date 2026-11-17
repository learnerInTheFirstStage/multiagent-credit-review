"""balance_vs_txn endpoint tests. c_012's deliberate gap is the headline."""


def _get(client, headers, customer, start="2026-11-03", end="2026-11-10"):
    return client.get(
        f"/customers/{customer}/tools/balance_vs_txn?start={start}&end={end}",
        headers=headers,
    )


def test_c002_reconciles(client, auth_headers):
    body = _get(client, auth_headers, "c_002").json()
    data = body["data"]
    assert data["balance_delta_cents"] == data["txn_sum_cents"]
    assert data["discrepancy_cents"] == 0
    assert data["reconciled"] is True


def test_c012_shows_the_deliberate_gap(client, auth_headers):
    body = _get(client, auth_headers, "c_012").json()
    data = body["data"]
    # Per yaml: txns explain ~-$95K; balance series falls by ~-$290K.
    assert data["balance_delta_cents"] == -29_000_000
    assert data["txn_sum_cents"] == -9_500_000
    assert data["discrepancy_cents"] == -19_500_000
    assert data["reconciled"] is False


def test_evidence_includes_endpoints_and_aggregate(client, auth_headers):
    body = _get(client, auth_headers, "c_012").json()
    kinds = [e["kind"] for e in body["evidence"]]
    assert "daily_balance" in kinds
    assert "balance_vs_txn_reconciliation" in kinds
