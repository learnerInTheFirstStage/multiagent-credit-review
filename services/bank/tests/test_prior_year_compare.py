"""prior_year_compare: the same weeks one year earlier, set against every payment of the review window."""

from tests.conftest import call


def test_c007_last_year_set_against_this_window(client, auth_headers):
    """Every payment of the window, $294K, against every payment of last year's two weeks, $300K: the same kinds
    of movement in both years. Regression: the filler made this window's outflow 184% of last year's."""
    body = call(client, auth_headers, "c_007", "prior_year_compare").json()
    assert body["evidence"] == [
        {
            "id": "agg_c_007_prior_year",
            "kind": "prior_year_comparison",
            "data": {
                "outflow_cents": 30_000_000,
                "inflow_cents": 8_500_000,
                "period_days": 14,
                "window_days": 7,
                "window_out_cents": 29_400_000,
                "window_vs_prior_out_bp": 9800,
            },
        }
    ]


def test_no_similar_flag_the_guidelines_own_the_threshold(client, auth_headers):
    data = call(client, auth_headers, "c_007", "prior_year_compare").json()["evidence"][0]["data"]
    assert "similar" not in data


def test_a_customer_without_last_years_figures_gets_no_record(client, auth_headers):
    body = call(client, auth_headers, "c_001", "prior_year_compare").json()
    assert body["evidence"] == []
    assert body["data"]["not_available"] == ["Figures for the same weeks last year are not on record."]


def test_dates_do_not_change_the_answer(client, auth_headers):
    plain = call(client, auth_headers, "c_007", "prior_year_compare").json()
    asked = call(client, auth_headers, "c_007", "prior_year_compare", start="2026-10-01", end="2026-10-08").json()
    assert asked["evidence"] == plain["evidence"]
