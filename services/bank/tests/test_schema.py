"""Schema-level tests. Exercise models.py without invoking the seeder."""

from datetime import date, datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.db import Base, engine
from app.db.models import (
    BankTransaction,
    Counterparty,
    Customer,
    DailyBalance,
    LinkedAccount,
    PriorYearWeekly,
)


def test_all_six_tables_exist(session):
    tables = set(inspect(engine).get_table_names())
    assert {
        "customers",
        "counterparties",
        "linked_accounts",
        "daily_balances",
        "bank_transactions",
        "prior_year_weekly",
    }.issubset(tables)


def test_create_all_is_idempotent(session):
    # Running create_all twice must not raise.
    Base.metadata.create_all(engine)


def test_foreign_key_from_linked_account_to_customer(session):
    session.add(
        LinkedAccount(
            id="acct_bogus",
            customer_id="c_does_not_exist",
            type="checking",
            opened_at=date(2024, 1, 1),
            last_reported_at=date(2024, 1, 1),
            active=True,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_transaction_category_check_rejects_unknown_value(session):
    session.add(
        Customer(customer_id="c_test", name="Test", created_at=datetime(2024, 1, 1))
    )
    session.add(
        LinkedAccount(
            id="acct_test",
            customer_id="c_test",
            type="checking",
            opened_at=date(2024, 1, 1),
            last_reported_at=date(2024, 1, 1),
            active=True,
        )
    )
    session.flush()
    session.add(
        BankTransaction(
            account_id="acct_test",
            date=date(2024, 1, 1),
            amount_cents=-100,
            category="bogus_category",
            counterparty_id=None,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_daily_balance_composite_primary_key(session):
    session.add(
        Customer(customer_id="c_test", name="Test", created_at=datetime(2024, 1, 1))
    )
    session.add(
        LinkedAccount(
            id="acct_test",
            customer_id="c_test",
            type="checking",
            opened_at=date(2024, 1, 1),
            last_reported_at=date(2024, 1, 1),
            active=True,
        )
    )
    session.flush()
    session.add(DailyBalance(account_id="acct_test", date=date(2024, 1, 1), closing_cents=100))
    session.flush()
    # Same account + same date is not allowed a second time.
    session.add(DailyBalance(account_id="acct_test", date=date(2024, 1, 1), closing_cents=200))
    with pytest.raises(IntegrityError):
        session.flush()


def test_prior_year_week_in_range(session):
    session.add(
        Customer(customer_id="c_test", name="Test", created_at=datetime(2024, 1, 1))
    )
    session.flush()
    session.add(
        PriorYearWeekly(
            customer_id="c_test", year=2025, week=99, inflow_cents=0, outflow_cents=0
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()


def test_counterparty_kind_check(session):
    session.add(Counterparty(display_name="x", kind="nonsense_kind"))
    with pytest.raises(IntegrityError):
        session.flush()
