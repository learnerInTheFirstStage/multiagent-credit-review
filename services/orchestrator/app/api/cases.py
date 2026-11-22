import logging
from datetime import UTC, datetime
from typing import Literal

from contracts.logs import bind
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from app import history, runner
from app.agents.errors import OutageKind
from app.auth import User, current_user
from app.card_client import CardError
from app.db import get_session
from app.db.models import OPEN_STATUSES, Case, CaseStatus, OutageWait, Review, SignalRow
from app.graph import facts
from app.graph.build import DECISION_REASONS, DOCUMENTS_SHOWED, approval_outcomes, can_reduce

log = logging.getLogger(__name__)
router = APIRouter()

# Which roles may answer which approval step.
MAY_DECIDE = {"analyst": ("analyst", "manager"), "manager": ("manager",), "documents": ("analyst", "manager")}
# Who may run a case that waits for Bank or Card, or failed for want of them, again now.
MAY_RETRY = ("analyst", "manager")


class Decision(BaseModel):
    # Who decides, in which role and when come from the token and the clock (_stamp). A "by" in the body is
    # refused, not ignored.
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    # Why, in the person's words. Kept with the case's review, where later searches read it masked.
    note: str | None = Field(default=None, max_length=500)
    # What the person concluded on a report that handed the case over, or, required to approve the documents step,
    # what the customer's documents showed. A reduction or a pause is carried out as a recommended one would be: the
    # manager step when it needs one, then Card.
    decided: Literal["keep_limit", "reduce_limit", "pause_card", "request_documents"] | None = None
    # Why, as one label, at any step. Kept with the decision and the case's review, as the note is.
    reason: Literal[DECISION_REASONS] | None = None
    # The step the person decided on, as their page showed it. When the case has moved on since (someone else decided
    # first), the decision is refused rather than taken as an answer to the next step. Optional for older callers.
    step: runner.WaitingFor | None = None


class Outage(BaseModel):
    """Why a case waits for Bank or Card (waiting_for_data), or failed for want of them. See app/runner.py."""

    service: str
    # As the agent's service gave them, such as "http_503" or "http_401, timeout".
    codes: str
    kind: OutageKind
    # Runs that met the outage, the first one included.
    attempts: int
    since: datetime
    # None once the service retries no more: the case failed, and a person decides.
    next_retry_at: datetime | None
    message: str


def _outage(wait: OutageWait | None) -> Outage | None:
    if wait is None:
        return None
    shown = runner.outage_of(wait)
    return Outage(
        service=shown["service"],
        codes=shown["codes"],
        kind=shown["kind"],
        attempts=shown["attempts"],
        since=shown["first_seen"],
        next_retry_at=shown["next_retry_at"],
        message=runner.describe(shown),
    )


class CaseView(BaseModel):
    """The case page. The frontend reads this, never the graph state."""

    case_id: str
    customer_id: str
    status: CaseStatus
    waiting_for: runner.WaitingFor | None
    report: dict | None
    # The guidelines the report cites, in its order, as the case ran on them: id, title, when, allows, note.
    guidelines: list[dict]
    # Per choice the person can make at the step the case waits at: next ("manager", "card", "awaiting_documents" or
    # "done") and the action Card would be sent. None when the case waits for nobody, or for a Card retry.
    approval_outcomes: dict[str, dict] | None
    decisions: list[dict]
    # What the customer is asked for, once request_documents is approved: asked_for, message, due, requested_at,
    # requested_by. Kept after the case closes.
    documents_request: dict | None
    action: dict | None
    outage: Outage | None
    # Card's words when it refused the approved action, so sending it again would not help. None when Card applied
    # it, or only failed to answer (then it may have applied it).
    card_refusal: str | None
    # A review kept from earlier records for a case this system never investigated (a seed): what it recorded, so the
    # page shows it rather than an empty case. None for every case that ran here.
    past_review: dict | None = None


class CasePath(BaseModel):
    """How a case was investigated: what was asked, searched and checked, and every record it held. For grading."""

    case_id: str
    flow: str | None
    policy_version: str | None
    rounds: int
    # Calls refused before they were sent, every one of them.
    refusals: int
    # The questions ended because the round limit was reached.
    out_of_rounds: bool
    questions: list[dict]
    searches: list[dict]
    gaps: list[dict]
    drafts: list[dict]
    # The ids of the records the case was given about the customer's earlier reviews.
    history: list[str]
    # Every record of the case's ledger, its data raw but without text a customer or a person wrote. A grader
    # matches what a report cites by kind and data, not by the id one specialist happens to give.
    records: list[dict]
    handed_over_by: Literal["report", "low_confidence", "gave_up"] | None
    # The question that met it is not among the questions: it was neither answered nor "no answer".
    outage: Outage | None


class CaseRow(BaseModel):
    """One line of the queue. The case page has the rest."""

    case_id: str
    customer_id: str
    status: CaseStatus
    created_at: datetime
    updated_at: datetime
    outage: Outage | None
    # Every distinct review reason the signals for this case name, in the order
    # they first arrived. Empty means the case was opened before signals carried
    # reasons (not expected on new data), which is why the queue still renders
    # when it is empty.
    signal_reasons: list[str] = []
    # The checked report's, None before there is one. Kept on the row with the status (runner.keep_row).
    recommendation: str | None
    confidence: str | None
    # The engine's cut as basis points of the current limit, from the signal the case runs on. None when it cuts
    # nothing, and for a seeded case, which has no signal.
    cut_share_bp: int | None = None
    waiting_for: runner.WaitingFor | None
    # A review imported from earlier records (a seed): no signal, and no investigation in this system.
    imported: bool = False
    # YYYY-MM-DD the customer's documents are due, while the case waits for them.
    documents_due: str | None = None


class CaseList(BaseModel):
    cases: list[CaseRow]
    # How many cases match in all, for paging.
    total: int


@router.get("/cases", response_model=CaseList)
def list_cases(
    # Named case_status so it does not hide fastapi's status module in this file.
    case_status: CaseStatus | None = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> CaseList:
    """The queue: the open cases of the customers this token may see, the longest waiting first.

    With ?status= it lists the cases in that one status instead, closed ones included.
    """
    mine = Case.customer_id.in_(user.customers)
    # Open cases only unless asked: otherwise old finished cases would fill the first page.
    wanted = (mine, Case.status == case_status if case_status else Case.status.in_(OPEN_STATUSES))
    total = session.scalar(select(func.count()).select_from(Case).where(*wanted))
    # Materialised to a list because from_signals below needs the ids first,
    # and the CaseRow comprehension then iterates over rows again.
    rows = list(session.execute(
        select(Case, OutageWait)
        .outerjoin(OutageWait, OutageWait.case_id == Case.case_id)
        .where(*wanted)
        .order_by(Case.created_at, Case.case_id)
        .limit(limit)
        .offset(offset)
    ))
    signals = from_signals(session, [row.case_id for row, _ in rows])
    return CaseList(
        cases=[
            CaseRow(
                case_id=row.case_id,
                customer_id=row.customer_id,
                status=CaseStatus(row.status),
                created_at=row.created_at,
                updated_at=row.updated_at,
                outage=_outage(wait),
                # From the row, not the graph state: a page of 200 would read 200 checkpoints.
                recommendation=row.recommendation,
                confidence=row.confidence,
                waiting_for=row.waiting_for,
                documents_due=row.documents_due,
                **signals.get(row.case_id, {}),
            )
            for row, wait in rows
        ],
        total=total,
    )


def from_signals(session: Session, case_ids: list[str]) -> dict[str, dict]:
    """What a queue row shows of each case's signals: signal_reasons, every distinct review reason they named in
    arrival order, and cut_share_bp, from the first of them, the signal the case runs on (runner._opening_signal).

    Read from SignalRow.payload, one query for all cases, no N+1.
    """
    if not case_ids:
        return {}
    shown: dict[str, dict] = {}
    for signal in session.execute(
        select(SignalRow)
        .where(SignalRow.case_id.in_(case_ids))
        .order_by(SignalRow.received_at, SignalRow.signal_id)
    ).scalars():
        if signal.case_id not in shown:
            current, engine = signal.payload["current_limit_cents"], signal.payload["engine_limit_cents"]
            # Rounded as the signal's own record of the cut is (facts.signal_facts), which a report cites.
            cut = facts.bp(current - engine, current) if engine < current else None
            shown[signal.case_id] = {"signal_reasons": [], "cut_share_bp": cut, "imported": False}
        seen = shown[signal.case_id]["signal_reasons"]
        # `or []` so a payload that stored review_reasons as null (older data,
        # or a sender bug) returns no reasons instead of raising on iteration.
        for reason in signal.payload.get("review_reasons") or []:
            if reason not in seen:
                seen.append(reason)
    # A case opens only from a signal, so one with none is a review imported from earlier records.
    for case_id in case_ids:
        shown.setdefault(case_id, {"signal_reasons": [], "cut_share_bp": None, "imported": True})
    return shown


def _case(session: Session, case_id: str, user: User, *, lock: bool = False) -> Case:
    query = select(Case).where(Case.case_id == case_id)
    case = session.scalar(query.with_for_update() if lock else query)
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"There is no case {case_id}")
    if case.customer_id not in user.customers:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This token may not see this customer")
    return case


def _view(session: Session, case: Case, graph, role: str | None = None) -> CaseView:
    """The case page, for a reader in this role: what approving does depends on whether a manager approves."""
    state, waiting = {}, None
    if graph is not None:
        snapshot = graph.get_state(runner.thread(case.case_id))
        state, waiting = snapshot.values, runner.waiting_for(snapshot)
    report = runner.checked_report(state)
    ran_on = {guideline["id"]: guideline for guideline in state.get("guidelines", [])}
    cited_ids = dict.fromkeys(report["guideline_ids"]) if report else {}
    cited = [ran_on[guideline_id] for guideline_id in cited_ids if guideline_id in ran_on]
    return CaseView(
        case_id=case.case_id,
        customer_id=case.customer_id,
        status=CaseStatus(case.status),
        waiting_for=waiting,
        report=report,
        guidelines=[
            {
                "id": guideline["id"],
                "title": guideline["title"],
                "when": guideline["when"],
                "allows": guideline["allows"],
                "note": guideline["note"] or None,  # null, not "", for a guideline without one
            }
            for guideline in cited
        ],
        approval_outcomes=approval_outcomes(state, waiting, role),
        decisions=state.get("decisions", []),
        documents_request=state.get("documents_request"),
        action=state.get("action"),
        outage=_outage(session.get(OutageWait, case.case_id)),
        card_refusal=case.card_refusal,
        past_review=None if state else _past_review(session.get(Review, case.case_id)),
    )


def _past_review(review: Review | None) -> dict | None:
    """What a review kept from earlier records says, for a case with no run in this system: the signal's figures,
    the report as it was recorded and the decisions with their notes."""
    if review is None or review.source != "seed":
        return None
    report = review.report or {}
    return {
        "as_of": review.as_of.isoformat(),
        "outcome": review.outcome,
        "review_reasons": review.review_reasons,
        "current_limit_cents": review.current_limit_cents,
        "engine_limit_cents": review.engine_limit_cents,
        "cash_7d_ago_cents": review.cash_7d_ago_cents,
        "cash_now_cents": review.cash_now_cents,
        "likely_cause": review.likely_cause,
        "recommendation": review.recommendation,
        "confidence": review.confidence,
        "human_decision": review.human_decision,
        "guideline_ids": review.guideline_ids,
        "claims": [item["claim"] for item in report.get("evidence", []) if item.get("claim")],
        "decisions": review.decisions,
    }


@router.get("/cases/{case_id}", response_model=CaseView)
def read_case(
    case_id: str,
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> CaseView:
    return _view(session, _case(session, case_id, user), request.app.state.graph, user.role)


@router.post("/cases/{case_id}/retry", response_model=CaseView, status_code=status.HTTP_202_ACCEPTED)
def retry(
    case_id: str,
    request: Request,
    background: BackgroundTasks,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> CaseView:
    """Run a case that waits for Bank or Card, or failed for want of them, again now: from the question that met the
    outage, keeping everything asked before it. The run happens after the answer, which says "investigating".

    It counts as one more attempt (runner._wait_or_fail): a failed case that meets the outage again fails again.
    """
    graph = request.app.state.graph
    if graph is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No model is configured, so no case can run")
    case = _case(session, case_id, user, lock=True)
    if user.role not in MAY_RETRY:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"The role {user.role} may not retry a case")
    wait = session.get(OutageWait, case_id)
    if wait is None or case.status not in (CaseStatus.WAITING_FOR_DATA, CaseStatus.FAILED):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Case {case_id} is {case.status}. It is not waiting for Bank or Card"
        )
    if case.status == CaseStatus.FAILED:
        # A failed case is closed, so the customer may have a newer one open. Reopening this one would make two.
        other = session.scalar(
            select(Case.case_id).where(Case.customer_id == case.customer_id, Case.status.in_(OPEN_STATUSES))
        )
        if other is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{case.customer_id} has another open case, {other}. Case {case_id} stays failed",
            )
    # Only if the status is still the one read: the retry loop may have taken the case since (SQLite has no row lock).
    try:
        taken = session.execute(
            update(Case)
            .where(Case.case_id == case_id, Case.status == case.status)
            .values(status=CaseStatus.INVESTIGATING)
            .execution_options(synchronize_session=False)
        )
        session.commit()
    except IntegrityError:  # a signal opened a case for the customer in the meantime
        session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"{case.customer_id} has another open case. Case {case_id} stays failed"
        ) from None
    if taken.rowcount != 1:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Case {case_id} is already running again")
    # What was written, without reading it back: a read here would open a transaction that the run, which starts
    # before this session closes, has to wait for on SQLite.
    set_committed_value(case, "status", CaseStatus.INVESTIGATING)
    log.info("case %s: %s retries it after %s", case_id, user.sub, runner.describe(runner.outage_of(wait)))
    background.add_task(runner.run_case, graph, case_id)
    return _view(session, case, graph, user.role)


@router.get("/cases/{case_id}/path", response_model=CasePath)
def read_path(
    case_id: str,
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> CasePath:
    """The question path of a case. Empty for a case nobody ran, and while no model is configured."""
    case = _case(session, case_id, user)
    graph = request.app.state.graph
    state = graph.get_state(runner.thread(case.case_id)).values if graph is not None else {}
    asked = state.get("asked", [])
    # Each tool call adds one of each (graph/state.py). A case begun before asked was kept has more answers than
    # questions, and pairing them could put an answer under the wrong question: such a case shows none.
    answers = state.get("answers", [])
    if len(answers) != len(asked):
        answers = [{}] * len(asked)
    drafts = state.get("draft_log", [])
    last_draft = drafts[-1] if drafts else {}
    handed_over_by = None
    if (runner.checked_report(state) or {}).get("recommendation") == "needs_human":
        if not state.get("check", {}).get("passed"):
            handed_over_by = "gave_up"
        elif last_draft.get("result") == "passed_low_confidence" and last_draft.get("recommendation") != "needs_human":
            handed_over_by = "low_confidence"  # code turned a draft that acted into a hand-over
        else:
            handed_over_by = "report"
    gaps = state.get("gap_log", [])
    return CasePath(
        case_id=case.case_id,
        flow=state.get("flow"),
        policy_version=state.get("policy_version"),
        rounds=state.get("rounds", 0),
        # Not state["refusals"]: that one stops counting once the free refusals are used up.
        refusals=sum(1 for entry in asked if entry.get("outcome") == "refused"),
        out_of_rounds=any(entry.get("ended_by") == "round_limit" for entry in gaps),
        questions=[entry | _answered(answer) for entry, answer in zip(asked, answers)],
        searches=state.get("searches", []),
        gaps=gaps,
        drafts=drafts,
        history=[record["id"] for record in state.get("facts", []) if record["id"].startswith("hist_")],
        records=[
            {"id": record["id"], "kind": record["kind"], "data": _without_text(record["data"])}
            for record in facts.ledger(state).values()
        ],
        handed_over_by=handed_over_by,
        outage=_outage(session.get(OutageWait, case.case_id)),
    )


def _answered(answer: dict) -> dict:
    """What a specialist answered a question, as the investigator read it: its findings, each with its confidence and
    the records behind it, and what it could not determine. Its model wrote the words. Not its suggested next step,
    which nothing reads and Card does not check for figures. Nothing for a call refused before it was sent or a
    specialist that gave no usable answer."""
    response = answer.get("response")
    if not response:
        return {}
    return {
        "answer": {
            "findings": response.get("findings", []),
            "could_not_determine": response.get("could_not_determine", []),
        }
    }


def _without_text(value):
    """Data with the keys of free text taken out, at every depth. Figures keep their raw names: not facts.shown."""
    if isinstance(value, dict):
        return {
            key: _without_text(item)
            for key, item in value.items()
            if key not in ("memo", "note") and not key.endswith("_text")
        }
    if isinstance(value, list):
        return [_without_text(item) for item in value]
    return value


@router.post("/cases/{case_id}/decision", response_model=CaseView)
def decide(
    case_id: str,
    decision: Decision,
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> CaseView:
    """Approve or reject what the case is waiting on. An approved action goes to Card in this request."""
    graph = request.app.state.graph
    if graph is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "No model is configured, so no case can run")
    # Locked until this request ends, so two answers to one approval cannot both go through.
    case = _case(session, case_id, user, lock=True)
    config = runner.thread(case_id, user_id=user.sub)
    snapshot = graph.get_state(config)
    step = runner.waiting_for(snapshot)

    if step is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Case {case_id} is {case.status}. It is not waiting for a decision"
        )
    # Before any check of the body: they are about the step the case waits at, not the one the person saw.
    if decision.step is not None and decision.step != step:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Case {case_id} has moved on to the {step} step since your page showed the {decision.step} step. "
            "Reload it to see what it waits for now",
        )
    # Before the steps below: a rejection on the retry step would otherwise close the case first.
    if step == "documents" and decision.decision == "approve":
        if decision.decided not in DOCUMENTS_SHOWED:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Approving the documents step needs decided, what the customer's documents showed: "
                + ", ".join(DOCUMENTS_SHOWED),
            )
    elif decision.decided is not None and not (
        step == "analyst"
        and decision.decision == "approve"
        and snapshot.values["report"]["recommendation"] == "needs_human"
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "What the person decided is kept only when the analyst step approves a report that hands the case over, "
            "and when the documents step is approved",
        )
    # Card would refuse it, and the approval would be stuck. The case page does not offer it either.
    if decision.decided == "reduce_limit" and not can_reduce(snapshot.values["signal"]):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The engine's limit is not a lower limit Card can set, so there is no reduction to carry out",
        )
    last_approver = snapshot.values["decisions"][-1]["by"] if snapshot.values["decisions"] else None
    if step == "retry":
        # Approved earlier, but Card has not confirmed the action.
        if decision.decision == "approve" and user.sub == last_approver:
            resume = None  # the approver sends it again, under the same approval id
        elif decision.decision == "reject" and user.role == "manager":
            return _close_unconfirmed(session, case, graph, user, decision.note, decision.reason)
        else:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Case {case_id} was approved by {last_approver}, but Card has not confirmed the action. "
                f"{last_approver} can approve again to retry it, or a manager can reject to close the case as failed",
            )
    elif user.role not in MAY_DECIDE[step]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"The role {user.role} may not decide the {step} step")
    elif step == "manager" and user.sub == last_approver:
        # The manager step is a second pair of eyes on the approval before it, the analyst's or the documents step's.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"{user.sub} approved the {snapshot.values['decisions'][-1]['step']} step. "
            "The manager step needs another person",
        )
    else:
        # A note, a reason and what was decided travel only when given; who, in which role and when, always.
        resume = Command(resume={**decision.model_dump(exclude_none=True, exclude={"step"}), **_stamp(user)})

    try:
        with bind(case_id=case_id):
            state = graph.invoke(resume, config, context={"approver_token": user.token})
    except CardError as error:
        log.warning("case %s: approved by %s, but Card did not carry out the action: %s", case_id, user.sub, error)
        # The graph kept the approval and waits for its retry: the queue row says so. The status is the same.
        runner.keep_row(case, graph.get_state(config))
        # Kept for the case page after a reload: a refusal is not worth sending again, a failure may be.
        case.card_refusal = str(error)[:500] if error.refused else None
        session.commit()
        if error.refused:
            # Sending the same approval again gets the same answer, so do not suggest it.
            message = (f"The approval is recorded, but {error}. Sending it again will not change that; "
                       "a manager can reject to close the case")
        elif error.token_rejected:
            # What to put right is in the log line above. The person can only sign in afresh or pass it on.
            message = ("The approval is recorded, but Card did not accept your sign-in, so it did not carry out the "
                       "action. Sign in again and send the approval again. If Card still does not accept it, Card is "
                       "not set up as the Orchestrator is: ask whoever runs the system to rebuild Card")
        else:
            message = "The approval is recorded, but Card did not carry out the action. Send the approval again to retry"
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, message) from error

    runner.keep_row(case, graph.get_state(config))
    case.card_refusal = None
    # In the transaction that writes the status, so a closed case is never left without its review.
    history.record(session, case, state, ledger=facts.ledger)
    session.commit()
    log.info("case %s: %s by %s at the %s step, now %s", case_id, decision.decision, user.sub, step, case.status)
    return _view(session, case, graph, user.role)


def _stamp(user: User) -> dict:
    """Who decided, in which role and when, for the case's record. From the token and this clock, never the body.
    by_name, the person's email when the token carries it, is for showing them; by stays who decided."""
    stamp = {"by": user.sub, "by_role": user.role, "decided_at": datetime.now(UTC).isoformat(timespec="seconds")}
    if user.name:
        stamp["by_name"] = user.name
    return stamp


def _close_unconfirmed(
    session: Session, case: Case, graph, user: User, note: str | None, reason: str | None
) -> CaseView:
    """Close a case whose approved action Card never confirmed, so it does not stay open for good.

    It ends as failed, not rejected: Card may have applied the action and lost its answer,
    so a person has to check. Nothing more is sent.
    """
    closing = {"step": "retry", "decision": "reject", **_stamp(user)}
    # Cleaned as the graph cleans the note of any other decision.
    if note and (note := note.replace("\x00", "").strip()):
        closing["note"] = note
    if reason:
        closing["reason"] = reason
    config = runner.thread(case.case_id)
    graph.update_state(
        config,
        {"status": "failed", "decisions": [closing]},
        as_node="act",  # as if the step had run, so the graph ends here
    )
    snapshot = graph.get_state(config)
    runner.keep_row(case, snapshot)
    history.record(session, case, snapshot.values, ledger=facts.ledger)
    session.commit()
    log.warning("case %s: closed as failed by %s, Card never confirmed the approved action", case.case_id, user.sub)
    return _view(session, case, graph, user.role)
