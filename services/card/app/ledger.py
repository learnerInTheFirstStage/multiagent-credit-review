"""One customer's card ledger, read once per request. The business logic every endpoint shares.

Every number the detection job or an agent reads is worked out here, in code: the balance on
a day, the limit on a day, and what a repayment's status means (paid, late or failed). Columns
are selected one by one, so nothing reads a field no endpoint needs.

A customer has a few thousand rows at most, so one load and in-memory sums are simple and give
the same answer on SQLite and Postgres.
"""

from __future__ import annotations

import os
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from itertools import accumulate

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CardTransaction, CreditLimit, Customer, Repayment

# What a repayment's status can be called outside this service. The same words as the signal
# (contracts.signal.Repayment) and data/shared_customers.yaml.
PAID, LATE, FAILED = "paid", "late", "failed"


@lru_cache
def service_as_of() -> date:
    """The day the data is read on: AS_OF_DATE, or else the shared file's signal_date."""
    if value := os.environ.get("AS_OF_DATE"):
        return date.fromisoformat(value)
    from app.db.seed import load  # the file the database was seeded from

    return date.fromisoformat(load()["clock"]["signal_date"])


def day_of(moment: datetime) -> date:
    """The UTC day of a timestamp. Postgres returns aware datetimes, SQLite naive ones (stored as UTC)."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    return moment.date()


@dataclass(frozen=True)
class Profile:
    customer_id: str
    industry: str
    onboarded_on: date
    status: str
    card_status: str
    credit_limit_cents: int
    funding_account_id: str
    funding_account_since: date
    card_feed_updated_at: datetime
    payments_feed_updated_at: datetime


@dataclass(frozen=True)
class Txn:
    txn_id: str
    card_id: str
    card_nickname: str
    state: str
    transaction_type: str
    authorized_at: datetime
    cleared_at: datetime | None
    amount_cents: int
    merchant_amount_cents: int
    merchant_currency: str
    merchant_name: str
    merchant_descriptor: str
    mcc: str
    mcc_description: str
    spend_category: str
    merchant_country: str
    card_present: bool
    is_recurring: bool
    decline_reason: str | None
    memo: str | None

    @property
    def day(self) -> date:
        return day_of(self.authorized_at)

    @property
    def approved(self) -> bool:
        """Counts toward spend and the balance: authorized (PENDING) or settled (CLEARED)."""
        return self.state != "DECLINED"


@dataclass(frozen=True)
class Rep:
    repayment_id: str
    statement_period_start: date
    statement_period_end: date
    statement_balance_cents: int
    due_date: date
    amount_cents: int
    payment_method: str
    funding_account_id: str
    status: str
    ach_return_code: str | None
    return_reason: str | None
    initiated_at: datetime | None
    settled_at: datetime | None
    returned_at: datetime | None

    @property
    def settled_on(self) -> date | None:
        return day_of(self.settled_at) if self.status == "COMPLETED" and self.settled_at else None

    @property
    def returned_on(self) -> date | None:
        return day_of(self.returned_at) if self.status == "RETURNED" and self.returned_at else None


@dataclass(frozen=True)
class LimitRow:
    effective_on: date
    limit_cents: int


class Book:
    """Everything one customer's endpoints read, oldest first."""

    def __init__(self, profile: Profile, txns: list[Txn], repayments: list[Rep], limits: list[LimitRow]):
        self.profile, self.txns, self.repayments, self.limits = profile, txns, repayments, limits
        self._txn_days = [t.day for t in txns]
        approved = [(t.day, t.amount_cents) for t in txns if t.approved]
        self._spend_days = [d for d, _ in approved]
        self._spend_cum = list(accumulate(a for _, a in approved))
        paid = sorted((r.settled_on, r.amount_cents) for r in repayments if r.settled_on)
        self._paid_days = [d for d, _ in paid]
        self._paid_cum = list(accumulate(a for _, a in paid))

    @property
    def customer_id(self) -> str:
        return self.profile.customer_id

    @staticmethod
    def _upto(days: list[date], cum: list[int], day: date) -> int:
        i = bisect_right(days, day)
        return cum[i - 1] if i else 0

    def balance_on(self, day: date) -> int:
        """End-of-day balance: spend authorized and not declined, less repayments completed by then.
        A returned repayment never lowered it."""
        return self._upto(self._spend_days, self._spend_cum, day) - self._upto(self._paid_days, self._paid_cum, day)

    def limit_on(self, day: date) -> int:
        """The limit in force at the end of `day`: the newest credit_limits row on or before it."""
        current = self.limits[0].limit_cents if self.limits else self.profile.credit_limit_cents
        for row in self.limits:
            if row.effective_on <= day:
                current = row.limit_cents
        return current

    def txns_between(self, start: date, end: date) -> list[Txn]:
        """Transactions authorized on start..end, both included."""
        return self.txns[bisect_left(self._txn_days, start):bisect_right(self._txn_days, end)]

    def spend_between(self, start: date, end: date) -> int:
        return sum(t.amount_cents for t in self.txns_between(start, end) if t.approved)

    def due_by(self, day: date) -> list[Rep]:
        """Repayments due on or before `day`, oldest first: one per statement, the scheduled debit.

        A later payment toward the same statement (a returned debit paid by hand, rep_..._r1) is
        not a repayment of its own; evidence.repayment reads it as the statement's paid_since.
        """
        seen: set[date] = set()
        out = []
        for r in self.repayments:
            if r.due_date <= day and r.statement_period_end not in seen:
                seen.add(r.statement_period_end)
                out.append(r)
        return out

    def first_day(self) -> date | None:
        """The first day the card ledger holds anything."""
        return self._txn_days[0] if self._txn_days else None


def repayment_status(rep: Rep, as_of: date) -> str | None:
    """paid, late or failed, as known at the end of `as_of`. None while it cannot be called yet:
    not due, or due today and still in flight.

    paid: settled on or before its due date. late: settled after it, or due earlier and not settled.
    failed: the ACH debit came back (RETURNED).
    """
    if rep.due_date > as_of:
        return None
    if rep.returned_on is not None and rep.returned_on <= as_of:
        return FAILED
    if rep.settled_on is not None and rep.settled_on <= as_of:
        return PAID if rep.settled_on <= rep.due_date else LATE
    return LATE if as_of > rep.due_date else None


def load_book(session: Session, customer_id: str) -> Book | None:
    """The customer's ledger, or None when the customer is not on file."""
    row = session.execute(
        select(
            Customer.customer_id, Customer.industry, Customer.onboarded_on, Customer.status, Customer.card_status,
            Customer.credit_limit_cents, Customer.funding_account_id, Customer.funding_account_since,
            Customer.card_feed_updated_at, Customer.payments_feed_updated_at,
        ).where(Customer.customer_id == customer_id)
    ).first()
    if row is None:
        return None
    profile = Profile(*row)
    txns = [
        Txn(*r)
        for r in session.execute(
            select(
                CardTransaction.txn_id, CardTransaction.card_id, CardTransaction.card_nickname, CardTransaction.state,
                CardTransaction.transaction_type, CardTransaction.authorized_at, CardTransaction.cleared_at,
                CardTransaction.amount_cents, CardTransaction.merchant_amount_cents, CardTransaction.merchant_currency,
                CardTransaction.merchant_name, CardTransaction.merchant_descriptor, CardTransaction.mcc,
                CardTransaction.mcc_description, CardTransaction.spend_category, CardTransaction.merchant_country,
                CardTransaction.card_present, CardTransaction.is_recurring, CardTransaction.decline_reason,
                CardTransaction.memo,
            )
            .where(CardTransaction.customer_id == customer_id)
            .order_by(CardTransaction.authorized_at, CardTransaction.txn_id)
        )
    ]
    repayments = [
        Rep(*r)
        for r in session.execute(
            select(
                Repayment.repayment_id, Repayment.statement_period_start, Repayment.statement_period_end,
                Repayment.statement_balance_cents, Repayment.due_date, Repayment.amount_cents,
                Repayment.payment_method, Repayment.funding_account_id, Repayment.status, Repayment.ach_return_code,
                Repayment.return_reason, Repayment.initiated_at, Repayment.settled_at, Repayment.returned_at,
            )
            .where(Repayment.customer_id == customer_id)
            .order_by(Repayment.due_date, Repayment.repayment_id)
        )
    ]
    limits = [
        LimitRow(*r)
        for r in session.execute(
            select(CreditLimit.effective_on, CreditLimit.limit_cents)
            .where(CreditLimit.customer_id == customer_id)
            .order_by(CreditLimit.effective_on, CreditLimit.id)
        )
    ]
    return Book(profile, txns, repayments, limits)


def customer_ids(session: Session) -> list[str]:
    return list(session.scalars(select(Customer.customer_id).order_by(Customer.customer_id)))
