"""One customer's bank ledger, read once per request, and every record the tools return.

Every figure is worked out here, in code, from the rows and the service clock. A record is built
by one function whatever tool returns it, so the same id always carries the same content.
Measures only: shares of the fall, ratios against the usual, counts of days. Thresholds belong to
the Orchestrator's guidelines.

A customer has a few thousand rows at most, so one load and in-memory sums are simple and give
the same answer on SQLite and Postgres.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median_low

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import evidence as ev
from app.db.models import BankClock, BankTransaction, Counterparty, Customer, DailyBalance, LinkedAccount, PriorYearWeekly
from app.db.seed import prior_year_weeks


# The payments whose payee history is set against what the payee was usually paid (vs_usual_bp): a vendor's.
# The Orchestrator's G-02 reads vs_usual_bp on any payee history, whatever payment it is about, so a running
# cost or a payroll run in line with its own history would pass for the vendor's.
SET_AGAINST_THE_USUAL = ("vendor_known",)


def bp(part: int, whole: int) -> int | None:
    """part as a share of whole, in basis points rounded half up. None when there is no whole."""
    return (10000 * part + whole // 2) // whole if whole > 0 else None


def months_between(earlier: date, later: date) -> int:
    """Whole months from earlier to later."""
    months = (later.year - earlier.year) * 12 + later.month - earlier.month
    return months - 1 if later.day < earlier.day else months


@dataclass(frozen=True)
class Record:
    id: str
    kind: str
    data: dict

    def evidence(self) -> dict:
        return {"id": self.id, "kind": self.kind, "data": self.data}


@dataclass(frozen=True)
class Txn:
    row_id: int
    ref: str | None
    account_id: str
    day: date
    amount_cents: int
    category: str
    counterparty_id: int | None
    counterparty_kind: str | None
    to_account: str | None
    memo: str | None


class Ledger:
    def __init__(self, session: Session, customer: Customer):
        self.id = customer.customer_id
        self.customer = customer
        clock = session.get(BankClock, 1)
        # The day the data is read on: AS_OF_DATE, or else the signal date the data was generated for.
        self.as_of = date.fromisoformat(os.environ["AS_OF_DATE"]) if os.environ.get("AS_OF_DATE") else clock.signal_date
        self.weeks = clock.trailing_weeks
        self.window = (self.as_of - timedelta(days=clock.window_days), self.as_of)
        self.accounts = list(
            session.scalars(select(LinkedAccount).where(LinkedAccount.customer_id == self.id).order_by(LinkedAccount.id))
        )
        account_ids = [account.id for account in self.accounts]
        self.balances: dict[date, dict[str, int]] = {}
        for row in session.scalars(select(DailyBalance).where(DailyBalance.account_id.in_(account_ids))):
            self.balances.setdefault(row.date, {})[row.account_id] = int(row.closing_cents)
        rows = session.execute(
            select(BankTransaction, Counterparty.kind)
            .outerjoin(Counterparty, BankTransaction.counterparty_id == Counterparty.id)
            .where(BankTransaction.account_id.in_(account_ids))
        ).all()
        self.txns = sorted(
            (
                Txn(t.id, t.ref, t.account_id, t.date, int(t.amount_cents), t.category, t.counterparty_id, kind,
                    t.to_account, t.memo)
                for t, kind in rows
            ),
            key=lambda t: (t.day, t.ref, t.row_id),
        )
        self.prior_year_rows = {
            (row.year, row.week): row
            for row in session.scalars(select(PriorYearWeekly).where(PriorYearWeekly.customer_id == self.id))
        }

    # -- the window and its figures ---------------------------------------------------------

    def suffix(self, start: date, end: date) -> str:
        return ev.window_suffix(start, end, self.window)

    def cash_on(self, day: date) -> int | None:
        """Linked cash at the end of a day: the balances reported that day. None when none was."""
        reported = self.balances.get(day)
        return sum(reported.values()) if reported else None

    def in_window(self, txn: Txn, start: date, end: date) -> bool:
        return start < txn.day <= end

    def listed(self, start: date, end: date) -> list[Txn]:
        """The transactions of a window: every one, since every one is itemised."""
        return [t for t in self.txns if self.in_window(t, start, end)]

    def fall(self, start: date, end: date) -> int | None:
        before, after = self.cash_on(start), self.cash_on(end)
        return None if before is None or after is None else before - after

    def stale(self) -> list[LinkedAccount]:
        return [a for a in self.accounts if (self.as_of - a.last_reported_at).days > 0 or not a.active]

    def shares(self, start: date, end: date) -> int | None:
        """The fall that shares are worked out against: only when cash fell and every account reports."""
        fall = self.fall(start, end)
        return fall if fall is not None and fall > 0 and not self.stale() else None

    # -- records ----------------------------------------------------------------------------

    def account(self, account: LinkedAccount) -> Record:
        days = (self.as_of - account.last_reported_at).days
        data = {
            "active": account.active,
            "days_stale": days,
            "last_synced": account.last_reported_at.isoformat(),
            "is_stale": days > 0 or not account.active,
        }
        # How a person knows the account, only when the data names it: a row seeded without them has neither key.
        if account.name is not None:
            data["name"] = account.name
        if account.mask is not None:
            data["mask"] = account.mask
        return Record(account.id, ev.LINKED_ACCOUNT, data)

    def txn(self, txn: Txn) -> Record:
        data = {"date": txn.day.isoformat(), "amount_cents": txn.amount_cents, "category": txn.category}
        if txn.counterparty_kind:
            data["counterparty_kind"] = txn.counterparty_kind
        if txn.to_account:
            data["to_account"] = txn.to_account
        if txn.memo:
            data["memo"] = txn.memo
        # Its share of the review window's fall, the same whichever window it is listed in.
        fall = self.shares(*self.window)
        if fall is not None and txn.amount_cents < 0 and self.in_window(txn, *self.window):
            data["share_of_fall_bp"] = bp(-txn.amount_cents, fall)
        return Record(txn.ref, ev.BANK_TRANSACTION, data)

    def balance(self, start: date, end: date) -> Record | None:
        before, after = self.cash_on(start), self.cash_on(end)
        if before is None or after is None:
            return None
        data = {"cash_week_ago_cents": before, "cash_now_cents": after, "change_cents": after - before}
        if (start, end) != self.window:
            data |= {"start": start.isoformat(), "end": end.isoformat()}
        return Record(ev.balance_id(self.id, self.suffix(start, end)), ev.DAILY_BALANCE, data)

    def payment_summary(self, start: date, end: date) -> Record | None:
        fall = self.shares(start, end)
        if fall is None:
            return None
        listed = self.listed(start, end)
        out = [t for t in listed if t.amount_cents < 0]
        listed_out = -sum(t.amount_cents for t in out)
        # Above 100% when income came in: the fall is what went out less what came in.
        summary = {"listed_count": len(out), "listed_out_cents": listed_out, "listed_share_bp": bp(listed_out, fall)}
        # Listed income is set out on its own, never netted against the payments.
        if income := [t.amount_cents for t in listed if t.amount_cents > 0]:
            summary["listed_in_cents"] = sum(income)
        by_category: dict[str, dict] = {}
        for t in out:
            category = by_category.setdefault(t.category, {"count": 0, "out_cents": 0})
            category["count"] += 1
            category["out_cents"] -= t.amount_cents
        if payroll_and_tax := [by_category[name]["out_cents"] for name in ("payroll", "tax") if name in by_category]:
            summary["payroll_and_tax_cents"] = sum(payroll_and_tax)
            summary["payroll_and_tax_share_bp"] = bp(sum(payroll_and_tax), fall)
        for category in by_category.values():
            category["share_bp"] = bp(category["out_cents"], fall)
        summary["by_category"] = by_category
        if (start, end) != self.window:
            summary |= {"start": start.isoformat(), "end": end.isoformat()}
        return Record(ev.aggregate_id(self.id, "payments", self.suffix(start, end)), ev.PAYMENT_SUMMARY, summary)

    def reconciliation(self, start: date, end: date) -> Record | None:
        """Whether the balances moved by what the transactions add up to. Exact, to the cent."""
        fall = self.shares(start, end)
        if fall is None:
            return None
        total = sum(t.amount_cents for t in self.txns if self.in_window(t, start, end))
        books = {"balance_change_cents": -fall, "transactions_total_cents": total, "reconciled": total == -fall}
        if total != -fall:
            unmatched = total + fall
            books |= {"unmatched_cents": unmatched, "unmatched_share_bp": bp(unmatched, fall)}
        if (start, end) != self.window:
            books |= {"start": start.isoformat(), "end": end.isoformat()}
        return Record(ev.aggregate_id(self.id, "reconciliation", self.suffix(start, end)), ev.BALANCE_RECONCILIATION, books)

    def inflows(self) -> Record:
        """Weekly inflows over the trailing weeks that end on the as-of date, oldest first."""
        series = []
        for back in range(self.weeks - 1, -1, -1):
            end = self.as_of - timedelta(days=7 * back)
            inflow = sum(t.amount_cents for t in self.txns if t.amount_cents > 0 and end - timedelta(days=7) < t.day <= end)
            series.append({"week_end": end.isoformat(), "inflow_cents": inflow})
        now = series[-1]["inflow_cents"]
        data = {"weekly_inflow_cents": now, "income_trend": self.customer.income_trend, "weeks": self.weeks}
        level = self.customer.inflow_compared_with
        if level == "three_months_ago":
            data["weekly_inflow_3mo_ago_cents"] = against = series[0]["inflow_cents"]
        elif level == "usual":
            data["weekly_inflow_typical_cents"] = against = median_low(week["inflow_cents"] for week in series[:-1])
        if level:
            data["compared_with"] = level
            if (share := bp(now, against)) is not None:
                data["vs_compared_bp"] = share
        data["series"] = series
        return Record(ev.aggregate_id(self.id, "inflows"), ev.WEEKLY_INFLOW_SERIES, data)

    def prior_year(self) -> Record | None:
        start, end = self.window
        rows = [self.prior_year_rows[week] for week in prior_year_weeks(start, end) if week in self.prior_year_rows]
        if not rows:
            return None
        outflow = sum(int(row.outflow_cents) for row in rows)
        # Every payment of the window against every payment of last year's weeks: the same kinds of movement.
        window_out = -sum(t.amount_cents for t in self.listed(start, end) if t.amount_cents < 0)
        data = {
            "outflow_cents": outflow,
            "inflow_cents": sum(int(row.inflow_cents) for row in rows),
            "period_days": 7 * len(rows),
            "window_days": (end - start).days,
            "window_out_cents": window_out,
        }
        if (share := bp(window_out, outflow)) is not None:
            data["window_vs_prior_out_bp"] = share
        return Record(ev.aggregate_id(self.id, "prior_year"), ev.PRIOR_YEAR_COMPARISON, data)

    def find(self, ref: str) -> Txn | None:
        return next((t for t in self.txns if t.ref == ref), None)

    def _earlier_payments(self, txn: Txn) -> list[Txn]:
        """The payments to this payment's payee before it, whatever it was paid for."""
        if txn.amount_cents >= 0 or txn.counterparty_id is None:
            return []
        return [
            t
            for t in self.txns
            if t.amount_cents < 0 and t.counterparty_id == txn.counterparty_id and t.day < txn.day
        ]

    def payee_history(self, txn: Txn) -> Record | None:
        """The payee's earlier payments, set against this one. None when the payee was never paid before."""
        earlier = self._earlier_payments(txn)
        if not earlier:
            return None
        amounts = [-t.amount_cents for t in earlier]
        history = {"n_prior_payments": len(earlier)}
        if min(amounts) == max(amounts):
            history["typical_amount_cents"] = amounts[0]
        else:
            history |= {"typical_amount_cents_min": min(amounts), "typical_amount_cents_max": max(amounts)}
        history["first_seen_months_ago"] = months_between(earlier[0].day, self.as_of)
        amount, top = -txn.amount_cents, max(amounts)
        history |= {"about": txn.ref, "amount_cents": amount, "usual_top_cents": top}
        if txn.category in SET_AGAINST_THE_USUAL and (share := bp(amount, top)) is not None:
            history["vs_usual_bp"] = share
        # The review window's first payment to a vendor paid before has the plain id; any other, its number.
        first = next(
            (t for t in self.listed(*self.window) if t.category == "vendor_known" and self._earlier_payments(t)), None
        )
        suffix = "" if first is not None and first.ref == txn.ref else "_" + txn.ref.removeprefix(f"txn_{self.id}_")
        return Record(ev.aggregate_id(self.id, "payee_history", suffix), ev.PAYEE_HISTORY, history)
