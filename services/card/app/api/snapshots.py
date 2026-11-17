"""GET /snapshots: a few raw numbers per customer, with no judgment attached (KAN-27).

Every case starts from these. They are the card half of a signal's snapshot
(contracts.signal.CardSnapshot: balance_cents and the last three repayments), plus the
limit, the share of it in use and the account that pays the card.

Nothing here says whether a number is good or bad. Whether a customer needs a review is the
detection job's call; why its numbers moved is the agents'.
"""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.api.auth import require_snapshot_reader
from app.db import get_session
from app.ledger import Book, customer_ids, load_book, repayment_status, service_as_of

router = APIRouter()

LAST_REPAYMENTS = 3


class SnapshotRepayment(BaseModel):
    """The same shape as contracts.signal.Repayment."""

    model_config = ConfigDict(extra="forbid")

    due: date
    amount_cents: int
    status: Literal["paid", "late", "failed"]


class CustomerSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str
    credit_limit_cents: int
    balance_cents: int
    # balance / limit in basis points (5800 is 58%). None when the limit is zero.
    used_share_bp: int | None
    card_status: Literal["active", "paused"]
    funding_account_id: str
    # The last repayments due on or before as_of whose outcome is known, oldest first.
    repayments: list[SnapshotRepayment]


class Snapshots(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: date
    customers: list[CustomerSnapshot]


def snapshot(book: Book, as_of: date) -> CustomerSnapshot:
    limit = book.limit_on(as_of)
    balance = book.balance_on(as_of)
    known = [(r, s) for r in book.due_by(as_of) if (s := repayment_status(r, as_of)) is not None]
    return CustomerSnapshot(
        customer_id=book.customer_id,
        credit_limit_cents=limit,
        balance_cents=balance,
        used_share_bp=round(10_000 * balance / limit) if limit else None,
        card_status=book.profile.card_status,
        funding_account_id=book.profile.funding_account_id,
        repayments=[
            SnapshotRepayment(due=r.due_date, amount_cents=r.amount_cents, status=s)
            for r, s in known[-LAST_REPAYMENTS:]
        ],
    )


@router.get("/snapshots", response_model=Snapshots)
def get_snapshots(
    session: Annotated[Session, Depends(get_session)],
    _: Annotated[object, Depends(require_snapshot_reader)],
    customer_id: Annotated[str | None, Query(pattern=r"^c_[0-9]{3}$", description="one customer; all when left out")] = None,
    as_of: Annotated[date | None, Query(description="read the numbers at the end of this day; default the service's as-of date")] = None,
) -> Snapshots:
    latest = service_as_of()
    day = as_of or latest
    if day > latest:
        raise HTTPException(
            422,
            {"error": "invalid_request", "message": f"as_of is after {latest.isoformat()}, the last day the data covers"},
        )
    ids = [customer_id] if customer_id else customer_ids(session)
    books = [book for cid in ids if (book := load_book(session, cid)) is not None]
    if customer_id and not books:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, {"error": "not_found", "message": f"Unknown customer {customer_id}"}
        )
    return Snapshots(as_of=day, customers=[snapshot(book, day) for book in books])
