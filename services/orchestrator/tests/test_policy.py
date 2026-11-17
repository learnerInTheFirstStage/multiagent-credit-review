import ast
import json
from datetime import date
from pathlib import Path
from typing import get_args

from app import policy
from app.graph.report import LikelyCause

APP = Path(policy.__file__).parent
# The two share lines: the least that the cited payments' shares of the fall add up to (share_at_least_bp).
SHARE_LINES = ("EXPLAINS_BP", "LARGE_PART_BP")


def test_the_policy_file_imports_nothing_from_the_app():
    # Every part of the app may read the policy, so the policy reads nothing: no cycle, no number kept elsewhere.
    tree = ast.parse(Path(policy.__file__).read_text())

    assert [node.lineno for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))] == []


def test_no_specialist_stand_in_reads_the_policy():
    # A specialist measures and never judges: every threshold sits in a guideline need (1.5).
    for path in sorted((APP / "agents").rglob("*.py")):
        tree = ast.parse(path.read_text())
        names = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        names += [
            f"{node.module or ''}.{alias.name}"
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        ]

        assert [name for name in names if "policy" in name.split(".")] == [], path


def test_every_share_line_is_a_whole_number_of_basis_points_within_the_whole():
    for name in SHARE_LINES:
        value = getattr(policy, name)
        # type() and not isinstance(): True is an int, and a line of True would read as 1 bp.
        assert type(value) is int and 1 <= value <= 10000, name


def test_every_ratio_line_is_a_positive_whole_number_of_basis_points():
    # These compare one amount with another, so the owner may set one over the whole:
    # "in line with its history" could be read as up to 110% of the usual top (decision 2).
    lines = {
        name: value
        for name, value in vars(policy).items()
        if name.endswith("_BP") and name not in SHARE_LINES and not isinstance(value, tuple)
    }

    assert {"SHARP_BP", "IN_LINE_BP", "RECOVERED_BP"} <= set(lines)
    for name, value in lines.items():
        assert type(value) is int and value >= 1, name


def test_the_same_fall_is_a_band_of_whole_basis_points_that_holds_an_equal_amount():
    # A ratio, not a share: this window can send out more than last year's record did, so the top is over the whole.
    low, high = policy.SAME_FALL_BP

    assert type(low) is int and type(high) is int
    assert 0 < low < 10000 < high


def test_what_explains_the_fall_asks_for_more_than_a_large_part_of_it():
    # G-01 and G-04 need the fall explained, G-02 and G-09 only a large part of it.
    assert policy.LARGE_PART_BP < policy.EXPLAINS_BP


def test_the_two_numbers_the_policy_does_not_have_yet_are_none():
    # Until the owner gives them, G-12 and G-13 cannot be cited.
    assert policy.LARGE_LIMIT_CENTS is None
    assert policy.MIN_CASH_CENTS is None


def test_every_reading_the_owner_has_not_ruled_on_has_a_switch_and_it_is_on():
    assert set(policy.RULES) == {
        "every_cited_guideline_allows",
        "hand_over_cites_a_guideline",
        "none_fits_is_checked",
        "must_address",
    }
    # `is True`, not ==: a switch of 1 or "yes" would compare equal and read as on.
    assert [name for name, on in policy.RULES.items() if on is not True] == []
    # Decision 1 is the reading held in a value, not a switch: by default a failed repayment
    # that nobody recorded as paid counts as unpaid.
    assert "not_recorded" in policy.UNPAID_MEANS


def test_a_repayment_paid_since_never_counts_as_unpaid():
    # G-08a and G-08b split on paid_since: with "yes" here one repayment would meet both halves.
    # A bare string would match by substring ("no" is in "not_recorded"), so it must be a tuple.
    assert type(policy.UNPAID_MEANS) is tuple
    assert "no" in policy.UNPAID_MEANS
    assert "yes" not in policy.UNPAID_MEANS
    assert set(policy.UNPAID_MEANS) <= {"yes", "no", "not_recorded"}


def test_the_causes_that_count_toward_a_repeat_are_causes_a_report_can_give():
    # An empty list, or a misspelt cause, would match no earlier review, and the repeat count would stay at 0
    # without a word.
    assert type(policy.REPEAT_CAUSES) is tuple
    assert policy.REPEAT_CAUSES
    assert set(policy.REPEAT_CAUSES) <= set(get_args(LikelyCause))


def test_a_repeat_takes_at_least_one_earlier_request():
    # G-14 allows at most REPEAT_REQUESTS - 1 requests in the window: at 0 no customer could meet it.
    assert type(policy.REPEAT_REQUESTS) is int and policy.REPEAT_REQUESTS >= 1


def test_every_day_count_is_a_positive_whole_number_and_a_season_is_under_half_a_year():
    counts = {name: value for name, value in vars(policy).items() if name.endswith("_DAYS")}

    assert {"WITHIN_DAYS", "SEASON_DAYS", "REPEAT_WINDOW_DAYS"} <= set(counts)
    for name, value in counts.items():
        assert type(value) is int and value > 0, name
    # Give or take half a year, every day of every earlier year would be "the same period".
    assert 2 * policy.SEASON_DAYS < 365


def test_the_policy_version_is_a_date():
    assert date.fromisoformat(policy.POLICY_VERSION)


def test_every_value_fits_in_one_json_line():
    # The evaluation writes the whole policy into the record of each run (8.4).
    values = {name: value for name, value in vars(policy).items() if name.isupper()}

    assert {"POLICY_VERSION", "UNPAID_MEANS", "REPEAT_CAUSES", "RULES"} <= set(values)
    json.dumps(values)  # raises on a value that is not plain data, such as a set
