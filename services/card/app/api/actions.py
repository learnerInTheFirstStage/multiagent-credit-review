"""POST /customers/{customer_id}/actions: carry out an action a person approved (KAN-28).

Called by the Orchestrator's code after the approval step, never by an agent: an agent's
service token is refused. The body is what services/orchestrator/app/card_client.py sends:

    {"type": "reduce_limit", "limit_cents": 5800000, "case_id": "case_ab12cd34",
     "customer_id": "c_010", "approved_by": "u_analyst", "approval_id": "case_ab12cd34:analyst"}
    {"type": "pause_card", ..., "approval_id": "case_ab12cd34:manager"}

Card checks everything again, whatever the Orchestrator already checked:
- The token is a person's (analyst, manager or admin), it names this customer, and its `sub` is
  `approved_by`: the approver's own token, not a service's.
- `approval_id` is required and is `<case_id>:analyst` or `<case_id>:manager`.
- The step and the role fit the action, by the Orchestrator's rule (MANAGER_CUT in its graph):
  a card pause, or a cut of more than half the current limit, needs the manager step, which only
  a manager decides. Any other reduction needs the analyst step, which an analyst or a manager
  decides. A manager's approval also covers an action that needed only the analyst's.
- Each approval is applied once. The receipt is written in the same transaction as the change,
  keyed by approval_id. A retry of the same approval gets the stored receipt back
  (status already_applied) and changes nothing; the same approval_id with another action is a 409.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import Caller, current_caller, refused
from app.db import get_session
from app.db.models import AppliedAction, CreditLimit, Customer
from app.ledger import service_as_of

log = logging.getLogger(__name__)
router = APIRouter()

PEOPLE = ("analyst", "manager", "admin")
# Who may decide each approval step: the Orchestrator's MAY_DECIDE (app/api/cases.py).
MAY_DECIDE = {"analyst": ("analyst", "manager"), "manager": ("manager",)}
STEP_RANK = {"analyst": 1, "manager": 2}


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["reduce_limit", "pause_card"]
    # The new limit, for reduce_limit only.
    limit_cents: int | None = Field(default=None, ge=0, strict=True)
    case_id: str = Field(pattern=r"^case_[A-Za-z0-9_-]{1,56}$")
    customer_id: str = Field(pattern=r"^c_[0-9]{3}$")
    approved_by: str = Field(min_length=1, max_length=128)
    approval_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.type == "reduce_limit" and self.limit_cents is None:
            raise ValueError("reduce_limit needs limit_cents, the new limit")
        if self.type == "pause_card" and self.limit_cents is not None:
            raise ValueError("pause_card takes no limit_cents")
        if self.approval_id not in (f"{self.case_id}:analyst", f"{self.case_id}:manager"):
            raise ValueError("approval_id must be <case_id>:analyst or <case_id>:manager")
        return self

    @property
    def step(self) -> str:
        return self.approval_id.rsplit(":", 1)[1]


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approval_id: str
    case_id: str
    customer_id: str
    type: str
    status: Literal["applied", "already_applied"]
    approved_by: str
    approver_role: str
    applied_at: datetime
    details: dict[str, Any]


def _receipt(row: AppliedAction, status_: str) -> Receipt:
    # SQLite hands back a naive timestamp (stored as UTC), Postgres an aware one: one shape either way.
    applied_at = row.applied_at if row.applied_at.tzinfo else row.applied_at.replace(tzinfo=UTC)
    return Receipt(approval_id=row.approval_id, case_id=row.case_id, customer_id=row.customer_id, type=row.type,
                   status=status_, approved_by=row.approved_by, approver_role=row.approver_role,
                   applied_at=applied_at.astimezone(UTC), details=row.details)


def _same(row: AppliedAction, body: ActionRequest) -> bool:
    return (row.type, row.limit_cents, row.case_id, row.customer_id) == (
        body.type, body.limit_cents, body.case_id, body.customer_id)


def _retry(row: AppliedAction, body: ActionRequest) -> Receipt:
    if not _same(row, body):
        raise HTTPException(status.HTTP_409_CONFLICT, {
            "error": "conflict", "message": f"{body.approval_id} was already used for another action"})
    return _receipt(row, "already_applied")


def _needs_manager(body: ActionRequest, current_limit: int) -> bool:
    # More than half the current limit: new < current / 2, in whole cents.
    return body.type == "pause_card" or 2 * (body.limit_cents or 0) < current_limit


@router.post("/customers/{customer_id}/actions", response_model=Receipt)
def act(
    customer_id: Annotated[str, Path(pattern=r"^c_[0-9]{3}$")],
    body: ActionRequest,
    session: Annotated[Session, Depends(get_session)],
    caller: Annotated[Caller, Depends(current_caller)],
) -> Receipt:
    if body.customer_id != customer_id:
        raise HTTPException(422, {"error": "invalid_request", "message": "customer_id in the body is not the one in the URL"})
    if caller.role not in PEOPLE:
        raise refused(status.HTTP_403_FORBIDDEN, "forbidden", "An action needs the approver's own token, not a service token")
    if customer_id not in caller.customers:
        raise refused(status.HTTP_403_FORBIDDEN, "forbidden", f"This token does not cover customer {customer_id}")
    if body.approved_by != caller.sub:
        raise refused(status.HTTP_403_FORBIDDEN, "forbidden", "approved_by is not the person this token belongs to")

    if (done := session.get(AppliedAction, body.approval_id)) is not None:
        return _retry(done, body)

    # Locked until commit on Postgres, so two approvals for one customer apply one after the other.
    customer = session.scalars(select(Customer).where(Customer.customer_id == customer_id).with_for_update()).first()
    if customer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"error": "not_found", "message": f"Unknown customer {customer_id}"})

    current = customer.credit_limit_cents
    if body.type == "reduce_limit" and body.limit_cents >= current:
        raise HTTPException(422, {"error": "invalid_request",
                                  "message": f"limit_cents {body.limit_cents} is not below the current limit {current}"})
    needed = "manager" if _needs_manager(body, current) else "analyst"
    if caller.role not in MAY_DECIDE[body.step]:
        raise refused(status.HTTP_403_FORBIDDEN, "forbidden", f"The role {caller.role} may not approve the {body.step} step")
    if STEP_RANK[body.step] < STEP_RANK[needed]:
        raise refused(status.HTTP_403_FORBIDDEN, "forbidden",
                      "A card pause or a cut of more than half the limit needs the manager's approval")

    now = datetime.now(UTC)
    if body.type == "reduce_limit":
        customer.credit_limit_cents = body.limit_cents
        # Dated the data's own today, so snapshots and tools on that day see the new limit.
        session.add(CreditLimit(customer_id=customer_id, effective_on=service_as_of(), limit_cents=body.limit_cents,
                                reason=f"approved reduction, case {body.case_id}", approval_id=body.approval_id))
        details = {"previous_limit_cents": current, "limit_cents": body.limit_cents,
                   "cut_bp": round(10_000 * (current - body.limit_cents) / current) if current else None}
    else:
        details = {"previous_card_status": customer.card_status, "card_status": "paused"}
        customer.card_status = "paused"

    row = AppliedAction(approval_id=body.approval_id, case_id=body.case_id, customer_id=customer_id, type=body.type,
                        limit_cents=body.limit_cents, approved_by=caller.sub, approver_role=caller.role,
                        applied_at=now, details={k: v for k, v in details.items() if v is not None})
    session.add(row)
    try:
        session.commit()
    except IntegrityError:
        # The same approval arrived twice at once and the other one committed first.
        session.rollback()
        if (done := session.get(AppliedAction, body.approval_id)) is not None:
            return _retry(done, body)
        raise
    log.info("action applied approval=%s case=%s customer=%s type=%s by=%s role=%s details=%s",
             row.approval_id, row.case_id, customer_id, row.type, row.approved_by, row.approver_role, row.details)
    return _receipt(row, "applied")
