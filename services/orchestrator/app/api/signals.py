import logging
from uuid import uuid4

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_session
from app.db.models import Case, CaseStatus, SignalRow
from contracts import Signal

log = logging.getLogger(__name__)
router = APIRouter()


class SignalAccepted(BaseModel):
    case_id: str
    status: CaseStatus
    duplicate: bool


def _case_for(session: Session, signal_id: str) -> Case | None:
    return session.scalar(select(Case).where(Case.signal_id == signal_id))


@router.post("/signals", response_model=SignalAccepted, status_code=status.HTTP_201_CREATED)
def receive_signal(
    signal: Signal, response: Response, session: Session = Depends(get_session)
) -> SignalAccepted:
    """Open a case for a new signal. A signal_id seen before returns its case and opens nothing."""
    case = _case_for(session, signal.signal_id)
    duplicate = case is not None
    if case is None:
        case = Case(
            case_id=f"case_{uuid4().hex[:8]}",
            signal_id=signal.signal_id,
            customer_id=signal.customer_id,
            status=CaseStatus.QUEUED,
        )
        try:
            session.add(
                SignalRow(
                    signal_id=signal.signal_id,
                    customer_id=signal.customer_id,
                    payload=signal.model_dump(mode="json"),
                )
            )
            # The signal row goes in first: cases.signal_id points at it.
            session.flush()
            session.add(case)
            session.commit()
        except IntegrityError:
            # Another request stored the same signal between the check and the commit.
            session.rollback()
            case = _case_for(session, signal.signal_id)
            if case is None:
                raise
            duplicate = True

    if duplicate:
        response.status_code = status.HTTP_200_OK
    log.info(
        "signal %s for %s: %s case %s",
        signal.signal_id,
        signal.customer_id,
        "duplicate of" if duplicate else "opened",
        case.case_id,
    )
    return SignalAccepted(case_id=case.case_id, status=CaseStatus(case.status), duplicate=duplicate)
