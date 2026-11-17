"""Runs cases on the graph and keeps the cases table in step with it.

One process is assumed. Two would both pick up an unfinished case after a restart.
"""

import logging
from typing import Literal

from langgraph.types import StateSnapshot
from sqlalchemy import select, update

from app import history
from app.db import SessionLocal
from app.db.models import OPEN_STATUSES, Case, CaseStatus, Review, SignalRow
from app.graph import facts

log = logging.getLogger(__name__)

# The next move belongs to the analyst, to the manager, or to the approver again:
# "retry" means the action was approved but Card has not carried it out.
WaitingFor = Literal["analyst", "manager", "retry"]
_WAITING_AT: dict[tuple, WaitingFor] = {
    ("analyst_approval",): "analyst",
    ("manager_approval",): "manager",
    ("act",): "retry",
}


def thread(case_id: str, user_id: str | None = None) -> dict:
    """The graph config for a case: one thread per case, and one tracing session.

    A case is several runs (the investigation, then each decision). LangFuse shows
    them together under the session, with the person who decided on their run.
    """
    metadata = {"langfuse_session_id": case_id}
    if user_id:
        metadata["langfuse_user_id"] = user_id
    return {"configurable": {"thread_id": case_id}, "metadata": metadata}


def waiting_for(snapshot: StateSnapshot) -> WaitingFor | None:
    return _WAITING_AT.get(snapshot.next)


def unfinished() -> list[str]:
    """Every open case, oldest first.

    A restart may have left one queued or half investigated, or with a status in
    the table that is older than what the graph has saved.
    """
    with SessionLocal() as session:
        return list(
            session.scalars(select(Case.case_id).where(Case.status.in_(OPEN_STATUSES)).order_by(Case.created_at))
        )


def run_case(graph, case_id: str) -> None:
    """Take a case as far as it goes without a person. Safe to call on a case at any point."""
    config = thread(case_id)
    try:
        snapshot = graph.get_state(config)
        if not snapshot.values:
            _set_status(case_id, CaseStatus.INVESTIGATING)
            graph.invoke({"case_id": case_id, "signal": _opening_signal(case_id)}, config)
        elif snapshot.next and waiting_for(snapshot) is None:
            # A restart stopped it part-way. Go on from the last saved step.
            _set_status(case_id, CaseStatus.INVESTIGATING)
            graph.invoke(None, config)
        # Otherwise the graph is where it should be and only the table may be behind.
        status = _catch_up(graph, case_id)
    except Exception:
        log.exception("case %s failed", case_id)
        status = CaseStatus.FAILED
        _set_status(case_id, status)
    log.info("case %s is %s", case_id, status)


def _opening_signal(case_id: str) -> dict:
    with SessionLocal() as session:
        return session.scalars(
            select(SignalRow.payload)
            .where(SignalRow.case_id == case_id)
            .order_by(SignalRow.received_at, SignalRow.signal_id)
        ).first()


def _catch_up(graph, case_id: str) -> str:
    """Write the status the graph has saved now, which may be newer than what a run returned.

    A decision can arrive the moment a run pauses. The row lock makes this write
    wait for it, and reading the graph afterwards keeps it from being overwritten.
    A case that closed while the table was behind gets its review in the same transaction.
    """
    with SessionLocal() as session:
        case = session.scalar(select(Case).where(Case.case_id == case_id).with_for_update())
        state = graph.get_state(thread(case_id)).values
        case.status = state["status"]
        history.record(session, case, state, ledger=facts.ledger)
        session.commit()
        return case.status


def keep_missing_reviews(graph) -> int:
    """Write the review of every closed case that has none. Returns how many were written.

    history.record never raises, so a review whose insert failed is not tried again
    on its own, and a case closed before reviews were kept never got one. A missing
    review would read as "no earlier review" to the customer's next case. A case
    whose report was never checked gets none here either.
    """
    with SessionLocal() as session:
        missing = list(
            session.scalars(
                select(Case.case_id)
                .outerjoin(Review, Review.case_id == Case.case_id)
                .where(Case.status.in_(history.CLOSED), Review.case_id.is_(None))
                .order_by(Case.created_at, Case.case_id)
            )
        )
    written = 0
    for case_id in missing:
        try:
            with SessionLocal() as session:
                case = session.scalar(select(Case).where(Case.case_id == case_id).with_for_update())
                history.record(session, case, graph.get_state(thread(case_id)).values, ledger=facts.ledger)
                session.commit()
                if session.get(Review, case_id) is not None:
                    written += 1
        except Exception:  # one unreadable case must not keep the service from starting
            log.exception("case %s: its missing review could not be written", case_id)
    log.info("Reviews written for closed cases that had none: %d of %d", written, len(missing))
    return written


def _set_status(case_id: str, status: str) -> None:
    with SessionLocal() as session:
        session.execute(update(Case).where(Case.case_id == case_id).values(status=status))
        session.commit()
