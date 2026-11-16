from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
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


class SignalRow(Base):
    """Every signal received, kept so a repeated signal_id opens no second case."""

    __tablename__ = "signals"

    signal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(16), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Case(Base):
    __tablename__ = "cases"

    case_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # Unique: one signal opens at most one case.
    signal_id: Mapped[str] = mapped_column(ForeignKey("signals.signal_id"), unique=True)
    customer_id: Mapped[str] = mapped_column(String(16), index=True)
    status: Mapped[str] = mapped_column(String(32), default=CaseStatus.QUEUED)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
