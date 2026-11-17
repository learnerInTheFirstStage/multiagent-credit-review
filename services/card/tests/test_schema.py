"""Schema-level tests. Exercise models.py without the seeder."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.db import Base, engine
from app.db.models import CardTransaction, CreditLimit, Customer, Repayment

NOW = datetime(2026, 11, 10, 6, tzinfo=UTC)


def _customer(cid: str = "c_test") -> Customer:
    return Customer(
        customer_id=cid,
        name="Test Co",
        industry="software",
        onboarded_on=date(2024, 1, 1),
        credit_limit_cents=1_000_000,
        funding_account_id="acct_test_a",
        funding_account_since=date(2024, 1, 1),
        card_feed_updated_at=NOW,
        payments_feed_updated_at=NOW,
    )


def _txn(**overrides) -> CardTransaction:
    fields = dict(
        txn_id="ctx_test_00001", customer_id="c_test", card_id="crd_test_1", card_last4="1234",
        card_nickname="Ops card", state="CLEARED", authorized_at=NOW, cleared_at=NOW, amount_cents=1_000,
        merchant_amount_cents=1_000, merchant_id="mer_1", merchant_name="Staples",
        merchant_descriptor="STAPLES 00115", mcc="5943", mcc_description="Stationery", spend_category="Office supplies",
    )
    return CardTransaction(**(fields | overrides))


def _repayment(**overrides) -> Repayment:
    fields = dict(
        repayment_id="rep_test_202611", customer_id="c_test", statement_period_start=date(2026, 9, 11),
        statement_period_end=date(2026, 10, 10), statement_balance_cents=1_000, due_date=date(2026, 11, 5),
        amount_cents=1_000, payment_method="ach_autopay", funding_account_id="acct_test_a", status="COMPLETED",
    )
    return Repayment(**(fields | overrides))


def test_all_four_tables_exist(session):
    tables = set(inspect(engine).get_table_names())
    assert {"customers", "credit_limits", "card_transactions", "repayments"} <= tables


def test_create_all_is_idempotent(session):
    Base.metadata.create_all(engine)


def test_transaction_needs_a_known_customer(session):
    session.add(_txn(customer_id="c_does_not_exist"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_limit_row_needs_a_known_customer(session):
    session.add(CreditLimit(customer_id="c_nobody", effective_on=date(2024, 1, 1), limit_cents=1, reason="x"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_transaction_state_check_rejects_unknown_value(session):
    session.add(_customer())
    session.flush()
    session.add(_txn(state="SETTLED"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_declined_authorization_never_clears(session):
    session.add(_customer())
    session.flush()
    session.add(_txn(state="DECLINED", decline_reason="AUTHORIZER_BUSINESS_LIMIT"))  # cleared_at still set
    with pytest.raises(IntegrityError):
        session.flush()


def test_a_returned_repayment_carries_its_return_code(session):
    session.add(_customer())
    session.flush()
    session.add(_repayment(status="RETURNED"))  # no ach_return_code
    with pytest.raises(IntegrityError):
        session.flush()


def test_only_a_returned_repayment_carries_a_return_code(session):
    session.add(_customer())
    session.flush()
    session.add(_repayment(status="COMPLETED", ach_return_code="R01"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_repayment_status_check_rejects_unknown_value(session):
    session.add(_customer())
    session.flush()
    session.add(_repayment(status="paid"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_an_approval_changes_the_limit_once(session):
    session.add(_customer())
    session.flush()
    for _ in range(2):
        session.add(CreditLimit(customer_id="c_test", effective_on=date(2026, 11, 10), limit_cents=500_000,
                                reason="approved reduction", approval_id="case_1:analyst"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_card_status_check_rejects_unknown_value(session):
    customer = _customer()
    customer.card_status = "frozen"
    session.add(customer)
    with pytest.raises(IntegrityError):
        session.flush()
