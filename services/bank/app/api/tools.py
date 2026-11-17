"""Read-only analysis tools on the Bank Service.

Each endpoint is one SQL query under the hood. The Cash Flow agent in the
Orchestrator Service calls them over HTTP with a short-lived service token
(see auth.require_service_access). Every response has the same envelope:
{data, evidence} — data is the tool's answer, evidence lists every record the
tool looked at with a stable id the report can later cite.

Must tools (KAN-20, first wave):
- cash_summary       — opening, closing, inflows, outflows, totals by category
- transactions       — the raw transactions in a window, with memo and payee
- income_trend       — weekly inflows for trailing N weeks, with slope/zscore

Should tools (second wave):
- payee_history      — counts and amounts for one counterparty
- account_reporting  — per-account last_reported_at + days_stale
- balance_vs_txn     — balance delta vs. txn sum in a window, with discrepancy
- prior_year_compare — same window one year earlier
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from statistics import mean, stdev
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.auth import require_service_access
from app.api.evidence import (
    ACCOUNT_REPORTING_STATUS,
    BALANCE_VS_TXN_RECONCILIATION,
    BANK_TRANSACTION,
    CASH_SUMMARY,
    DAILY_BALANCE,
    PAYEE_HISTORY,
    PRIOR_YEAR_COMPARISON,
    WEEKLY_INFLOW_SERIES,
    aggregate_evidence_id,
    balance_evidence_id,
    txn_evidence_id,
)
from app.api.schemas import Evidence, ToolResponse
from app.db import get_session
from app.db.models import (
    BankTransaction,
    Counterparty,
    Customer,
    DailyBalance,
    LinkedAccount,
    PriorYearWeekly,
)

router = APIRouter(prefix="/customers/{customer_id}/tools")

# The scenario clock. Treated as a constant for the POC. The team has not
# fixed AS_OF_DATE across services yet (KAN-14).
_RECONCILE_TOLERANCE_CENTS = 10_000_000  # $100K; c_012's deliberate gap is ~$195K


def _ensure_customer(session: Session, customer_id: str) -> None:
    if not session.get(Customer, customer_id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"error": "not_found", "message": f"Unknown customer {customer_id}"},
        )


def _account_ids_for(session: Session, customer_id: str) -> list[str]:
    return list(
        session.scalars(
            select(LinkedAccount.id).where(LinkedAccount.customer_id == customer_id)
        )
    )


# ---------------------------------------------------------------------------
# cash_summary
# ---------------------------------------------------------------------------

@router.get("/cash_summary", response_model=ToolResponse)
def get_cash_summary(
    customer_id: Annotated[str, Path(min_length=1)],
    start: Annotated[date, Query(description="window start, inclusive")],
    end: Annotated[date, Query(description="window end, inclusive")],
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[object, Depends(require_service_access)],
) -> ToolResponse:
    """Opening, closing, inflow/outflow totals and totals by category."""
    if start > end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start is after end")

    _ensure_customer(session, customer_id)
    accounts = _account_ids_for(session, customer_id)
    if not accounts:
        return ToolResponse(data={"opening_cents": 0, "closing_cents": 0,
                                   "inflow_total_cents": 0, "outflow_total_cents": 0,
                                   "by_category": {}},
                            evidence=[])

    # In the seeded data `start` is the first day of balance history, so its
    # closing acts as the opening-of-window balance. The scenarios have no
    # transactions on `start` itself, so this does not double-count activity.
    opening_row = _closing_on_or_before(session, accounts, start)
    closing_row = _closing_on_or_before(session, accounts, end)

    opening_cents = sum(closing for _, closing, _ in opening_row.values()) if opening_row else 0
    closing_cents = sum(closing for _, closing, _ in closing_row.values()) if closing_row else 0

    txns = session.scalars(
        select(BankTransaction)
        .join(LinkedAccount, BankTransaction.account_id == LinkedAccount.id)
        .where(LinkedAccount.customer_id == customer_id)
        .where(BankTransaction.date >= start)
        .where(BankTransaction.date <= end)
        .order_by(BankTransaction.date, BankTransaction.id)
    ).all()

    inflow = sum(t.amount_cents for t in txns if t.amount_cents > 0)
    outflow = sum(t.amount_cents for t in txns if t.amount_cents < 0)
    by_category: dict[str, int] = defaultdict(int)
    for t in txns:
        by_category[t.category] += t.amount_cents

    data = {
        "opening_cents": opening_cents,
        "closing_cents": closing_cents,
        "inflow_total_cents": inflow,
        "outflow_total_cents": outflow,
        "by_category": dict(by_category),
    }

    evidence = _balances_as_evidence(opening_row) + _balances_as_evidence(closing_row)
    evidence.append(
        Evidence(
            id=aggregate_evidence_id("cash_summary", customer_id, start.isoformat(), end.isoformat()),
            kind=CASH_SUMMARY,
            data=data,
        )
    )
    return ToolResponse(data=data, evidence=evidence)


def _closing_on_or_before(
    session: Session, account_ids: list[str], day: date
) -> dict[str, tuple[date, int, str]]:
    """Per-account tuple of (date, closing_cents, evidence_id) for the latest
    daily_balances row on or before `day`. Missing accounts are omitted (c_005)."""
    out: dict[str, tuple[date, int, str]] = {}
    for acct_id in account_ids:
        row = session.execute(
            select(DailyBalance.date, DailyBalance.closing_cents)
            .where(DailyBalance.account_id == acct_id)
            .where(DailyBalance.date <= day)
            .order_by(DailyBalance.date.desc())
            .limit(1)
        ).first()
        if row:
            out[acct_id] = (row.date, int(row.closing_cents), balance_evidence_id(acct_id, row.date))
    return out


def _balances_as_evidence(rows: dict[str, tuple[date, int, str]]) -> list[Evidence]:
    return [
        Evidence(
            id=eid,
            kind=DAILY_BALANCE,
            data={"account_id": acct_id, "date": d.isoformat(), "closing_cents": closing},
        )
        for acct_id, (d, closing, eid) in rows.items()
    ]


# ---------------------------------------------------------------------------
# transactions
# ---------------------------------------------------------------------------

@router.get("/transactions", response_model=ToolResponse)
def list_transactions(
    customer_id: Annotated[str, Path(min_length=1)],
    start: Annotated[date, Query()],
    end: Annotated[date, Query()],
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[object, Depends(require_service_access)],
    min_abs_amount_cents: Annotated[int | None, Query(ge=0)] = None,
    category: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ToolResponse:
    """Raw transactions in the window, newest-last. Filters are optional."""
    if start > end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start is after end")

    _ensure_customer(session, customer_id)

    stmt = (
        select(BankTransaction, Counterparty.display_name)
        .join(LinkedAccount, BankTransaction.account_id == LinkedAccount.id)
        .outerjoin(Counterparty, BankTransaction.counterparty_id == Counterparty.id)
        .where(LinkedAccount.customer_id == customer_id)
        .where(BankTransaction.date >= start)
        .where(BankTransaction.date <= end)
    )
    if category:
        stmt = stmt.where(BankTransaction.category == category)
    if min_abs_amount_cents is not None:
        stmt = stmt.where(func.abs(BankTransaction.amount_cents) >= min_abs_amount_cents)
    stmt = stmt.order_by(BankTransaction.date, BankTransaction.id).limit(limit + 1)

    rows = session.execute(stmt).all()
    truncated = len(rows) > limit
    rows = rows[:limit]

    listed = []
    evidence = []
    for txn, cp_name in rows:
        listed.append(
            {
                "id": txn_evidence_id(txn.id),
                "date": txn.date.isoformat(),
                "amount_cents": txn.amount_cents,
                "category": txn.category,
                "counterparty_display": cp_name,
                "memo": txn.memo,
            }
        )
        evidence.append(
            Evidence(
                id=txn_evidence_id(txn.id),
                kind=BANK_TRANSACTION,
                data={
                    "date": txn.date.isoformat(),
                    "amount_cents": txn.amount_cents,
                    "category": txn.category,
                    "counterparty_display": cp_name,
                    "memo": txn.memo,
                },
            )
        )

    return ToolResponse(
        data={"transactions": listed, "truncated": truncated},
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# income_trend
# ---------------------------------------------------------------------------

@router.get("/income_trend", response_model=ToolResponse)
def get_income_trend(
    customer_id: Annotated[str, Path(min_length=1)],
    end: Annotated[date, Query(description="trend ends at the week containing `end`")],
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[object, Depends(require_service_access)],
    weeks: Annotated[int, Query(ge=2, le=52)] = 13,
) -> ToolResponse:
    """Weekly inflow series ending at `end`. Returns slope (cents/week) and
    the z-score of the last week. enough_history=false if fewer than `weeks`
    weeks of inflow data are available (the agent should treat the trend as
    unreliable then)."""
    _ensure_customer(session, customer_id)

    # Normalise `end` to that week's Monday so the buckets align.
    last_week_start = end - timedelta(days=end.weekday())
    first_week_start = last_week_start - timedelta(weeks=weeks - 1)

    rows = session.execute(
        select(BankTransaction.date, BankTransaction.amount_cents)
        .join(LinkedAccount, BankTransaction.account_id == LinkedAccount.id)
        .where(LinkedAccount.customer_id == customer_id)
        .where(BankTransaction.amount_cents > 0)  # inflows only
        .where(BankTransaction.date >= first_week_start)
        .where(BankTransaction.date <= last_week_start + timedelta(days=6))
    ).all()

    buckets: dict[date, int] = {
        first_week_start + timedelta(weeks=k): 0 for k in range(weeks)
    }
    for d, amount in rows:
        week_start = d - timedelta(days=d.weekday())
        if week_start in buckets:
            buckets[week_start] += int(amount)

    series = sorted(buckets.items())
    inflows = [cents for _, cents in series]

    slope = _simple_slope(inflows)
    zscore = _zscore_of_last(inflows)
    # "enough history" = every one of the requested weeks has at least one
    # inflow record. c_005 has no signal-window events but has baselines:
    # the seeder only lays down the signal window, so most customers will
    # fail this on real data. We deliberately keep the check strict: the
    # agent prefers "too short to tell" over a trend from one data point.
    enough_history = all(cents > 0 for cents in inflows)

    data = {
        "weeks": [{"week_start": d.isoformat(), "inflow_cents": c} for d, c in series],
        "slope_cents_per_week": slope,
        "zscore_of_last_week": zscore,
        "enough_history": enough_history,
    }
    evidence = [
        Evidence(
            id=aggregate_evidence_id(
                "income_trend",
                customer_id,
                last_week_start.isoformat(),
                str(weeks),
            ),
            kind=WEEKLY_INFLOW_SERIES,
            data=data,
        )
    ]
    return ToolResponse(data=data, evidence=evidence)


def _simple_slope(values: list[int]) -> float:
    """Linear regression slope with x = 0, 1, 2, .... In cents per week."""
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den else 0.0


def _zscore_of_last(values: list[int]) -> float | None:
    if len(values) < 2:
        return None
    head = values[:-1]
    if len(head) < 2:
        return None
    try:
        s = stdev(head)
    except Exception:
        return None
    if s == 0:
        return None
    return (values[-1] - mean(head)) / s


# ---------------------------------------------------------------------------
# payee_history (Should)
# ---------------------------------------------------------------------------

@router.get("/payee_history", response_model=ToolResponse)
def get_payee_history(
    customer_id: Annotated[str, Path(min_length=1)],
    counterparty_id: Annotated[int, Query(description="Counterparty.id to look up")],
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[object, Depends(require_service_access)],
) -> ToolResponse:
    """Counts, amount stats, first/last seen for one counterparty's history
    with this customer. Used to decide whether a vendor is new or recurring."""
    _ensure_customer(session, customer_id)
    counterparty = session.get(Counterparty, counterparty_id)
    if counterparty is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"error": "not_found", "message": f"Unknown counterparty {counterparty_id}"},
        )

    rows = session.execute(
        select(BankTransaction.id, BankTransaction.date, BankTransaction.amount_cents)
        .join(LinkedAccount, BankTransaction.account_id == LinkedAccount.id)
        .where(LinkedAccount.customer_id == customer_id)
        .where(BankTransaction.counterparty_id == counterparty_id)
        .order_by(BankTransaction.date)
    ).all()

    amounts_abs = [abs(int(r.amount_cents)) for r in rows]
    data = {
        "counterparty_id": counterparty_id,
        "counterparty_display": counterparty.display_name,
        "n_payments": len(rows),
        "min_amount_cents": min(amounts_abs) if amounts_abs else None,
        "max_amount_cents": max(amounts_abs) if amounts_abs else None,
        "mean_amount_cents": (sum(amounts_abs) / len(amounts_abs)) if amounts_abs else None,
        "first_seen": rows[0].date.isoformat() if rows else None,
        "last_seen": rows[-1].date.isoformat() if rows else None,
        "is_new": len(rows) <= 1,
    }
    evidence = [
        Evidence(
            id=txn_evidence_id(r.id),
            kind=BANK_TRANSACTION,
            data={"date": r.date.isoformat(), "amount_cents": int(r.amount_cents)},
        )
        for r in rows
    ]
    evidence.append(
        Evidence(
            id=aggregate_evidence_id("payee_history", customer_id, str(counterparty_id)),
            kind=PAYEE_HISTORY,
            data=data,
        )
    )
    return ToolResponse(data=data, evidence=evidence)


# ---------------------------------------------------------------------------
# account_reporting (Should) — c_005
# ---------------------------------------------------------------------------

@router.get("/account_reporting", response_model=ToolResponse)
def check_account_reporting(
    customer_id: Annotated[str, Path(min_length=1)],
    as_of: Annotated[date, Query(description="compute days_stale against this date")],
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[object, Depends(require_service_access)],
) -> ToolResponse:
    """Per linked account, when it last reported and how stale that is."""
    _ensure_customer(session, customer_id)

    accounts = session.scalars(
        select(LinkedAccount)
        .where(LinkedAccount.customer_id == customer_id)
        .order_by(LinkedAccount.id)
    ).all()

    listed = []
    evidence = []
    for a in accounts:
        days_stale = (as_of - a.last_reported_at).days
        listed.append(
            {
                "id": a.id,
                "last_reported_at": a.last_reported_at.isoformat(),
                "days_stale": days_stale,
                "active": a.active,
            }
        )
        evidence.append(
            Evidence(
                id=a.id,  # row-level account id is already prefixed `acct_*`
                kind=ACCOUNT_REPORTING_STATUS,
                data={
                    "customer_id": customer_id,
                    "last_reported_at": a.last_reported_at.isoformat(),
                    "days_stale": days_stale,
                    "active": a.active,
                },
            )
        )

    return ToolResponse(data={"accounts": listed}, evidence=evidence)


# ---------------------------------------------------------------------------
# balance_vs_txn (Should) — c_012
# ---------------------------------------------------------------------------

@router.get("/balance_vs_txn", response_model=ToolResponse)
def get_balances_vs_transactions(
    customer_id: Annotated[str, Path(min_length=1)],
    start: Annotated[date, Query()],
    end: Annotated[date, Query()],
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[object, Depends(require_service_access)],
) -> ToolResponse:
    """Balance delta (closing_end − closing_start_minus_1) compared with the
    sum of the window's transactions. c_012's story is that they do not match."""
    if start > end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start is after end")

    _ensure_customer(session, customer_id)
    accounts = _account_ids_for(session, customer_id)

    opening_rows = _closing_on_or_before(session, accounts, start)
    closing_rows = _closing_on_or_before(session, accounts, end)

    opening = sum(v[1] for v in opening_rows.values())
    closing = sum(v[1] for v in closing_rows.values())
    balance_delta = closing - opening

    txn_sum = session.scalar(
        select(func.coalesce(func.sum(BankTransaction.amount_cents), 0))
        .join(LinkedAccount, BankTransaction.account_id == LinkedAccount.id)
        .where(LinkedAccount.customer_id == customer_id)
        .where(BankTransaction.date >= start)
        .where(BankTransaction.date <= end)
    )
    discrepancy = balance_delta - int(txn_sum)
    reconciled = abs(discrepancy) <= _RECONCILE_TOLERANCE_CENTS

    data = {
        "balance_delta_cents": balance_delta,
        "txn_sum_cents": int(txn_sum),
        "discrepancy_cents": discrepancy,
        "reconciled": reconciled,
    }
    evidence = _balances_as_evidence(opening_rows) + _balances_as_evidence(closing_rows)
    evidence.append(
        Evidence(
            id=aggregate_evidence_id(
                "balance_vs_txn", customer_id, start.isoformat(), end.isoformat()
            ),
            kind=BALANCE_VS_TXN_RECONCILIATION,
            data=data,
        )
    )
    return ToolResponse(data=data, evidence=evidence)


# ---------------------------------------------------------------------------
# prior_year_compare (Should) — c_007
# ---------------------------------------------------------------------------

@router.get("/prior_year_compare", response_model=ToolResponse)
def compare_to_prior_year(
    customer_id: Annotated[str, Path(min_length=1)],
    start: Annotated[date, Query(description="current window start (inclusive)")],
    end: Annotated[date, Query(description="current window end (inclusive)")],
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[object, Depends(require_service_access)],
) -> ToolResponse:
    """Current-window inflows/outflows alongside the same ISO weeks of the
    prior year, with a `similar` flag when both sides are within 20%."""
    if start > end:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "start is after end")

    _ensure_customer(session, customer_id)

    current_rows = session.execute(
        select(BankTransaction.amount_cents)
        .join(LinkedAccount, BankTransaction.account_id == LinkedAccount.id)
        .where(LinkedAccount.customer_id == customer_id)
        .where(BankTransaction.date >= start)
        .where(BankTransaction.date <= end)
    ).all()
    current_inflow = sum(int(a) for (a,) in current_rows if int(a) > 0)
    current_outflow = sum(int(a) for (a,) in current_rows if int(a) < 0)

    weeks_touched = _iso_weeks_in_range(start, end)
    prior_year = start.year - 1
    py = session.execute(
        select(PriorYearWeekly)
        .where(PriorYearWeekly.customer_id == customer_id)
        .where(PriorYearWeekly.year == prior_year)
        .where(PriorYearWeekly.week.in_(weeks_touched))
    ).all()
    prior_inflow = sum(int(row[0].inflow_cents) for row in py)
    # prior_year_weekly stores outflow as a positive number; sign normalisation keeps
    # the comparison apples-to-apples with current_outflow (which is negative).
    prior_outflow = -sum(int(row[0].outflow_cents) for row in py)

    def _within_pct(a: int, b: int, pct: float) -> bool:
        if a == 0 and b == 0:
            return True
        denom = max(abs(a), abs(b))
        return denom > 0 and abs(a - b) / denom <= pct

    similar = _within_pct(current_inflow, prior_inflow, 0.2) and _within_pct(
        current_outflow, prior_outflow, 0.2
    )

    data = {
        "current_inflow_cents": current_inflow,
        "current_outflow_cents": current_outflow,
        "prior_inflow_cents": prior_inflow,
        "prior_outflow_cents": prior_outflow,
        "similar": similar,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
    }
    evidence = [
        Evidence(
            id=aggregate_evidence_id(
                "prior_year_compare",
                customer_id,
                start.isoformat(),
                end.isoformat(),
            ),
            kind=PRIOR_YEAR_COMPARISON,
            data=data,
        )
    ]
    return ToolResponse(data=data, evidence=evidence)


def _iso_weeks_in_range(start: date, end: date) -> list[int]:
    weeks: set[int] = set()
    cur = start
    while cur <= end:
        weeks.add(cur.isocalendar().week)
        cur += timedelta(days=1)
    return sorted(weeks)
