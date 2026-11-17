"""income_trend endpoint tests. Note: the seeder lays down only the signal
window (Nov 3–10), so most customers lack 13 weeks of inflow history. This
is a deliberate test of enough_history=false, not a bug."""


def _get(client, headers, customer, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(
        f"/customers/{customer}/tools/income_trend?{qs}",
        headers=headers,
    )


def test_series_has_requested_number_of_weeks(client, auth_headers):
    response = _get(client, auth_headers, "c_003", end="2026-11-10", weeks=4)
    data = response.json()["data"]
    assert len(data["weeks"]) == 4


def test_not_enough_history_flag_when_most_weeks_are_empty(client, auth_headers):
    # c_003 has one small income event in the signal window; other weeks have none.
    response = _get(client, auth_headers, "c_003", end="2026-11-10", weeks=13)
    data = response.json()["data"]
    assert data["enough_history"] is False


def test_customer_with_no_inflows_returns_zero_slope(client, auth_headers):
    response = _get(client, auth_headers, "c_002", end="2026-11-10", weeks=4)
    data = response.json()["data"]
    # c_002 has no income events — zero inflows throughout → slope of zeros = 0.
    assert data["slope_cents_per_week"] == 0
    assert data["enough_history"] is False


def test_evidence_single_aggregate(client, auth_headers):
    response = _get(client, auth_headers, "c_003", end="2026-11-10", weeks=4)
    evidence = response.json()["evidence"]
    assert len(evidence) == 1
    assert evidence[0]["kind"] == "weekly_inflow_series"
    assert evidence[0]["id"].startswith("agg_")
