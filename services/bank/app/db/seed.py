"""Seed the Bank database from data/shared_customers.yaml.

Idempotent: seed_if_empty(session) only runs when the customers table is empty.
The file is the single source of truth per KAN-15; this seeder expands every
customer's snapshot + signal_window + baselines into concrete rows across the
six tables defined in models.py.

Key per-customer rules (from data/shared_customers.yaml data_integrity section):

- c_005: acct_c005_a stops reporting 5 days before signal_date — no
  daily_balances rows written for it after that date. The "missing" $350K is
  not modelled. Bank cannot see it; that is the point.
- c_009: the vendor_known transaction keeps its full memo verbatim, including
  the prompt-injection phrase. The adversarial regression test depends on it.
- c_012: transactions sum to about -$95K while the daily_balances series
  falls by about -$290K. The ~$195K discrepancy is deliberate — it is what
  get_balances_vs_transactions will later surface.

For every other customer a filler transaction (category=other,
counterparty_kind=unknown_payee) is added so that the sum of transactions
matches cash_now_cents - cash_week_ago_cents exactly, so the reconciliation
tool will later return `reconciled=true` on them.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from app.db.models import (
    BankTransaction,
    Counterparty,
    Customer,
    DailyBalance,
    LinkedAccount,
    PriorYearWeekly,
)


def _default_yaml_path() -> Path:
    """Where the seeder looks when no path is passed.

    Order: SHARED_CUSTOMERS_YAML env var, then /app/data/..., then the repo's
    data/ directory relative to this file (useful when running tests locally).
    """
    env = os.environ.get("SHARED_CUSTOMERS_YAML")
    if env:
        return Path(env)
    in_container = Path("/app/data/shared_customers.yaml")
    if in_container.exists():
        return in_container
    # services/bank/app/db/seed.py -> repo root is parents[4]
    return Path(__file__).resolve().parents[4] / "data" / "shared_customers.yaml"


# One shared counterparty row per (display_name, kind). Specific names come
# from the yaml baselines where available (c_009, c_011); the others are
# generic and reused across customers.
_PAYEES: dict[str, tuple[str, str]] = {
    "payroll": ("Standard Payroll Processor", "payroll_vendor"),
    "tax": ("US Internal Revenue Service", "tax_authority"),
    "known_vendor": ("Routine Known Vendor Co", "known_vendor"),
    "unknown_payee_1": ("Unknown Payee A", "unknown_payee"),
    "unknown_payee_2": ("Unknown Payee B", "unknown_payee"),
    "unknown_payee_3": ("Unknown Payee C", "unknown_payee"),
    "unknown_payee_filler": ("Unspecified Small Payee", "unknown_payee"),
    "own_account_other_bank": (
        "Own Account at Another Bank",
        "own_account_other_bank",
    ),
    "c009_annual_vendor": ("Annual Vendor Co", "known_vendor"),
    "c011_routine_supplier": ("Routine Supplier Ltd", "known_vendor"),
}


def _ensure_counterparties(session: Session) -> dict[str, Counterparty]:
    out: dict[str, Counterparty] = {}
    for key, (name, kind) in _PAYEES.items():
        row = Counterparty(display_name=name, kind=kind)
        session.add(row)
        out[key] = row
    session.flush()
    return out


def _signed_amount(category: str, amount_cents: int) -> int:
    """Inflow categories keep positive sign; everything else goes negative."""
    if category == "income":
        return abs(amount_cents)
    return -abs(amount_cents)


def _pick_counterparty(
    payees: dict[str, Counterparty],
    customer_id: str,
    event: dict,
    offset: int,
) -> Counterparty | None:
    kind = event.get("counterparty_kind")
    if kind == "payroll_vendor":
        return payees["payroll"]
    if kind == "tax_authority":
        return payees["tax"]
    if kind == "own_account_other_bank":
        return payees["own_account_other_bank"]
    if kind == "known_vendor":
        if customer_id == "c_009":
            return payees["c009_annual_vendor"]
        if customer_id == "c_011":
            return payees["c011_routine_supplier"]
        return payees["known_vendor"]
    if kind == "unknown_payee":
        # c_008 has three distinct unknown payees — rotate.
        return payees[f"unknown_payee_{((offset - 1) % 3) + 1}"]
    return None


def _seed_linked_accounts(
    session: Session,
    customer_yaml: dict,
    signal_date: date,
    opened_at: date,
) -> list[LinkedAccount]:
    rows: list[LinkedAccount] = []
    for acct_yaml in customer_yaml["snapshot"]["bank"]["linked_accounts"]:
        row = LinkedAccount(
            id=acct_yaml["id"],
            customer_id=customer_yaml["id"],
            type="checking",
            opened_at=opened_at,
            last_reported_at=signal_date - timedelta(days=int(acct_yaml.get("days_stale", 0))),
            active=bool(acct_yaml.get("active", True)),
        )
        session.add(row)
        rows.append(row)
    session.flush()
    return rows


def _seed_event_transactions(
    session: Session,
    customer_id: str,
    primary_account: LinkedAccount,
    bank_events: list[dict],
    payees: dict[str, Counterparty],
    window_start: date,
) -> list[BankTransaction]:
    """Every yaml bank_event becomes one transaction on day window_start + k."""
    txns: list[BankTransaction] = []
    for offset, event in enumerate(bank_events, start=1):
        counterparty = _pick_counterparty(payees, customer_id, event, offset)
        txns.append(
            BankTransaction(
                account_id=primary_account.id,
                date=window_start + timedelta(days=offset),
                amount_cents=_signed_amount(event["category"], int(event["amount_cents"])),
                category=event["category"],
                counterparty_id=counterparty.id if counterparty else None,
                memo=event.get("memo") or event.get("note"),
            )
        )
    for row in txns:
        session.add(row)
    session.flush()
    return txns


def _lerp(a: int, b: int, step: int, steps: int) -> int:
    return a + ((b - a) * step) // steps


def _seed_daily_balances(
    session: Session,
    customer_id: str,
    accounts: list[LinkedAccount],
    snap: dict,
    signal_date: date,
    window_start: date,
) -> int:
    """Daily balance series per account across days window_start..signal_date.

    Simple linear interpolation from cash_week_ago_cents to cash_now_cents on
    the primary account. c_005 has two accounts and is special-cased so that
    the stale account stops reporting after its days_stale cutoff.
    """
    if customer_id == "c_005":
        series = _split_c005(accounts, signal_date, window_start)
    else:
        primary = accounts[0]
        cash_now = int(snap["cash_now_cents"])
        cash_ago = int(snap["cash_week_ago_cents"])
        series = [
            (
                primary,
                [
                    (window_start + timedelta(days=k), _lerp(cash_ago, cash_now, k, 7))
                    for k in range(8)
                ],
            )
        ]

    count = 0
    for account, points in series:
        for d, closing in points:
            session.add(
                DailyBalance(account_id=account.id, date=d, closing_cents=int(closing))
            )
            count += 1
    session.flush()
    return count


def _split_c005(
    accounts: list[LinkedAccount],
    signal_date: date,
    window_start: date,
) -> list[tuple[LinkedAccount, list[tuple[date, int]]]]:
    acct_a, acct_b = accounts
    # Per the c_005 story: acct_c005_a sat at ~$350K and stopped reporting
    # 5 days before signal_date; acct_c005_b sat at $150K and kept reporting.
    a_cutoff = signal_date - timedelta(days=5)
    a_series = [
        (window_start + timedelta(days=k), 35_000_000)
        for k in range(8)
        if (window_start + timedelta(days=k)) <= a_cutoff
    ]
    b_series = [(window_start + timedelta(days=k), 15_000_000) for k in range(8)]
    return [(acct_a, a_series), (acct_b, b_series)]


def _seed_prior_year_weekly(
    session: Session,
    customer_id: str,
    customer_yaml: dict,
    signal_date: date,
) -> None:
    """Only customers with prior_year_same_two_weeks (currently c_007) seed this."""
    py = customer_yaml.get("baselines", {}).get("bank", {}).get("prior_year_same_two_weeks")
    if not py:
        return
    total_in = int(py["inflow_cents"])
    total_out = int(py["outflow_cents"])
    prior_year = signal_date.year - 1
    # Signal window (Nov 3 – Nov 10) straddles ISO weeks 45 and 46.
    for week in (45, 46):
        session.add(
            PriorYearWeekly(
                customer_id=customer_id,
                year=prior_year,
                week=week,
                inflow_cents=total_in // 2,
                outflow_cents=total_out // 2,
            )
        )
    session.flush()


def seed(session: Session, yaml_path: Path | None = None) -> dict:
    """Populate every table. Flushes but does not commit. Returns row counts.

    Adds a filler transaction to every customer except c_012 so that the
    recorded transactions sum to cash_now_cents - cash_week_ago_cents. c_012
    keeps the deliberate ~$195K gap between its balance series and txn sum.
    """
    path = yaml_path or _default_yaml_path()
    with open(path) as f:
        data = yaml.safe_load(f)

    signal_date = date.fromisoformat(data["clock"]["signal_date"])
    signal_window_days = int(data["clock"]["signal_window_days"])
    window_start = signal_date - timedelta(days=signal_window_days)
    opened_at = signal_date - timedelta(days=24 * 30)

    payees = _ensure_counterparties(session)
    totals = {"customers": 0, "transactions": 0, "balances": 0}

    for cust in data["customers"]:
        cid = cust["id"]
        session.add(
            Customer(
                customer_id=cid,
                name=cust["name"],
                created_at=datetime.combine(opened_at, datetime.min.time()),
            )
        )
        accounts = _seed_linked_accounts(session, cust, signal_date, opened_at)

        snap = cust["snapshot"]["bank"]
        events = cust["signal_window"].get("bank_events", []) or []
        txns = _seed_event_transactions(
            session, cid, accounts[0], events, payees, window_start
        )

        if cid != "c_012" and accounts:
            txn_sum = sum(t.amount_cents for t in txns)
            delta = int(snap["cash_now_cents"]) - int(snap["cash_week_ago_cents"])
            gap = delta - txn_sum
            if gap != 0:
                filler = BankTransaction(
                    account_id=accounts[0].id,
                    date=window_start + timedelta(days=3),
                    amount_cents=gap,
                    category="other",
                    counterparty_id=payees["unknown_payee_filler"].id,
                    memo="Routine small payments (seeder-generated)",
                )
                session.add(filler)
                txns.append(filler)

        totals["transactions"] += len(txns)
        totals["balances"] += _seed_daily_balances(
            session, cid, accounts, snap, signal_date, window_start
        )
        _seed_prior_year_weekly(session, cid, cust, signal_date)
        totals["customers"] += 1

    session.flush()
    return totals


def is_empty(session: Session) -> bool:
    return session.query(Customer).first() is None


def seed_if_empty(session: Session, yaml_path: Path | None = None) -> dict | None:
    """Run seed() only if customers is empty. Commits on success."""
    if not is_empty(session):
        return None
    summary = seed(session, yaml_path)
    session.commit()
    return summary


def main() -> None:
    """CLI: python -m app.db.seed"""
    from app.db import Base, SessionLocal, engine

    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        summary = seed_if_empty(session)
        if summary is None:
            print("database already seeded; no changes made")
        else:
            print(f"seeded: {summary}")


if __name__ == "__main__":
    main()
