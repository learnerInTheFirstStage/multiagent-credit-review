"""income_trend: thirteen weeks of inflows ending on the as-of date, with the trend's label."""

import pytest

from tests.conftest import call, team_customer, write_customers


def _inflows(client, headers, cid) -> dict:
    body = call(client, headers, cid, "income_trend").json()
    assert [r["id"] for r in body["evidence"]] == [f"agg_{cid}_inflows"]
    assert body["evidence"][0]["kind"] == "weekly_inflow_series"
    return body["evidence"][0]["data"]


def test_the_series_is_thirteen_weeks_ending_on_the_as_of_date(client, auth_headers):
    """Regression: every week was zero, with no label the rules could read."""
    data = _inflows(client, auth_headers, "c_001")
    assert data["weeks"] == 13 and len(data["series"]) == 13
    assert data["series"][-1] == {"week_end": "2026-11-10", "inflow_cents": 10_000_000}
    assert data["series"][0]["week_end"] == "2026-08-18"
    assert all(week["inflow_cents"] == 10_000_000 for week in data["series"])
    assert data["income_trend"] == "steady"
    assert not {"compared_with", "vs_compared_bp", "weekly_inflow_typical_cents"} & set(data)


@pytest.mark.parametrize(
    ("cid", "trend", "compared_with", "level_key", "level", "vs_bp"),
    [
        ("c_003", "shrinking", "three_months_ago", "weekly_inflow_3mo_ago_cents", 17_000_000, 1765),
        ("c_107", "shrinking", "three_months_ago", "weekly_inflow_3mo_ago_cents", 8_000_000, 5000),
        ("c_006", "stopped", "usual", "weekly_inflow_typical_cents", 9_500_000, 526),
        ("c_004", "too_short_to_tell", "usual", "weekly_inflow_typical_cents", 12_000_000, 5000),
    ],
)
def test_the_level_this_week_is_set_against_comes_from_the_series(client, auth_headers, cid, trend, compared_with, level_key, level, vs_bp):
    data = _inflows(client, auth_headers, cid)
    assert (data["income_trend"], data["compared_with"], data[level_key], data["vs_compared_bp"]) == (trend, compared_with, level, vs_bp)
    first = data["series"][0]["inflow_cents"]
    usual = sorted(week["inflow_cents"] for week in data["series"][:-1])[5]
    assert data[level_key] == (first if compared_with == "three_months_ago" else usual)


def test_arguments_do_not_change_the_answer(client, auth_headers):
    """Regression: weeks and end were hashed into a 4-hex id with different content."""
    plain = call(client, auth_headers, "c_003", "income_trend").json()
    asked = call(client, auth_headers, "c_003", "income_trend", end="2026-10-01", weeks=4).json()
    assert asked["evidence"] == plain["evidence"]


def test_the_label_is_the_files_as_stated(seeded_with, tmp_path, mint):
    """The label is stored, not derived: the same figures can carry another label in another file."""
    twin = team_customer("c_004")
    twin["id"], twin["snapshot"]["bank"]["linked_accounts"][0]["id"] = "c_904", "acct_c904_a"
    twin["baselines"]["bank"]["income_trend"] = "stopped"
    with seeded_with(write_customers(tmp_path, "twin.yaml", [twin])) as client:
        headers = {"Authorization": f"Bearer {mint(customers=['c_904'])}"}
        data = _inflows(client, headers, "c_904")
    assert (data["income_trend"], data["vs_compared_bp"]) == ("stopped", 5000)
