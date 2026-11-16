from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CaseStatus(StrEnum):
    """Where a case stands. See API doc section 6."""

    QUEUED = "queued"
    INVESTIGATING = "investigating"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_MANAGER = "needs_manager"
    DONE = "done"
    REJECTED = "rejected"
    FAILED = "failed"


OPEN_STATUSES = (
    CaseStatus.QUEUED,
    CaseStatus.INVESTIGATING,
    CaseStatus.NEEDS_APPROVAL,
    CaseStatus.NEEDS_MANAGER,
)
_is_open = text("status IN (" + ", ".join(f"'{s.value}'" for s in OPEN_STATUSES) + ")")


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        # One open case per customer, so a new signal joins it and opens no second one.
        Index(
            "one_open_case_per_customer",
            "customer_id",
            unique=True,
            postgresql_where=_is_open,
            sqlite_where=_is_open,
        ),
    )

    case_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(32), default=CaseStatus.QUEUED)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SignalRow(Base):
    """Every signal received, with the case it opened or joined."""

    __tablename__ = "signals"

    signal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("cases.case_id"), index=True)
    customer_id: Mapped[str] = mapped_column(String(16), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
