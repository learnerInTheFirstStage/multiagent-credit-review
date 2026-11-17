"""prior_year_compare endpoint tests. Only c_007 has prior-year data."""


def _get(client, headers, customer, start="2026-11-03", end="2026-11-10"):
    return client.get(
        f"/customers/{customer}/tools/prior_year_compare?start={start}&end={end}",
        headers=headers,
    )


def test_c007_has_prior_year_data(client, auth_headers):
    body = _get(client, auth_headers, "c_007").json()
    data = body["data"]
    assert data["prior_inflow_cents"] > 0
    assert data["prior_outflow_cents"] < 0


def test_customer_without_prior_year_data_returns_zeros(client, auth_headers):
    body = _get(client, auth_headers, "c_002").json()
    data = body["data"]
    assert data["prior_inflow_cents"] == 0
    assert data["prior_outflow_cents"] == 0


def test_c007_returns_both_sides(client, auth_headers):
    # c_007's yaml story is "seasonal_dip, same as last November", but the
    # seeder lays down only outflows in the current window (no income events).
    # The tool's job is to surface both sides for the agent to reason over; it
    # does not force a verdict on similarity. We check that both prior-year
    # numbers are non-zero so the agent has material to compare against.
    body = _get(client, auth_headers, "c_007").json()
    data = body["data"]
    assert data["prior_inflow_cents"] > 0
    assert data["prior_outflow_cents"] < 0
    assert data["current_outflow_cents"] < 0


def test_evidence_single_aggregate(client, auth_headers):
    body = _get(client, auth_headers, "c_007").json()
    evidence = body["evidence"]
    assert len(evidence) == 1
    assert evidence[0]["kind"] == "prior_year_comparison"
