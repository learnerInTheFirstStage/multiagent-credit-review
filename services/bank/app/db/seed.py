"""Seed the Bank database from the shared customer files.

Idempotent: seed_if_empty(session) only runs when the customers table is empty. The files are
the single source of truth per KAN-15: data/shared_customers.yaml, and any file the evaluation
adds (SHARED_CUSTOMERS_YAML takes a list separated by ":"; the first file sets the clock).

A file gives each customer's bank feed for the review window, every movement of cash in it
(`bank_events`), the cash a week ago and now, and baselines. The seeder turns that into ledger
rows by rules that read only the data, never a customer id. Every row is a transaction with an
id, a category and a counterparty: nothing is counted that is not listed.

- Each bank event is one transaction, numbered in file order: txn_<customer>_<nn>, on day
  1 + (nn - 1) mod 7 of the window. Its memo is the event's `memo`, word for word, and its payee
  or payer is named by `counterparty` (stored, never sent out). A `note` is the file authors'
  comment and is never stored.
- The window's receipts add up to `weekly_inflow_cents`, and all its transactions to the change
  in cash. A file that does not add up is refused, except by the gap it names in
  `signal_window.intended_gap`: the cash of an account that stopped reporting (`days_stale`: its
  balance drops out of linked cash, and no transaction explains that), or books that do not
  reconcile (the balance falls by the gap more than the transactions, spread over the window).
- The weeks before the window repeat its everyday business: receipts from the window's payers
  (income) and running costs to its operating payees (operating), each on the same weekday as
  in the window and in the window's proportions. Receipts follow the inflow baselines, falling
  evenly from `weekly_inflow_3mo_ago_cents`, flat at `weekly_inflow_typical_cents` before this
  week, or flat; running costs are `weekly_operating_outflow_cents` every week. Thirteen weeks
  (`trailing_window_weeks`) before the window, txn_<customer>_w<weeks back>_<nn>. The file's
  `income_trend` label is stored with them, as the file states it.
- Payroll and tax recur at their own pace over those weeks: each payroll run of the window every
  two weeks before it, each tax payment one quarter (13 weeks) before it, to the same payee on the
  same weekday at the same amount, txn_<customer>_w<weeks back>_<nn> too.
- `payee_history_hint` becomes earlier payments to the payee of the customer's first payment to
  a vendor paid before, the first one `first_seen_months_ago` months back: txn_<customer>_p<nn>.
  No other row goes to the payee of another vendor, a transfer or a new payee, so those payees of
  the window were never paid before.
- `prior_year_same_two_weeks` is every receipt and payment of the ISO weeks of last year that the
  window's days fall in, split evenly over those weeks and then as in a week before the window:
  txn_<customer>_y<n>_<nn> for the n-th of those weeks. Each week's totals are a prior_year_weekly row.
  Last year's large payments are among those running costs: a row to the story's vendor would give
  it a payee history, which the rules read (G-02).
- A linked account's `name` and `mask` (the last digits of its number) are stored as the file gives
  them, and left empty when it gives none.
"""

from __future__ import annotations

import os
from calendar import monthrange
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from app.db.models import (
    BankClock,
    BankTransaction,
    Counterparty,
    Customer,
    DailyBalance,
    LinkedAccount,
    PriorYearWeekly,
)

# Counterparties every customer shares. Every other payee or payer is a row of its own.
_SHARED_PAYEES = {"payroll_vendor": "Payroll processor", "tax_authority": "Tax authority"}
# Why a file may leave part of the week's fall to no transaction (signal_window.intended_gap).
_SILENT_ACCOUNT, _UNRECONCILED = "account_not_reporting", "books_do_not_reconcile"
# How often a payroll run and a tax payment recur, in weeks: every two weeks, and every quarter.
_EVERY_WEEKS = {"payroll": 2, "tax": 13}


def default_paths() -> list[Path]:
    """Where the seeder reads from when no path is passed.

    SHARED_CUSTOMERS_YAML, one path or several separated by ":"; else the image's copy at
    /app/data/...; else the repository's data/ directory (when running tests locally).
    """
    if named := [name for name in os.environ.get("SHARED_CUSTOMERS_YAML", "").split(os.pathsep) if name]:
        return [Path(name) for name in named]
    in_container = Path("/app/data/shared_customers.yaml")
    if in_container.exists():
        return [in_container]
    # services/bank/app/db/seed.py -> repo root is parents[4]
    return [Path(__file__).resolve().parents[4] / "data" / "shared_customers.yaml"]


def load(paths: list[Path] | None = None) -> dict:
    """The clock of the first file, and every customer of every file in order."""
    files = [yaml.safe_load(Path(path).read_text()) for path in (paths or default_paths())]
    customers, seen = [], set()
    for data in files:
        for customer in data["customers"]:
            if customer["id"] in seen:
                raise ValueError(f"{customer['id']} is in more than one data file")
            seen.add(customer["id"])
            customers.append(customer)
    return {"clock": files[0]["clock"], "customers": customers}


def _months_back(day: date, months: int) -> date:
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    return date(year, month + 1, min(day.day, monthrange(year, month + 1)[1]))


def _year_back(day: date) -> date:
    return day.replace(year=day.year - 1) if (day.month, day.day) != (2, 29) else date(day.year - 1, 2, 28)


def prior_year_weeks(start: date, end: date) -> list[tuple[int, int]]:
    """The (ISO year, ISO week) pairs of last year that the days start < d <= end fall in, in order."""
    weeks = []
    for offset in range(1, (end - start).days + 1):
        iso = _year_back(start + timedelta(days=offset)).isocalendar()
        if (iso.year, iso.week) not in weeks:
            weeks.append((iso.year, iso.week))
    return weeks


def _split(total: int, parts: int) -> list[int]:
    """total in `parts` whole-cent pieces, the remainder on the last one."""
    base = int(total / parts)  # toward zero, so a negative total splits like a positive one
    return [base] * (parts - 1) + [total - base * (parts - 1)]


def _in_proportion(total: int, weights: list[int]) -> list[int]:
    """total in whole cents, in the proportions of the weights, the remainder on the first."""
    whole = sum(weights)
    amounts = [total * weight // whole for weight in weights]
    amounts[0] += total - sum(amounts)
    return amounts


def _signed(event: dict) -> int:
    """Income comes in; every other category goes out."""
    amount = int(event["amount_cents"])
    return amount if event["category"] == "income" else -amount


def _weeks_back(weeks: int) -> Callable[[date], date]:
    """The same weekday, that many weeks earlier."""
    return lambda day: day - timedelta(days=7 * weeks)


def _on_weekday_of(year: int, week: int) -> Callable[[date], date]:
    """The day of that ISO week that falls on the same weekday as a given day."""
    return lambda day: date.fromisocalendar(year, week, day.isoweekday())


class _Customer:
    """One customer's rows, worked out from the file before anything is written."""

    def __init__(self, customer: dict, clock: dict):
        self.id, self.customer = customer["id"], customer
        self.as_of = date.fromisoformat(clock["signal_date"])
        self.window_days, self.weeks = int(clock["signal_window_days"]), int(clock["trailing_window_weeks"])
        self.start = self.as_of - timedelta(days=self.window_days)
        bank = customer["snapshot"]["bank"]
        self.baseline = customer["baselines"]["bank"]
        self.events = customer["signal_window"].get("bank_events") or []
        self.now, self.ago = int(bank["cash_now_cents"]), int(bank["cash_week_ago_cents"])
        self.accounts = bank["linked_accounts"]
        if not self.accounts:
            raise ValueError(f"{self.id} has no linked account")
        for account in self.accounts:
            if not account.get("active", True) and not account.get("days_stale"):
                raise ValueError(f"{account['id']} is inactive: say in days_stale when it stopped reporting")
            if int(account.get("days_stale", 0)) >= self.window_days:
                raise ValueError(f"{account['id']}: days_stale must be under the window's {self.window_days} days")
        self.reporting = [a for a in self.accounts if not a.get("days_stale") and a.get("active", True)]
        self.silent = [a for a in self.accounts if a not in self.reporting]
        if not self.reporting:
            raise ValueError(f"{self.id} has no account that reports today")
        # Shapes the seeder cannot turn into rows that say what the file says.
        if {"weekly_inflow_3mo_ago_cents", "weekly_inflow_typical_cents"} <= set(self.baseline):
            raise ValueError(f"{self.id}: give weekly_inflow_3mo_ago_cents or weekly_inflow_typical_cents, not both")
        if "weekly_operating_outflow_cents" not in self.baseline:
            raise ValueError(f"{self.id}: give weekly_operating_outflow_cents, the running costs of a usual week")
        if self.baseline.get("payee_history_hint") and not any(e["category"] == "vendor_known" for e in self.events):
            raise ValueError(f"{self.id}: payee_history_hint needs a vendor_known event to be about")
        self.gap = self._gap(customer["signal_window"].get("intended_gap"))

    def _gap(self, intended: dict | None) -> int:
        """The part of the week's fall no transaction explains: the file must name it, or have none."""
        came_in = sum(int(e["amount_cents"]) for e in self.events if e["category"] == "income")
        if came_in != int(self.baseline["weekly_inflow_cents"]):
            raise ValueError(
                f"{self.id}: the window's income events add up to {came_in}, "
                f"but weekly_inflow_cents is {self.baseline['weekly_inflow_cents']}"
            )
        net = sum(_signed(e) for e in self.events)
        unexplained = (self.ago - self.now) + net
        named = int((intended or {}).get("amount_cents", 0))
        if unexplained != named:
            raise ValueError(
                f"{self.id}: cash fell by {self.ago - self.now} but the window's transactions add up to {-net}. "
                "List every movement, or name the gap in signal_window.intended_gap"
            )
        if intended:
            reason = intended.get("reason")
            if named <= 0 or reason not in (_SILENT_ACCOUNT, _UNRECONCILED):
                raise ValueError(f"{self.id}: an intended gap is an amount above nothing, for {_SILENT_ACCOUNT} or {_UNRECONCILED}")
            if (reason == _SILENT_ACCOUNT) != bool(self.silent):
                raise ValueError(f"{self.id}: a gap is {_SILENT_ACCOUNT} exactly when a linked account stopped reporting")
        return named

    def _day(self, number: int) -> date:
        """The day of the window the event numbered `number` (from 1) happened on. Never after the signal date."""
        return self.start + timedelta(days=1 + (number - 1) % self.window_days)

    def weekly_inflows(self) -> list[int]:
        """Inflows of the trailing weeks, oldest first, as the baselines describe them."""
        now, n = int(self.baseline["weekly_inflow_cents"]), self.weeks
        if "weekly_inflow_3mo_ago_cents" in self.baseline:
            then = int(self.baseline["weekly_inflow_3mo_ago_cents"])
            return [then + (now - then) * week // (n - 1) for week in range(n)]
        usual = int(self.baseline.get("weekly_inflow_typical_cents", now))
        return [usual] * (n - 1) + [now]

    def compared_with(self) -> str | None:
        if "weekly_inflow_3mo_ago_cents" in self.baseline:
            return "three_months_ago"
        if "weekly_inflow_typical_cents" in self.baseline:
            return "usual"
        return None

    def rows(self, shared: dict[str, Counterparty]) -> tuple[list, list[BankTransaction], list[DailyBalance], list]:
        """Counterparties, transactions, balances and prior-year weeks."""
        primary = self.reporting[0]["id"]
        counterparties, txns = [], []
        hint = self.baseline.get("payee_history_hint")
        with_history = next((n for n, e in enumerate(self.events, 1) if e["category"] == "vendor_known"), None)
        # The window's receipts and running costs, (number, day, amount, counterparty): what recurs every week.
        recurring = {"income": [], "operating": []}
        # Its payroll runs and tax payments, the same with their category: what recurs at its own pace.
        runs = []

        for number, event in enumerate(self.events, 1):
            kind = event["counterparty_kind"]
            if kind in shared:
                payee = shared[kind]
            else:
                if hint and number == with_history:
                    name = hint["counterparty"]
                else:
                    name = event.get("counterparty") or f"{self.id} counterparty {number:02d}"
                payee = Counterparty(display_name=name, kind=kind)
                counterparties.append(payee)
            memo, day = event.get("memo"), self._day(number)
            txns.append(
                BankTransaction(
                    account_id=primary,
                    date=day,
                    amount_cents=_signed(event),
                    category=event["category"],
                    counterparty=payee,
                    ref=f"txn_{self.id}_{number:02d}",
                    to_account=event.get("to_account"),
                    memo=memo.strip() if memo else None,
                )
            )
            if event["category"] in recurring:
                recurring[event["category"]].append((number, day, int(event["amount_cents"]), payee))
            if event["category"] in _EVERY_WEEKS:
                runs.append((number, day, int(event["amount_cents"]), payee, event["category"]))
            if hint and number == with_history:
                txns += self._earlier_payments(hint, payee, primary)

        inflows, costs = self.weekly_inflows(), int(self.baseline["weekly_operating_outflow_cents"])
        for back in range(1, self.weeks + 1):
            # The trailing weeks end with the window. The week before the oldest is like the oldest.
            inflow = inflows[max(0, len(inflows) - 1 - back)]
            for category, total in (("income", inflow), ("operating", costs)):
                txns += self._recurring(primary, recurring[category], category, total, f"w{back:02d}", _weeks_back(back))
        for number, day, amount, payee, category in runs:
            txns += [
                BankTransaction(
                    account_id=primary,
                    date=_weeks_back(back)(day),
                    amount_cents=-amount,
                    category=category,
                    counterparty=payee,
                    ref=f"txn_{self.id}_w{back:02d}_{number:02d}",
                )
                for back in range(_EVERY_WEEKS[category], self.weeks + 1, _EVERY_WEEKS[category])
            ]

        prior_txns, prior_year = self._prior_year(primary, recurring)
        balances = self._balances(primary, [t for t in txns if self.start < t.date <= self.as_of])
        return counterparties, txns + prior_txns, balances, prior_year

    def _recurring(
        self, account: str, events: list[tuple], category: str, total: int, tag: str, when: Callable[[date], date]
    ) -> list[BankTransaction]:
        """`total` of one category over the window's events of it, in their proportions, on the days `when` gives."""
        if not total:
            return []
        if not events:
            raise ValueError(f"{self.id}: {category} of {total} outside the window needs a {category} event in it to recur")
        sign = 1 if category == "income" else -1
        return [
            BankTransaction(
                account_id=account,
                date=when(day),
                amount_cents=sign * amount,
                category=category,
                counterparty=payee,
                ref=f"txn_{self.id}_{tag}_{number:02d}",
            )
            for (number, day, _, payee), amount in zip(events, _in_proportion(total, [e[2] for e in events]))
            if amount
        ]

    def _earlier_payments(self, hint: dict, payee: Counterparty, account: str) -> list[BankTransaction]:
        count = int(hint["n_prior_payments"])
        if "typical_amount_cents" in hint:
            amounts = [int(hint["typical_amount_cents"])] * count
        else:
            low, high = int(hint["typical_amount_cents_min"]), int(hint["typical_amount_cents_max"])
            if count < 2:
                raise ValueError(f"{self.id}: a range of amounts needs at least two earlier payments")
            amounts = [low + (high - low) * n // (count - 1) for n in range(count)]
        first = _months_back(self.as_of, int(hint["first_seen_months_ago"]))
        last = self.start - timedelta(days=1)
        if count and first > last:
            raise ValueError(f"{self.id}: the payee's first payment falls in the window")
        span = (last - first).days
        return [
            BankTransaction(
                account_id=account,
                date=first + timedelta(days=span * n // (count - 1) if count > 1 else 0),
                amount_cents=-amount,
                category="vendor_known",
                counterparty=payee,
                ref=f"txn_{self.id}_p{n + 1:02d}",
            )
            for n, amount in enumerate(amounts)
        ]

    def _balances(self, primary: str, window_txns: list[BankTransaction]) -> list[DailyBalance]:
        """Closing balances from the window's first day to the signal date.

        The first reporting account carries every transaction. A silent account holds the gap, what
        the reporting side cannot explain, until the day it stopped reporting. Books that do not
        reconcile fall by the gap on top of the transactions, a share of it each day.
        """
        drift = 0 if self.silent else -self.gap
        opening = self.now - sum(t.amount_cents for t in window_txns) - drift
        silent_holds = self.ago - opening
        if silent_holds and not self.silent:
            raise ValueError(f"{self.id}: balances do not add up")  # cannot happen by construction
        if silent_holds < 0:
            raise ValueError(f"{self.id}: the silent accounts would hold less than nothing")
        rows = []
        for offset in range(self.window_days + 1):
            day = self.start + timedelta(days=offset)
            moved = sum(t.amount_cents for t in window_txns if t.date <= day)
            spread = drift * offset // self.window_days
            for account in self.accounts:
                if account in self.silent and day > self.as_of - timedelta(days=int(account.get("days_stale", 0))):
                    continue
                if account["id"] == primary:
                    closing = opening + moved + spread
                elif self.silent and account is self.silent[0]:
                    closing = silent_holds
                else:
                    closing = 0
                rows.append(DailyBalance(account_id=account["id"], date=day, closing_cents=closing))
        return rows

    def _prior_year(self, account: str, recurring: dict[str, list]) -> tuple[list[BankTransaction], list[PriorYearWeekly]]:
        last_year = self.baseline.get("prior_year_same_two_weeks")
        if not last_year:
            return [], []
        weeks = prior_year_weeks(self.start, self.as_of)
        inflows = _split(int(last_year["inflow_cents"]), len(weeks))
        outflows = _split(int(last_year["outflow_cents"]), len(weeks))
        txns, totals = [], []
        for number, ((year, week), inflow, outflow) in enumerate(zip(weeks, inflows, outflows), 1):
            for category, total in (("income", inflow), ("operating", outflow)):
                txns += self._recurring(account, recurring[category], category, total, f"y{number:02d}", _on_weekday_of(year, week))
            totals.append(
                PriorYearWeekly(customer_id=self.id, year=year, week=week, inflow_cents=inflow, outflow_cents=outflow)
            )
        return txns, totals


def seed(session: Session, yaml_paths: list[Path] | None = None) -> dict:
    """Populate every table. Flushes but does not commit. Returns row counts."""
    data = load(yaml_paths)
    clock = data["clock"]
    session.add(
        BankClock(
            id=1,
            signal_date=date.fromisoformat(clock["signal_date"]),
            window_days=int(clock["signal_window_days"]),
            trailing_weeks=int(clock["trailing_window_weeks"]),
        )
    )
    shared = {kind: Counterparty(display_name=name, kind=kind) for kind, name in _SHARED_PAYEES.items()}
    session.add_all(shared.values())
    totals = {"customers": 0, "transactions": 0, "balances": 0}

    for customer in data["customers"]:
        plan = _Customer(customer, clock)
        counterparties, txns, balances, prior_year = plan.rows(shared)
        opened = min([plan.as_of - timedelta(days=730), *(t.date for t in txns)])
        session.add(
            Customer(
                customer_id=plan.id,
                name=customer["name"],
                created_at=datetime.combine(opened, datetime.min.time()),
                income_trend=plan.baseline["income_trend"],
                inflow_compared_with=plan.compared_with(),
            )
        )
        for account in plan.accounts:
            session.add(
                LinkedAccount(
                    id=account["id"],
                    customer_id=plan.id,
                    type="checking",
                    opened_at=opened,
                    last_reported_at=plan.as_of - timedelta(days=int(account.get("days_stale", 0))),
                    active=bool(account.get("active", True)),
                    name=account.get("name"),
                    mask=account.get("mask"),
                )
            )
        session.flush()  # accounts before the rows that point at them
        session.add_all([*counterparties, *txns, *balances, *prior_year])
        session.flush()
        totals["customers"] += 1
        totals["transactions"] += len(txns)
        totals["balances"] += len(balances)
    return totals


def is_empty(session: Session) -> bool:
    return session.query(Customer).first() is None


def seed_if_empty(session: Session, yaml_paths: list[Path] | None = None) -> dict | None:
    """Run seed() only if customers is empty. Commits on success."""
    if not is_empty(session):
        return None
    summary = seed(session, yaml_paths)
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
