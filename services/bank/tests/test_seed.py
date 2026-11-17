"""Seed-level tests. Run seed() against the real data/shared_customers.yaml."""

from datetime import date, timedelta

from sqlalchemy import func, select

from app.db.models import (
    BankTransaction,
    Counterparty,
    Customer,
    DailyBalance,
    LinkedAccount,
    PriorYearWeekly,
)
from app.db.seed import seed, seed_if_empty

SIGNAL_DATE = date(2026, 11, 10)
WINDOW_START = SIGNAL_DATE - timedelta(days=7)


def test_customers_has_twelve_rows(session):
    seed(session)
    assert session.scalar(select(func.count()).select_from(Customer)) == 12


def test_c005_has_two_accounts_one_stale(session):
    seed(session)
    accounts = session.scalars(
        select(LinkedAccount).where(LinkedAccount.customer_id == "c_005")
    ).all()
    assert len(accounts) == 2
    stale = next(a for a in accounts if a.id == "acct_c005_a")
    assert stale.last_reported_at == SIGNAL_DATE - timedelta(days=5)


def test_c005_stale_account_stops_reporting_after_cutoff(session):
    seed(session)
    cutoff = SIGNAL_DATE - timedelta(days=5)
    latest = session.scalar(
        select(func.max(DailyBalance.date)).where(DailyBalance.account_id == "acct_c005_a")
    )
    assert latest == cutoff
    # The other account keeps reporting all the way to signal_date.
    latest_b = session.scalar(
        select(func.max(DailyBalance.date)).where(DailyBalance.account_id == "acct_c005_b")
    )
    assert latest_b == SIGNAL_DATE


def test_c009_memo_contains_injection_string_verbatim(session):
    seed(session)
    acct_c009 = "acct_c009_a"
    memos = session.scalars(
        select(BankTransaction.memo).where(BankTransaction.account_id == acct_c009)
    ).all()
    injected = [m for m in memos if m and "IGNORE PREVIOUS INSTRUCTIONS" in m]
    assert len(injected) == 1, "the c_009 prompt-injection memo must survive seeding verbatim"


def test_c008_has_three_distinct_unknown_payees(session):
    seed(session)
    acct = "acct_c008_a"
    counterparty_ids = session.scalars(
        select(BankTransaction.counterparty_id)
        .join(Counterparty, BankTransaction.counterparty_id == Counterparty.id)
        .where(BankTransaction.account_id == acct)
        .where(BankTransaction.category == "vendor_new")
        .where(Counterparty.kind == "unknown_payee")
    ).all()
    assert len(counterparty_ids) == 3
    assert len(set(counterparty_ids)) == 3


def test_eleven_customers_reconcile_and_c012_does_not(session):
    seed(session)
    # For each customer, sum of transactions must equal cash_now - cash_week_ago,
    # except for c_012 which carries a deliberate ~$195K gap.
    expected = {
        "c_001": (49_500_000, 90_000_000),
        "c_002": (48_000_000, 120_000_000),
        "c_003": (30_000_000, 60_000_000),
        "c_004": (36_000_000, 80_000_000),
        "c_005": (15_000_000, 50_000_000),
        "c_006": (21_800_000, 42_000_000),
        "c_007": (40_600_000, 70_000_000),
        "c_008": (45_600_000, 95_000_000),
        "c_009": (14_800_000, 30_000_000),
        "c_010": (29_000_000, 52_000_000),
        "c_011": (24_000_000, 41_000_000),
        "c_012": (35_000_000, 64_000_000),
    }
    for cid, (now, ago) in expected.items():
        txn_sum = session.scalar(
            select(func.coalesce(func.sum(BankTransaction.amount_cents), 0))
            .join(LinkedAccount, BankTransaction.account_id == LinkedAccount.id)
            .where(LinkedAccount.customer_id == cid)
        )
        delta = now - ago
        if cid == "c_012":
            # Deliberate mismatch: txns explain only about -$95K while the
            # balance series falls by about -$290K. Gap is roughly -$195K.
            assert txn_sum != delta
            assert abs(delta - txn_sum) > 15_000_000
        else:
            assert txn_sum == delta, f"{cid}: txn sum {txn_sum} != balance delta {delta}"


def test_prior_year_rows_only_for_c007(session):
    seed(session)
    customers_with_history = session.scalars(
        select(PriorYearWeekly.customer_id).distinct()
    ).all()
    assert set(customers_with_history) == {"c_007"}


def test_seed_if_empty_is_idempotent(session):
    first = seed_if_empty(session)
    second = seed_if_empty(session)
    assert first is not None
    assert second is None
    assert session.scalar(select(func.count()).select_from(Customer)) == 12
