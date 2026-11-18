"""Read-only analysis tools on the Bank Service.

The Cash Flow agent in the Orchestrator Service calls them over HTTP with a short-lived service
token (see auth.require_service_access). Every response has the same envelope, {data, evidence}:
evidence is the records the answer rests on, in the vocabulary the Orchestrator's rules read
(app/api/evidence.py); data says what was asked, which ids answer it, and what could not be
worked out. The figures are in the evidence only, so there is one place to cite them from.

The interface is described in services/bank/README.md, "Tool interface for the Cash Flow agent".

A window is two dates: the cash at the end of `start` against the cash at the end of `end`, and
the transactions dated after `start` up to `end`. Left out, it is the review window, the as-of
date less the window's days to the as-of date. `start` equal to `end` is that one day: the window from
the day before to that day.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.orm import Session

from app.api.auth import require_service_access
from app.api.schemas import ToolResponse
from app.db import get_session
from app.db.models import Customer
from app.ledger import Ledger, Record

router = APIRouter(prefix="/customers/{customer_id}/tools")

CustomerId = Annotated[str, Path(min_length=1)]
Db = Annotated[Session, Depends(get_session)]
Caller = Annotated[object, Depends(require_service_access)]
Start = Annotated[date | None, Query(description="window start: the cash at the end of this day")]
End = Annotated[date | None, Query(description="window end, inclusive")]

_STALE = "Linked cash moved while {} did not report, so no share of the fall or reconciliation is worked out."
_NO_FALL = "Linked cash did not fall in the window, so there is no fall to share out or reconcile."
_NO_BALANCE = "No balance was reported on {}."


def _ledger(session: Session, customer_id: str) -> Ledger:
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"error": "not_found", "message": f"Unknown customer {customer_id}"})
    return Ledger(session, customer)


def _window(ledger: Ledger, start: date | None, end: date | None) -> tuple[date, date]:
    start, end = start or ledger.window[0], end or ledger.window[1]
    if start > end:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"error": "invalid_window", "message": "start must not be after end"},
        )
    if start == end:
        # One day: the cash at the end of the day before against the end of that day, and that day's
        # transactions. The same window as start = the day before, so the same records and ids.
        start -= timedelta(days=1)
    return start, end


def _why_no_figures(ledger: Ledger, start: date, end: date) -> list[str]:
    """Why the window has no payment summary or reconciliation."""
    if missing := [day for day in (start, end) if ledger.cash_on(day) is None]:
        return [_NO_BALANCE.format(day.isoformat()) for day in missing]
    if stale := ledger.stale():
        return [_STALE.format(", ".join(account.id for account in stale))]
    return [_NO_FALL]


def _respond(data: dict, records: list[Record | None]) -> ToolResponse:
    return ToolResponse(data=data, evidence=[record.evidence() for record in records if record is not None])


@router.get("/transactions", response_model=ToolResponse)
def list_transactions(
    customer_id: CustomerId,
    session: Db,
    _: Caller,
    start: Start = None,
    end: End = None,
    category: Annotated[str | None, Query(description="list only this category")] = None,
    min_abs_amount_cents: Annotated[int | None, Query(ge=0, description="list only movements this large")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ToolResponse:
    """The cash at both ends of the window, its itemised transactions, and what the payments add up to.

    The filters choose which transactions are listed. The summary is always the whole window's.
    """
    ledger = _ledger(session, customer_id)
    start, end = _window(ledger, start, end)
    listed = [
        txn
        for txn in ledger.listed(start, end)
        if (category is None or txn.category == category)
        and (min_abs_amount_cents is None or abs(txn.amount_cents) >= min_abs_amount_cents)
    ]
    balance, summary = ledger.balance(start, end), ledger.payment_summary(start, end)
    data = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "transaction_ids": [txn.ref for txn in listed[:limit]],
        "truncated": len(listed) > limit,
        "not_available": [] if summary else _why_no_figures(ledger, start, end),
    }
    return _respond(data, [balance, *(ledger.txn(txn) for txn in listed[:limit]), summary])


@router.get("/cash_summary", response_model=ToolResponse)
def get_cash_summary(customer_id: CustomerId, session: Db, _: Caller, start: Start = None, end: End = None) -> ToolResponse:
    """The cash at both ends of the window and what the window's payments add up to, without the list."""
    ledger = _ledger(session, customer_id)
    start, end = _window(ledger, start, end)
    summary = ledger.payment_summary(start, end)
    data = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "not_available": [] if summary else _why_no_figures(ledger, start, end),
    }
    return _respond(data, [ledger.balance(start, end), summary])


@router.get("/income_trend", response_model=ToolResponse)
def get_income_trend(customer_id: CustomerId, session: Db, _: Caller) -> ToolResponse:
    """Weekly inflows over the trailing weeks that end on the as-of date, with the trend's label."""
    ledger = _ledger(session, customer_id)
    record = ledger.inflows()
    return _respond({"weeks": ledger.weeks, "end": ledger.as_of.isoformat()}, [record])


@router.get("/payee_history", response_model=ToolResponse)
def get_payee_history(
    customer_id: CustomerId,
    session: Db,
    _: Caller,
    record_id: Annotated[str, Query(min_length=1, description="the id of one transaction, as txn_<customer>_<nn>")],
) -> ToolResponse:
    """How often the payee of one payment was paid before, and how this payment compares."""
    ledger = _ledger(session, customer_id)
    txn = ledger.find(record_id)
    if txn is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            {"error": "not_found", "message": f"{customer_id} has no transaction {record_id}"},
        )
    history = ledger.payee_history(txn)
    data = {"record_id": record_id, "paid_before": history is not None}
    if txn.amount_cents >= 0:
        data["not_available"] = ["It is money coming in, not a payment."]
    elif history is None:
        data["not_available"] = ["No earlier payment to this payee is on record."]
    return _respond(data, [history, ledger.txn(txn)])


@router.get("/account_reporting", response_model=ToolResponse)
def check_account_reporting(
    customer_id: CustomerId,
    session: Db,
    _: Caller,
    account_id: Annotated[str | None, Query(min_length=1, description="one account, linked or not")] = None,
) -> ToolResponse:
    """Each linked account: when it last reported and how stale that is.

    Asked about an account that is not linked: the linked accounts, and every payment of the
    review window that went to that account.
    """
    ledger = _ledger(session, customer_id)
    linked = {account.id: account for account in ledger.accounts}
    if account_id is None:
        return _respond({"account_ids": list(linked)}, [ledger.account(a) for a in linked.values()])
    if account_id in linked:
        return _respond({"account_id": account_id, "linked": True}, [ledger.account(linked[account_id])])
    sent = [txn for txn in ledger.listed(*ledger.window) if txn.to_account == account_id and txn.amount_cents < 0]
    data = {"account_id": account_id, "linked": False, "transaction_ids": [txn.ref for txn in sent]}
    return _respond(data, [*(ledger.account(a) for a in linked.values()), *(ledger.txn(txn) for txn in sent)])


@router.get("/balance_vs_txn", response_model=ToolResponse)
def get_balances_vs_transactions(
    customer_id: CustomerId, session: Db, _: Caller, start: Start = None, end: End = None
) -> ToolResponse:
    """Whether the balances moved by exactly what the window's transactions add up to."""
    ledger = _ledger(session, customer_id)
    start, end = _window(ledger, start, end)
    books = ledger.reconciliation(start, end)
    data = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "not_available": [] if books else _why_no_figures(ledger, start, end),
    }
    return _respond(data, [books, ledger.balance(start, end)] if books else [])


@router.get("/prior_year_compare", response_model=ToolResponse)
def compare_to_prior_year(customer_id: CustomerId, session: Db, _: Caller) -> ToolResponse:
    """The same weeks one year earlier, set against every payment of the review window."""
    ledger = _ledger(session, customer_id)
    record = ledger.prior_year()
    data = {"not_available": [] if record else ["Figures for the same weeks last year are not on record."]}
    return _respond(data, [record])
