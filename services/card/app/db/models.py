"""Card Service data model.

Four tables:
  customers          one row per business with a card program: the current limit,
                     the card program's status, the bank account that pays the card,
                     and when each upstream feed last reported.
  credit_limits      every limit the customer has had, newest last. Seeded with the
                     initial limit; an approved action (KAN-28) adds a row.
  card_transactions  one card authorization and its settlement.
  repayments         one statement and the payment that settles it.

Columns follow what commercial card platforms expose (Ramp's Transaction and Transfer
objects, Stripe Issuing, Brex): signed integer amounts in cents, a lifecycle
(PENDING -> CLEARED, or DECLINED), raw merchant descriptor plus normalised name,
ISO 18245 MCC, issuer decline reasons, and NACHA return codes on repayments.

Money is BIGINT cents. Enums are VARCHAR + CHECK constraint (SQLAlchemy Enum with
native_enum=False), as on the Bank side.

Account ids in `funding_account_id` are the Bank Service's linked account ids
(acct_c001_a, ...), or an account Bank cannot see (acct_external_new_c006). That is
the key the Orchestrator matches across the two sides.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

CUSTOMER_STATUSES = ("active", "suspended", "closed")
CARD_STATUSES = ("active", "paused")
TRANSACTION_STATES = ("PENDING", "CLEARED", "DECLINED")
TRANSACTION_TYPES = ("purchase", "refund")
REPAYMENT_STATUSES = ("SCHEDULED", "INITIATED", "COMPLETED", "RETURNED")
PAYMENT_METHODS = ("ach_autopay", "ach_manual")


def _enum(values: tuple[str, ...], name: str) -> Enum:
    return Enum(*values, name=name, native_enum=False, create_constraint=True)


# BigInteger autoincrement works on Postgres, but SQLite only auto-increments a plain
# INTEGER PRIMARY KEY.
_AutoId = BigInteger().with_variant(Integer(), "sqlite")


class Customer(Base):
    __tablename__ = "customers"

    customer_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    industry: Mapped[str] = mapped_column(String(64), nullable=False)
    naics_code: Mapped[str | None] = mapped_column(String(8), nullable=True)
    onboarded_on: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        _enum(CUSTOMER_STATUSES, "customer_status"), nullable=False, default="active"
    )
    card_status: Mapped[str] = mapped_column(
        _enum(CARD_STATUSES, "card_status"), nullable=False, default="active"
    )
    # The limit in force now. credit_limits holds the history.
    credit_limit_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # The bank account autopay pulls from, and since when.
    funding_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    funding_account_since: Mapped[date] = mapped_column(Date, nullable=False)
    autopay_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # When each upstream feed last reported. A stale feed is a reason to say "cannot tell".
    card_feed_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payments_feed_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (CheckConstraint("credit_limit_cents >= 0", name="limit_not_negative"),)


class CreditLimit(Base):
    __tablename__ = "credit_limits"

    id: Mapped[int] = mapped_column(_AutoId, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.customer_id"), nullable=False, index=True
    )
    effective_on: Mapped[date] = mapped_column(Date, nullable=False)
    limit_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    # Set when an approved action made the change. Unique, so an approval applies once.
    approval_id: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)

    __table_args__ = (CheckConstraint("limit_cents >= 0", name="limit_cents_not_negative"),)


class CardTransaction(Base):
    __tablename__ = "card_transactions"

    txn_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.customer_id"), nullable=False
    )
    # The card. No cardholder names, only the department.
    card_id: Mapped[str] = mapped_column(String(48), nullable=False)
    card_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    card_nickname: Mapped[str] = mapped_column(String(64), nullable=False)
    cardholder_department: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Lifecycle.
    state: Mapped[str] = mapped_column(_enum(TRANSACTION_STATES, "transaction_state"), nullable=False)
    transaction_type: Mapped[str] = mapped_column(
        _enum(TRANSACTION_TYPES, "transaction_type"), nullable=False, default="purchase"
    )
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Settlement amount in USD cents (a refund is negative), and what the merchant charged.
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    merchant_amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    merchant_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    # Merchant.
    merchant_id: Mapped[str] = mapped_column(String(48), nullable=False)
    merchant_name: Mapped[str] = mapped_column(String(120), nullable=False)
    merchant_descriptor: Mapped[str] = mapped_column(String(120), nullable=False)
    mcc: Mapped[str] = mapped_column(String(4), nullable=False)
    mcc_description: Mapped[str] = mapped_column(String(120), nullable=False)
    spend_category: Mapped[str] = mapped_column(String(64), nullable=False)
    merchant_city: Mapped[str | None] = mapped_column(String(64), nullable=True)
    merchant_state: Mapped[str | None] = mapped_column(String(8), nullable=True)
    merchant_country: Mapped[str] = mapped_column(String(2), nullable=False, default="US")
    card_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_recurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    decline_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_txn_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    # Entered by the customer: data for the model, never instructions.
    memo: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        Index("ix_card_txn_customer_time", "customer_id", "authorized_at"),
        # A declined authorization never settles.
        CheckConstraint("state != 'DECLINED' OR cleared_at IS NULL", name="declined_never_clears"),
    )


class Repayment(Base):
    __tablename__ = "repayments"

    repayment_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.customer_id"), nullable=False
    )
    statement_period_start: Mapped[date] = mapped_column(Date, nullable=False)
    statement_period_end: Mapped[date] = mapped_column(Date, nullable=False)
    statement_balance_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payment_method: Mapped[str] = mapped_column(_enum(PAYMENT_METHODS, "payment_method"), nullable=False)
    # The bank account the payment was pulled from (a Bank linked account id, or one Bank cannot see).
    funding_account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(_enum(REPAYMENT_STATUSES, "repayment_status"), nullable=False)
    # NACHA return code on a RETURNED payment: R01 insufficient funds, R02 account closed, ...
    ach_return_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    return_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    initiated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_repayment_customer_due", "customer_id", "due_date"),
        CheckConstraint("statement_period_start <= statement_period_end", name="period_runs_forward"),
        # A returned payment carries its return code; nothing else does.
        CheckConstraint(
            "(status = 'RETURNED') = (ach_return_code IS NOT NULL)", name="return_code_iff_returned"
        ),
    )
