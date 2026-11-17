from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, ForeignKey, Index, String, Text, func, text
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


class Review(Base):
    """One closed case, kept so that later cases can look it up."""

    __tablename__ = "reviews"

    case_id: Mapped[str] = mapped_column(ForeignKey("cases.case_id"), primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(16), index=True)
    as_of: Mapped[date] = mapped_column(Date, index=True)  # the signal's day, never the wall clock
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    outcome: Mapped[str] = mapped_column(String(16))  # done, rejected, failed (closed after approval)
    source: Mapped[str] = mapped_column(String(8))  # case or seed
    check_passed: Mapped[bool] = mapped_column(Boolean)
    likely_cause: Mapped[str | None] = mapped_column(String(32))
    recommendation: Mapped[str | None] = mapped_column(String(32))
    confidence: Mapped[str | None] = mapped_column(String(8))
    human_decision: Mapped[str | None] = mapped_column(String(32))  # Decision.decided, when a hand-over was approved
    guideline_ids: Mapped[list] = mapped_column(JSON)
    review_reasons: Mapped[list] = mapped_column(JSON)
    policy_version: Mapped[str | None] = mapped_column(String(32))
    current_limit_cents: Mapped[int] = mapped_column(BigInteger)
    engine_limit_cents: Mapped[int] = mapped_column(BigInteger)
    cash_7d_ago_cents: Mapped[int] = mapped_column(BigInteger)
    cash_now_cents: Mapped[int] = mapped_column(BigInteger)
    report: Mapped[dict | None] = mapped_column(JSON)  # the checked report, whole
    evidence: Mapped[list] = mapped_column(JSON)  # the cited records, copied: ids mean nothing outside their case
    decisions: Mapped[list] = mapped_column(JSON)  # with notes where given
    action: Mapped[dict | None] = mapped_column(JSON)
    asked: Mapped[list] = mapped_column(JSON)
    text: Mapped[str] = mapped_column(Text)  # what search reads, composed by code
    embedder: Mapped[str | None] = mapped_column(String(64))
    embedding: Mapped[list | None] = mapped_column(JSON)  # the vector as floats: the copy every database can hold
