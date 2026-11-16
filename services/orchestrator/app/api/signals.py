import logging
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_session
from app.db.models import OPEN_STATUSES, Case, CaseStatus, SignalRow
from contracts import Signal

log = logging.getLogger(__name__)
router = APIRouter()

# opened: a new case. joined: the customer already had an open case.
# duplicate: this signal_id was received before.
Result = Literal["opened", "joined", "duplicate"]


class SignalAccepted(BaseModel):
    case_id: str
    status: CaseStatus
    result: Result


def _stored(session: Session, signal_id: str) -> SignalRow | None:
    return session.get(SignalRow, signal_id)


def _open_case(session: Session, customer_id: str) -> Case | None:
    return session.scalar(
        select(Case).where(Case.customer_id == customer_id, Case.status.in_(OPEN_STATUSES))
    )


def _place(session: Session, signal: Signal) -> tuple[Case, Result]:
    """Store the signal and return its case.

    Raises IntegrityError when another request stored this signal, or opened
    this customer's case, between the lookups and the commit.
    """
    payload = signal.model_dump(mode="json")
    seen = _stored(session, signal.signal_id)
    if seen is not None:
        if seen.payload != payload:
            # The same id with other content is a sender bug. Answering with the
            # stored case could hand one customer's case to another.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"signal_id {signal.signal_id} was already received with different content",
            )
        return session.get(Case, seen.case_id), "duplicate"

    case = _open_case(session, signal.customer_id)
    result: Result = "joined"
    if case is None:
        case = Case(
            case_id=f"case_{uuid4().hex[:8]}",
            customer_id=signal.customer_id,
            status=CaseStatus.QUEUED,
        )
        session.add(case)
        # The case row goes in first: signals.case_id points at it.
        session.flush()
        result = "opened"
    session.add(
        SignalRow(
            signal_id=signal.signal_id,
            case_id=case.case_id,
            customer_id=signal.customer_id,
            payload=payload,
        )
    )
    session.commit()
    return case, result


@router.post("/signals", response_model=SignalAccepted, status_code=status.HTTP_201_CREATED)
def receive_signal(
    signal: Signal, response: Response, session: Session = Depends(get_session)
) -> SignalAccepted:
    """Open a case for a signal, or attach the signal to the customer's open case."""
    attempts_left = 3
    while True:
        try:
            case, result = _place(session, signal)
            break
        except IntegrityError:
            # Lost a race with another request. Look again: the signal or the case is there now.
            session.rollback()
            attempts_left -= 1
            if attempts_left == 0:
                raise

    if result != "opened":
        response.status_code = status.HTTP_200_OK
    log.info(
        "signal %s for %s: %s, case %s",
        signal.signal_id,
        signal.customer_id,
        result,
        case.case_id,
    )
    return SignalAccepted(case_id=case.case_id, status=CaseStatus(case.status), result=result)
