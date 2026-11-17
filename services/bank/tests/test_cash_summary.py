"""cash_summary endpoint tests. Numbers come from data/shared_customers.yaml."""


def _get(client, headers, customer, start="2026-11-03", end="2026-11-10"):
    return client.get(
        f"/customers/{customer}/tools/cash_summary?start={start}&end={end}",
        headers=headers,
    )


def test_c002_summary_matches_yaml(client, auth_headers):
    response = _get(client, auth_headers, "c_002")
    assert response.status_code == 200
    data = response.json()["data"]
    # Per yaml: cash_week_ago=120M, cash_now=48M. Seeder adds a filler so txn
    # sum equals balance delta (-$720K).
    assert data["opening_cents"] == 120_000_000
    assert data["closing_cents"] == 48_000_000
    assert data["inflow_total_cents"] == 0  # no income events in c_002's window
    assert data["outflow_total_cents"] == -72_000_000
    # Known categories from the yaml events.
    assert data["by_category"]["transfer_own_account"] == -40_000_000
    assert data["by_category"]["payroll"] == -20_000_000
    assert data["by_category"]["vendor_known"] == -12_000_000


def test_evidence_includes_opening_closing_and_aggregate(client, auth_headers):
    response = _get(client, auth_headers, "c_002")
    evidence = response.json()["evidence"]
    kinds = [e["kind"] for e in evidence]
    assert "daily_balance" in kinds
    assert "cash_summary" in kinds
    agg_ids = [e["id"] for e in evidence if e["kind"] == "cash_summary"]
    assert len(agg_ids) == 1 and agg_ids[0].startswith("agg_")


def test_unknown_customer_is_404(client, mint):
    token = mint(customers=["c_999"])
    response = client.get(
        "/customers/c_999/tools/cash_summary?start=2026-11-03&end=2026-11-10",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


def test_start_after_end_is_422(client, auth_headers):
    response = _get(client, auth_headers, "c_002", start="2026-11-10", end="2026-11-03")
    assert response.status_code == 422
