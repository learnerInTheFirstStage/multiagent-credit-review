import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import runner
from app.auth import User, current_user
from app.card_client import CardError
from app.db import get_session
from app.db.models import Case, CaseStatus

log = logging.getLogger(__name__)
router = APIRouter()

# Which roles may answer which approval step.
MAY_DECIDE = {"analyst": ("analyst", "manager"), "manager": ("manager",)}
# The report is shown once code has checked it, never while it is being drafted.
# In the graph state "failed" means closed after approval: see _close_unconfirmed.
REPORT_IS_CHECKED = ("needs_approval", "needs_manager", "done", "rejected", "failed")


class Decision(BaseModel):
    # Who decides comes from the token. A "by" in the body is refused, not ignored.
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]


class CaseView(BaseModel):
    """The case page. The frontend reads this, never the graph state."""

    case_id: str
    customer_id: str
    status: CaseStatus
    waiting_for: runner.WaitingFor | None
    report: dict | None
    decisions: list[dict]
    action: dict | None


def _case(session: Session, case_id: str, user: User, *, lock: bool = False) -> Case:
    query = select(Case).where(Case.case_id == case_id)
    case = session.scalar(query.with_for_update() if lock else query)
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"There is no case {case_id}")
    if case.customer_id not in user.customers:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This token may not see this customer")
    return case


def _view(case: Case, graph) -> CaseView:
    state, waiting = {}, None
    if graph is not None:
        snapshot = graph.get_state(runner.thread(case.case_id))
        state, waiting = snapshot.values, runner.waiting_for(snapshot)
    return CaseView(
        case_id=case.case_id,
        customer_id=case.customer_id,
        status=CaseStatus(case.status),
        waiting_for=waiting,
        report=state.get("report") if state.get("status") in REPORT_IS_CHECKED else None,
        decisions=state.get("decisions", []),
        action=state.get("action"),
    )


@router.get("/cases/{case_id}", response_model=CaseView)
def read_case(
    case_id: str,
    request: Request,
    user: User = Depends(current_user),
    session: Session = Depends(get_session),
) -> CaseView:
    return _view(_case(session, case_id, user), request.app.state.graph)


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
    last_approver = snapshot.values["decisions"][-1]["by"] if snapshot.values["decisions"] else None
    if step == "retry":
        # Approved earlier, but Card has not confirmed the action.
        if decision.decision == "approve" and user.sub == last_approver:
            resume = None  # the approver sends it again, under the same approval id
        elif decision.decision == "reject" and user.role == "manager":
            return _close_unconfirmed(session, case, graph, user)
        else:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Case {case_id} was approved by {last_approver}, but Card has not confirmed the action. "
                f"{last_approver} can approve again to retry it, or a manager can reject to close the case as failed",
            )
    elif user.role not in MAY_DECIDE[step]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"A {user.role} may not decide the {step} step")
    elif step == "manager" and user.sub == last_approver:
        # The manager step is a second pair of eyes.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"{user.sub} approved the analyst step. The manager step needs another person"
        )
    else:
        resume = Command(resume={"decision": decision.decision, "by": user.sub})

    try:
        state = graph.invoke(resume, config, context={"approver_token": user.token})
    except CardError as error:
        log.warning("case %s: approved by %s, but Card did not carry out the action: %s", case_id, user.sub, error)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "The approval is recorded, but Card did not carry out the action. Send the approval again to retry",
        ) from error

    case.status = state["status"]
    session.commit()
    log.info("case %s: %s by %s at the %s step, now %s", case_id, decision.decision, user.sub, step, case.status)
    return _view(case, graph)


def _close_unconfirmed(session: Session, case: Case, graph, user: User) -> CaseView:
    """Close a case whose approved action Card never confirmed, so it does not stay open for good.

    It ends as failed, not rejected: Card may have applied the action and lost its answer,
    so a person has to check. Nothing more is sent.
    """
    graph.update_state(
        runner.thread(case.case_id),
        {"status": "failed", "decisions": [{"step": "retry", "decision": "reject", "by": user.sub}]},
        as_node="act",  # as if the step had run, so the graph ends here
    )
    case.status = CaseStatus.FAILED
    session.commit()
    log.warning("case %s: closed as failed by %s, Card never confirmed the approved action", case.case_id, user.sub)
    return _view(case, graph)
