"""Read-only analysis tools on the Card Service (KAN-25).

The Credit Behavior Agent in the Orchestrator Service calls these over HTTP with a short-lived
service token that names one customer (auth.require_service_access). Every response has the
same envelope as the Bank tools: {data, evidence}. `data` is the answer, worked out in code;
`evidence` lists every record behind it, under ids that keep their content (app/api/evidence.py).

    GET /customers/{customer_id}/tools/<name>?...

| Tool             | Orchestrator topic | What it answers |
|------------------|--------------------|-----------------|
| repayments       | repayments         | the last repayments, each paid, late or failed, with counts |
| paying_account   | paying_account     | whether the account paying the card changed, from what to what |
| utilization      | card_use           | balance over the limit on a day, against the same day in earlier months |
| spend_by_cycle   | card_use           | spend per statement cycle, and this cycle against the customer's usual |
| transactions     | (drill-down)       | the card transactions that match a filter |
| spend_breakdown  | (drill-down)       | spend by category, merchant, card or MCC, against the period before |

"Unusual" always means unlike this customer's own past. Where there is too little past to
compare with, a tool says too_short_to_tell, never steady or normal. The thresholds below are
the Card owner's starting values.
"""

from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from datetime import date, timedelta
from statistics import median
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api import evidence as ev
from app.api.auth import require_service_access
from app.db import get_session
from app.ledger import FAILED, LATE, PAID, Book, load_book, repayment_status, service_as_of

router = APIRouter(prefix="/customers/{customer_id}/tools")

# utilization: compare a day with the same day of this many earlier months.
TREND_MONTHS = 3
RISING_BP = 1500  # 15 points of the limit above the earlier months' mean
SPIKING_BP = 2500  # 25 points above it ...
SPIKING_LEVEL_BP = 9000  # ... and now at 90% of the limit or more
# spend_by_cycle: a cycle is unusual this far from the median of the closed cycles before it.
SPEND_UP_BP = 5000  # +50%
SPEND_DOWN_BP = -4000  # -40%
MIN_CYCLES = 3  # fewer closed cycles than this is too short to tell
MAX_ROWS = 50
MAX_LOOKBACK_DAYS = 400

Customer = Annotated[str, Path(pattern=r"^c_[0-9]{3}$")]
Auth = Annotated[object, Depends(require_service_access)]
Db = Annotated[Session, Depends(get_session)]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    data: dict[str, Any]


class ToolResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: dict[str, Any]
    evidence: list[Evidence]


def _invalid(message: str) -> HTTPException:
    return HTTPException(422, {"error": "invalid_request", "message": message})


def _book(session: Session, customer_id: str) -> Book:
    book = load_book(session, customer_id)
    if book is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"error": "not_found", "message": f"Unknown customer {customer_id}"})
    return book


def _window(start: date, end: date) -> tuple[date, date]:
    latest = service_as_of()
    if start > end:
        raise _invalid("start is after end")
    if end > latest:
        raise _invalid(f"end is after {latest.isoformat()}, the last day the data covers")
    if (latest - start).days > MAX_LOOKBACK_DAYS:
        raise _invalid(f"start is more than {MAX_LOOKBACK_DAYS} days before {latest.isoformat()}")
    return start, end


def _respond(data: dict[str, Any], items: list[dict[str, Any]]) -> ToolResponse:
    unique = list({i["id"]: i for i in items}.values())
    return ToolResponse(data=ev.plain(data), evidence=unique)


def _bp(part: int, whole: int) -> int | None:
    return round(10_000 * part / whole) if whole else None


# ---------------------------------------------------------------------------
# repayments
# ---------------------------------------------------------------------------


@router.get("/repayments", response_model=ToolResponse)
def repayments(
    customer_id: Customer, session: Db, _: Auth,
    count: Annotated[int, Query(ge=1, le=13, description="how many of the latest repayments")] = 6,
) -> ToolResponse:
    """The latest repayments that are due, oldest first, each paid, late or failed; and their counts."""
    book, as_of = _book(session, customer_id), service_as_of()
    due = [r for r in book.due_by(as_of) if repayment_status(r, as_of) is not None][-count:]
    items = [ev.repayment(book, r, as_of) for r in due]
    counts = Counter(i["data"]["status"] for i in items)
    summary = {"n": len(items), "paid": counts[PAID], "late": counts[LATE], "failed": counts[FAILED],
               "first_due": due[0].due_date if due else None, "last_due": due[-1].due_date if due else None}
    agg = ev.item(ev.aggregate_id("repayments", customer_id, count), ev.REPAYMENT_SUMMARY, summary)
    data = {"repayments": [{"id": i["id"], **i["data"]} for i in items], "counts": summary, "summary_id": agg["id"]}
    return _respond(data, [*items, agg])


# ---------------------------------------------------------------------------
# paying_account
# ---------------------------------------------------------------------------


@router.get("/paying_account", response_model=ToolResponse)
def paying_account(
    customer_id: Customer, session: Db, _: Auth,
    count: Annotated[int, Query(ge=2, le=13, description="how many of the latest repayments to compare")] = 3,
) -> ToolResponse:
    """Whether the bank account the card is paid from changed across the latest repayments."""
    book, as_of = _book(session, customer_id), service_as_of()
    due = [r for r in book.due_by(as_of) if repayment_status(r, as_of) is not None][-count:]
    reps = [ev.repayment(book, r, as_of) for r in due]
    accounts = [r.funding_account_id for r in due]
    current = book.profile.funding_account_id
    first = accounts[0] if accounts else current
    changed = any(a != first for a in accounts) or first != current
    change = {
        "changed": changed,
        "from": first,
        "to": current,
        "changed_on": book.profile.funding_account_since if changed else None,
        "repayments_compared": len(due),
        "repayment_ids": [r.repayment_id for r in due],
    }
    agg = ev.item(ev.aggregate_id("paying_account", customer_id, count), ev.FUNDING_ACCOUNT_CHANGE, change)
    data = {**change, "change_id": agg["id"],
            "accounts": [{"repayment_id": r.repayment_id, "due": r.due_date, "funding_account_id": r.funding_account_id}
                         for r in due]}
    return _respond(data, [agg, *reps])


# ---------------------------------------------------------------------------
# utilization
# ---------------------------------------------------------------------------


def _months_back(day: date, months: int) -> date:
    y, m = divmod(day.month - 1 - months, 12)
    year, month = day.year + y, m + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _months_back_all(day: date) -> list[date]:
    """The same day of each of the TREND_MONTHS months before."""
    return [_months_back(day, k) for k in range(1, TREND_MONTHS + 1)]


def _balance_record(book: Book, day: date) -> dict[str, Any]:
    """The card_balance record for one day. Its trend uses a fixed method, so the record is the
    same whichever call returns it."""
    limit, balance = book.limit_on(day), book.balance_on(day)
    share = _bp(balance, limit)
    earlier = _months_back_all(day)
    first = book.first_day()
    if share is None or first is None or earlier[-1] < first or earlier[-1] < book.profile.onboarded_on:
        trend, usual = "too_short_to_tell", None
    else:
        usual = round(sum(_bp(book.balance_on(d), book.limit_on(d)) or 0 for d in earlier) / len(earlier))
        delta = share - usual
        if delta >= SPIKING_BP and share >= SPIKING_LEVEL_BP:
            trend = "spiking"
        elif delta >= RISING_BP:
            trend = "rising"
        elif delta <= -RISING_BP:
            trend = "falling"
        else:
            trend = "steady"
    return ev.item(ev.balance_id(book.customer_id, day), ev.CARD_BALANCE, {
        "date": day, "credit_limit_cents": limit, "balance_cents": balance, "used_share_bp": share,
        "utilization_trend": trend, "usual_share_bp": usual, "compared_with": earlier,
    })


@router.get("/utilization", response_model=ToolResponse)
def utilization(
    customer_id: Customer, session: Db, _: Auth,
    on: Annotated[date | None, Query(description="the day to read; default the service's as-of date")] = None,
) -> ToolResponse:
    """Balance over the limit at the end of a day, against the same day of the three months before.

    utilization_trend: spiking (25+ points above the earlier months and at 90%+ of the limit),
    rising (15+ points above), falling (15+ below), steady, or too_short_to_tell when the card's
    history does not reach three months back.
    """
    book = _book(session, customer_id)
    day = on or service_as_of()
    _window(day, day)
    now = _balance_record(book, day)
    earlier = [_balance_record(book, d) for d in _months_back_all(day)]
    data = {**now["data"], "balance_id": now["id"],
            "earlier": [{"id": e["id"], "date": e["data"]["date"], "used_share_bp": e["data"].get("used_share_bp")}
                        for e in earlier]}
    return _respond(data, [now, *earlier])


# ---------------------------------------------------------------------------
# spend_by_cycle
# ---------------------------------------------------------------------------


@router.get("/spend_by_cycle", response_model=ToolResponse)
def spend_by_cycle(
    customer_id: Customer, session: Db, _: Auth,
    cycles: Annotated[int, Query(ge=3, le=13, description="how many closed statement cycles to show")] = 6,
) -> ToolResponse:
    """Spend per statement cycle, and the open cycle against the median of the closed ones.

    The open cycle is projected to a full cycle when it has not ended. change_vs_usual_bp is that
    against the median; unusual is "up" at +50% or more, "down" at -40% or less. With fewer than
    three closed cycles the history is too_short_to_tell.
    """
    book, as_of = _book(session, customer_id), service_as_of()
    closed = [r for r in book.repayments if r.statement_period_end < as_of][-cycles:]
    rows = [{"start": r.statement_period_start, "end": r.statement_period_end,
             "spend_cents": book.spend_between(r.statement_period_start, r.statement_period_end)} for r in closed]
    open_start = (closed[-1].statement_period_end + timedelta(days=1)) if closed else (book.first_day() or as_of)
    open_end = _months_back(open_start, -1) - timedelta(days=1)
    elapsed = (min(as_of, open_end) - open_start).days + 1
    length = (open_end - open_start).days + 1
    spent = book.spend_between(open_start, as_of)
    projected = round(spent * length / elapsed) if elapsed > 0 else 0
    current = {"start": open_start, "end": open_end, "spend_cents": spent, "complete": as_of >= open_end,
               "projected_cents": projected}
    series: dict[str, Any] = {"cycles": rows, "current_cycle": current}
    if len(rows) < MIN_CYCLES:
        series["history"] = "too_short_to_tell"
    else:
        usual = round(median(r["spend_cents"] for r in rows))
        change = _bp(projected - usual, usual)
        series |= {
            "history": "enough",
            "monthly_card_spend_cents": usual,
            "change_vs_usual_bp": change,
            "unusual": "up" if change is not None and change >= SPEND_UP_BP
            else "down" if change is not None and change <= SPEND_DOWN_BP else "no",
        }
    agg = ev.item(ev.aggregate_id("spend_by_cycle", customer_id, cycles), ev.CARD_SPEND_SERIES, series)
    return _respond({**series, "series_id": agg["id"]}, [agg])


# ---------------------------------------------------------------------------
# transactions
# ---------------------------------------------------------------------------


@router.get("/transactions", response_model=ToolResponse)
def transactions(
    customer_id: Customer, session: Db, _: Auth,
    start: Annotated[date, Query(description="first day, inclusive")],
    end: Annotated[date, Query(description="last day, inclusive")],
    merchant: Annotated[str | None, Query(max_length=60, description="part of the merchant name or descriptor")] = None,
    category: Annotated[str | None, Query(max_length=60, description="spend category, exactly")] = None,
    mcc: Annotated[str | None, Query(pattern=r"^[0-9]{4}$")] = None,
    card_id: Annotated[str | None, Query(max_length=48)] = None,
    state: Annotated[Literal["approved", "declined", "any"], Query()] = "any",
    min_amount_cents: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_ROWS)] = 20,
) -> ToolResponse:
    """Card transactions in a window that match every filter given, largest first, with totals."""
    book = _book(session, customer_id)
    start, end = _window(start, end)
    rows = book.txns_between(start, end)
    if merchant:
        q = merchant.lower()
        rows = [t for t in rows if q in t.merchant_name.lower() or q in t.merchant_descriptor.lower()]
    if category:
        rows = [t for t in rows if t.spend_category.lower() == category.lower()]
    if mcc:
        rows = [t for t in rows if t.mcc == mcc]
    if card_id:
        rows = [t for t in rows if t.card_id == card_id]
    if state != "any":
        rows = [t for t in rows if t.approved == (state == "approved")]
    if min_amount_cents is not None:
        rows = [t for t in rows if abs(t.amount_cents) >= min_amount_cents]
    shown = sorted(rows, key=lambda t: (-abs(t.amount_cents), t.txn_id))[:limit]
    totals = {
        "start": start, "end": end, "matches": len(rows),
        "approved_total_cents": sum(t.amount_cents for t in rows if t.approved),
        "declined": sum(not t.approved for t in rows),
        "declined_by_reason": dict(Counter(t.decline_reason for t in rows if not t.approved)),
        "shown": len(shown), "truncated": len(rows) > len(shown),
    }
    filters = (start, end, merchant, category, mcc, card_id, state, min_amount_cents)
    agg = ev.item(ev.aggregate_id("transactions", customer_id, *filters), ev.TRANSACTION_SEARCH, totals)
    items = [ev.transaction(t) for t in shown]
    data = {**totals, "summary_id": agg["id"], "transactions": [{"id": i["id"], **i["data"]} for i in items]}
    return _respond(data, [agg, *items])


# ---------------------------------------------------------------------------
# spend_breakdown
# ---------------------------------------------------------------------------


@router.get("/spend_breakdown", response_model=ToolResponse)
def spend_breakdown(
    customer_id: Customer, session: Db, _: Auth,
    start: Annotated[date, Query()],
    end: Annotated[date, Query()],
    by: Annotated[Literal["category", "merchant", "card", "mcc"], Query()] = "category",
) -> ToolResponse:
    """Approved spend in a window grouped one way, against the period of the same length just before.

    Each group has its share of the window's spend and its spend in the period before, so a
    group that is new (nothing before) stands out. The eight largest groups are shown.
    """
    book = _book(session, customer_id)
    start, end = _window(start, end)
    days = (end - start).days + 1
    prior_start, prior_end = start - timedelta(days=days), start - timedelta(days=1)

    def key(t) -> str:
        return {"category": t.spend_category, "merchant": t.merchant_name, "card": t.card_id, "mcc": t.mcc}[by]

    now: dict[str, int] = defaultdict(int)
    before: dict[str, int] = defaultdict(int)
    for t in book.txns_between(start, end):
        if t.approved:
            now[key(t)] += t.amount_cents
    for t in book.txns_between(prior_start, prior_end):
        if t.approved:
            before[key(t)] += t.amount_cents
    total, prior_total = sum(now.values()), sum(before.values())
    groups = sorted(set(now) | set(before), key=lambda k: (-now.get(k, 0), k))[:8]
    rows = [{by: k, "window_cents": now.get(k, 0), "share_bp": _bp(now.get(k, 0), total),
             "prior_cents": before.get(k, 0), "new": before.get(k, 0) == 0 and now.get(k, 0) > 0} for k in groups]
    breakdown = {"by": by, "start": start, "end": end, "prior_start": prior_start, "prior_end": prior_end,
                 "window_total_cents": total, "prior_total_cents": prior_total,
                 "change_bp": _bp(total - prior_total, prior_total), "groups": rows}
    agg = ev.item(ev.aggregate_id("spend_breakdown", customer_id, start, end, by), ev.SPEND_BREAKDOWN, breakdown)
    return _respond({**breakdown, "breakdown_id": agg["id"]}, [agg])
