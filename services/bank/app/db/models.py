"""Bank Service data model. Per Cash Flow PRD v0.2 "Data" section.

Six tables:
  customers           — one row per test customer.
  counterparties      — shared pool of payees, reused across customers.
  linked_accounts     — customer accounts Ramp sees. Many-to-one to customer.
  daily_balances      — closing balance per account per day.
  bank_transactions   — one row per itemised money movement in the window.
  prior_year_weekly   — weekly inflow / outflow one year back, for the
                        compare_to_prior_year tool (only populated where
                        a customer's story calls for it).

Enums are stored as VARCHAR + CHECK constraint (SQLAlchemy Enum with
native_enum=False). Values match data/shared_customers.yaml enums:.
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
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# Values from data/shared_customers.yaml enums:.
TRANSACTION_CATEGORIES = (
    "payroll",
    "tax",
    "vendor_known",
    "vendor_new",
    "transfer_own_account",
    "transfer_other",
    "income",
    "other",
)
COUNTERPARTY_KINDS = (
    "payroll_vendor",
    "tax_authority",
    "known_vendor",
    "unknown_payee",
    "own_account_other_bank",
)
LINKED_ACCOUNT_TYPES = ("checking", "savings")


class Customer(Base):
    __tablename__ = "customers"

    customer_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    accounts: Mapped[list["LinkedAccount"]] = relationship(back_populates="customer")


class Counterparty(Base):
    __tablename__ = "counterparties"

    # BigInteger autoincrement works on Postgres but SQLite only recognises
    # plain INTEGER PRIMARY KEY as the auto-incrementing ROWID alias.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(
        Enum(
            *COUNTERPARTY_KINDS,
            name="counterparty_kind",
            native_enum=False,
            create_constraint=True,
        ),
        nullable=False,
    )


class LinkedAccount(Base):
    __tablename__ = "linked_accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.customer_id"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(
        Enum(
            *LINKED_ACCOUNT_TYPES,
            name="linked_account_type",
            native_enum=False,
            create_constraint=True,
        ),
        nullable=False,
    )
    opened_at: Mapped[date] = mapped_column(Date, nullable=False)
    last_reported_at: Mapped[date] = mapped_column(Date, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    customer: Mapped[Customer] = relationship(back_populates="accounts")
    balances: Mapped[list["DailyBalance"]] = relationship(back_populates="account")
    transactions: Mapped[list["BankTransaction"]] = relationship(back_populates="account")


class DailyBalance(Base):
    __tablename__ = "daily_balances"

    account_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("linked_accounts.id"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    closing_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (PrimaryKeyConstraint("account_id", "date"),)

    account: Mapped[LinkedAccount] = relationship(back_populates="balances")


class BankTransaction(Base):
    __tablename__ = "bank_transactions"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    account_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("linked_accounts.id"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    # Negative for outflow, positive for inflow. Integer cents per API doc §1.
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category: Mapped[str] = mapped_column(
        Enum(
            *TRANSACTION_CATEGORIES,
            name="transaction_category",
            native_enum=False,
            create_constraint=True,
        ),
        nullable=False,
    )
    counterparty_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("counterparties.id"), nullable=True
    )
    # Memos reach the model; c_009 keeps a prompt-injection string here on purpose.
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)

    account: Mapped[LinkedAccount] = relationship(back_populates="transactions")
    counterparty: Mapped[Counterparty | None] = relationship()


class PriorYearWeekly(Base):
    __tablename__ = "prior_year_weekly"

    customer_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("customers.customer_id"), nullable=False
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    week: Mapped[int] = mapped_column(Integer, nullable=False)
    inflow_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    outflow_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("customer_id", "year", "week"),
        CheckConstraint("week BETWEEN 1 AND 53", name="week_in_range"),
    )
