"""Runs cases on the graph and keeps the cases table in step with it.

One process is assumed. Two would both pick up an unfinished case after a restart.

When Bank or Card gives nothing at all, the graph stops before that question (errors.CaseWaiting). The case then
waits (waiting_for_data) and runs again from that question at its next retry, policy.OUTAGE_RETRY_SECONDS after
the last attempt. Once those retries are used up, or at once when retrying cannot help (an outage of kind data),
it goes to a person: failed, with the reason in outage_waits and no report.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from contracts.logs import bind
from langgraph.types import StateSnapshot
from sqlalchemy import delete, select, update

from app import history, policy
from app.agents.errors import CaseWaiting
from app.db import SessionLocal
from app.db.models import OPEN_STATUSES, Case, CaseStatus, OutageWait, Review, SignalRow
from app.graph import facts

log = logging.getLogger(__name__)

# The next move belongs to the analyst, to the manager, to the approver again, or to whoever records what the
# customer's documents showed: "retry" means the action was approved but Card has not carried it out.
WaitingFor = Literal["analyst", "manager", "retry", "documents"]
_WAITING_AT: dict[tuple, WaitingFor] = {
    ("analyst_approval",): "analyst",
    ("manager_approval",): "manager",
    ("act",): "retry",
    ("documents_approval",): "documents",
}
# The report is shown once code has checked it, never while it is being drafted.
# In the graph state "failed" means closed after approval: see api/cases._close_unconfirmed.
REPORT_IS_CHECKED = ("needs_approval", "needs_manager", "awaiting_documents", "done", "rejected", "failed")


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


def checked_report(state: dict) -> dict | None:
    return state.get("report") if state.get("status") in REPORT_IS_CHECKED else None


def keep_row(case: Case, snapshot: StateSnapshot) -> None:
    """Write the status the graph saved onto the case's row, with what the queue shows of it besides: whose move it
    is and the checked report's recommendation and confidence. Every status read from the graph is written here.

    The other status writes are of a case that waits for nobody (queued, investigating, waiting for Bank or Card,
    failed by its run): _set_status clears waiting_for, and the rest find it clear.
    """
    report = checked_report(snapshot.values) or {}
    case.status = snapshot.values["status"]
    case.waiting_for = waiting_for(snapshot)
    case.recommendation = report.get("recommendation")
    case.confidence = report.get("confidence")
    # The queue shows when a request for documents falls due, so an overdue one is seen without opening the case. By
    # whose move it is, not the status: once the documents step is decided, a Card retry keeps the old status.
    waiting = case.waiting_for == "documents"
    case.documents_due = (snapshot.values.get("documents_request") or {}).get("due") if waiting else None


def unfinished() -> list[str]:
    """Every open case but those waiting for Bank or Card, oldest first.

    A restart may have left one queued or half investigated, or with a status in
    the table that is older than what the graph has saved. A waiting case is left to
    retry_due: run at every start, a crash loop would use up its retries at once.
    """
    with SessionLocal() as session:
        return list(
            session.scalars(
                select(Case.case_id)
                .where(Case.status.in_(OPEN_STATUSES), Case.status != CaseStatus.WAITING_FOR_DATA)
                .order_by(Case.created_at)
            )
        )


def run_case(graph, case_id: str, now: datetime | None = None) -> None:
    """Take a case as far as it goes without a person. Safe to call on a case at any point.

    now is when an outage met in this run counts as met: the current time unless given.
    """
    # Every line the run logs carries the case, the graph's and the specialists' too.
    with bind(case_id=case_id):
        config = thread(case_id)
        try:
            snapshot = graph.get_state(config)
            if not snapshot.values:
                _set_status(case_id, CaseStatus.INVESTIGATING)
                graph.invoke({"case_id": case_id, "signal": _opening_signal(case_id)}, config)
            elif snapshot.next and waiting_for(snapshot) is None:
                # A restart stopped it part-way, or Bank or Card gave nothing. Go on from the last saved step.
                _set_status(case_id, CaseStatus.INVESTIGATING)
                graph.invoke(None, config)
            # Otherwise the graph is where it should be and only the table may be behind.
            status = _catch_up(graph, case_id)
        except CaseWaiting as outage:
            try:
                status = _wait_or_fail(case_id, outage, now or datetime.now(UTC))
            except Exception:  # the database is gone too. The case stays investigating; the next start picks it up.
                log.exception("case %s: %s, and the wait could not be recorded", case_id, outage)
                return
        except Exception:
            log.exception("case %s failed", case_id)
            status = CaseStatus.FAILED
            _set_status(case_id, status, outage_over=True)  # it failed, but not for want of Bank or Card
        log.info("case %s is %s", case_id, status)


def retry_due(graph, now: datetime | None = None, stopping: Callable[[], bool] = lambda: False) -> list[str]:
    """Run every case waiting for Bank or Card whose retry time has come, one after another. Returns their ids.

    Each case is taken first (claim), so the retry endpoint and this cannot both run it. Given now, it is also when
    an outage met again counts as met; left out, each run reads the clock when it meets one. Once stopping() says
    so, no further case is started: those left run at the next start.
    """
    with SessionLocal() as session:
        due = list(
            session.scalars(
                select(Case.case_id)
                .join(OutageWait, OutageWait.case_id == Case.case_id)
                .where(Case.status == CaseStatus.WAITING_FOR_DATA, OutageWait.next_retry_at <= (now or datetime.now(UTC)))
                .order_by(OutageWait.next_retry_at, Case.case_id)
            )
        )
    ran = []
    for case_id in due:
        if stopping():
            break
        try:
            if not claim(case_id, CaseStatus.WAITING_FOR_DATA):
                continue
            ran.append(case_id)
            run_case(graph, case_id, now)
        except Exception:  # one case that cannot even be marked must not keep the others waiting
            log.exception("case %s: its retry could not be started", case_id)
    return ran


def claim(case_id: str, status: str) -> bool:
    """Mark a case investigating if it still has this status. False when another run took it first."""
    with SessionLocal() as session:
        taken = session.execute(
            update(Case).where(Case.case_id == case_id, Case.status == status).values(status=CaseStatus.INVESTIGATING)
        )
        session.commit()
        return taken.rowcount == 1


def _wait_or_fail(case_id: str, outage: CaseWaiting, now: datetime) -> str:
    """Record an outage the run met: the case waits for its next retry, or fails once retrying is done.

    Attempt n waits OUTAGE_RETRY_SECONDS[n-1]. The attempt after the last of them fails the case, and so does
    the first one when retrying cannot help. A run that got past the question and met an outage at a later one
    meets a new outage: its attempts start again.
    """
    with SessionLocal() as session:
        case = session.scalar(select(Case).where(Case.case_id == case_id).with_for_update())
        wait = session.get(OutageWait, case_id)
        if wait is None:
            wait = OutageWait(case_id=case_id)
            session.add(wait)
        if wait.asked_before != outage.asked_before:
            wait.attempts, wait.first_seen, wait.asked_before = 0, now, outage.asked_before
        wait.service, wait.codes, wait.kind = outage.service, ", ".join(outage.codes)[:200], outage.kind
        wait.attempts += 1
        delays = policy.OUTAGE_RETRY_SECONDS
        if outage.kind == "data" or wait.attempts > len(delays):
            wait.next_retry_at, case.status = None, CaseStatus.FAILED
        else:
            wait.next_retry_at = now + timedelta(seconds=delays[wait.attempts - 1])
            case.status = CaseStatus.WAITING_FOR_DATA
        said = describe(outage_of(wait))
        session.commit()
    # A refused token or a wrong URL waits for someone to fix the setup; a service that is down may come back.
    level = logging.WARNING if outage.kind == "transient" else logging.ERROR
    log.log(level, "case %s is %s: %s (%s)", case_id, case.status, said, outage.kind)
    return case.status


def outage_of(wait: OutageWait) -> dict:
    """What the case page shows of a wait, times in UTC. SQLite gives them back without their zone."""

    def utc(moment: datetime | None) -> datetime | None:
        return moment if moment is None or moment.tzinfo else moment.replace(tzinfo=UTC)

    return {
        "service": wait.service,
        "codes": wait.codes,
        "kind": wait.kind,
        "attempts": wait.attempts,
        "first_seen": utc(wait.first_seen),
        "next_retry_at": utc(wait.next_retry_at),
    }


def describe(wait: dict) -> str:
    """The reason a person reads: "Bank Service unavailable (http_503) since 2026-11-18T10:00Z, retried 3 times"."""

    def at(moment: datetime) -> str:
        return f"{moment.astimezone(UTC):%Y-%m-%dT%H:%MZ}"

    said = f"{wait['service']} unavailable ({wait['codes']}) since {at(wait['first_seen'])}"
    retried = wait["attempts"] - 1
    if wait["kind"] == "data" and wait["next_retry_at"] is None and retried == 0:
        return f"{said}, not retried: retrying cannot help"
    said += f", retried {retried} time{'' if retried == 1 else 's'}"
    if wait["next_retry_at"] is not None:
        said += f", next retry at {at(wait['next_retry_at'])}"
    return said


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
    A run that got here got past any outage it waited on.
    """
    with SessionLocal() as session:
        case = session.scalar(select(Case).where(Case.case_id == case_id).with_for_update())
        snapshot = graph.get_state(thread(case_id))
        keep_row(case, snapshot)
        # A resend Card applied after an earlier refusal, whose own write of the row was lost: the refusal is over.
        # Only on Card's receipt, so a case a manager closed after the refusal keeps it.
        if (snapshot.values.get("action") or {}).get("result"):
            case.card_refusal = None
        session.execute(delete(OutageWait).where(OutageWait.case_id == case_id))
        history.record(session, case, snapshot.values, ledger=facts.ledger)
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


def _set_status(case_id: str, status: str, *, outage_over: bool = False) -> None:
    with SessionLocal() as session:
        # A run picks up a case nobody is asked about. One a decision left part-way may still say who was.
        session.execute(update(Case).where(Case.case_id == case_id).values(status=status, waiting_for=None))
        if outage_over:
            session.execute(delete(OutageWait).where(OutageWait.case_id == case_id))
        session.commit()
