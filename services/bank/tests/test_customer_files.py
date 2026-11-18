"""The customer files describe a bank feed that adds up.

Every movement of cash in the review window is an itemised event in `signal_window.bank_events`, so the week's
change in linked cash is what those events add up to: cash a week ago less cash now is the money that went out
less the money that came in. Two customers leave a gap on purpose and name it in `signal_window.intended_gap`:
c_005, part of whose cash sits in an account that stopped reporting (the missing movement is in that account,
which a feed would not show), and c_012, whose balances and transactions do not reconcile.

These read the files only. The seeder enforces the same rules when it seeds (test_seed.py).
"""

import pytest
import yaml

from tests.conftest import EXTRA_FILE, TEAM_FILE

FILES = {path.name: yaml.safe_load(path.read_text()) for path in (TEAM_FILE, EXTRA_FILE)}
CUSTOMERS = [customer for data in FILES.values() for customer in data["customers"]]
by_customer = pytest.mark.parametrize("customer", CUSTOMERS, ids=lambda customer: customer["id"])


def _moved(customer: dict) -> tuple[int, int]:
    """What the window's events took out and brought in, in cents."""
    events = customer["signal_window"]["bank_events"]
    out = sum(event["amount_cents"] for event in events if event["category"] != "income")
    came_in = sum(event["amount_cents"] for event in events if event["category"] == "income")
    return out, came_in


@by_customer
def test_the_weeks_fall_is_what_its_transactions_add_up_to(customer):
    bank = customer["snapshot"]["bank"]
    out, came_in = _moved(customer)
    gap = (customer["signal_window"].get("intended_gap") or {}).get("amount_cents", 0)

    assert bank["cash_week_ago_cents"] - bank["cash_now_cents"] == out - came_in + gap


def test_only_c_005_and_c_012_leave_a_gap_and_each_says_why():
    gaps = {
        customer["id"]: (gap["reason"], gap["amount_cents"])
        for customer in CUSTOMERS
        if (gap := customer["signal_window"].get("intended_gap"))
    }
    assert gaps == {"c_005": ("account_not_reporting", 35000000), "c_012": ("books_do_not_reconcile", 19500000)}

    c_005 = next(customer for customer in CUSTOMERS if customer["id"] == "c_005")
    assert any(account["days_stale"] for account in c_005["snapshot"]["bank"]["linked_accounts"])
    assert "reconciled = false" in FILES["shared_customers.yaml"]["data_integrity"]["c_012"]["note"]


@by_customer
def test_the_weeks_receipts_are_its_inflow(customer):
    _, came_in = _moved(customer)
    assert came_in == customer["baselines"]["bank"]["weekly_inflow_cents"]


@by_customer
def test_every_event_has_a_category_of_the_enum_and_an_amount_above_nothing(customer):
    categories = set(FILES["shared_customers.yaml"]["enums"]["transaction_category"])
    for event in customer["signal_window"]["bank_events"]:
        assert event["category"] in categories, event
        assert type(event["amount_cents"]) is int and event["amount_cents"] > 0, event
