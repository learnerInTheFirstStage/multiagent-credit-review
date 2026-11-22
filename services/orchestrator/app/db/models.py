from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CaseStatus(StrEnum):
    """Where a case stands. See API doc section 6."""

    QUEUED = "queued"
    INVESTIGATING = "investigating"
    # Bank or Card gave nothing at all. The case runs again from that question at its next retry (OutageWait).
    WAITING_FOR_DATA = "waiting_for_data"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_MANAGER = "needs_manager"
    # request_documents was approved: a person records what the customer's documents showed.
    AWAITING_DOCUMENTS = "awaiting_documents"
    DONE = "done"
    REJECTED = "rejected"
    FAILED = "failed"


OPEN_STATUSES = (
    CaseStatus.QUEUED,
    CaseStatus.INVESTIGATING,
    CaseStatus.WAITING_FOR_DATA,
    CaseStatus.NEEDS_APPROVAL,
    CaseStatus.NEEDS_MANAGER,
    CaseStatus.AWAITING_DOCUMENTS,
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
    # Copied from the graph state with the status (runner.keep_row), so the queue need not read the graph.
    waiting_for: Mapped[str | None] = mapped_column(String(16))
    recommendation: Mapped[str | None] = mapped_column(String(32))  # the checked report's, null before there is one
    confidence: Mapped[str | None] = mapped_column(String(8))
    # Card's words when it refused the approved action at the last attempt; null once it applies one or only failed.
    card_refusal: Mapped[str | None] = mapped_column(String(500))
    # YYYY-MM-DD the customer's documents are due, while the case waits for them (runner.keep_row); else null.
    documents_due: Mapped[str | None] = mapped_column(String(10))
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


class OutageWait(Base):
    """Why a case stopped for want of Bank or Card, since when, how often it was tried, and when it runs again.

    Kept while the case waits (waiting_for_data), and after it failed for the outage (next_retry_at is then None),
    so the case page can say why. Removed once a run gets past the outage, or fails for another reason.
    """

    __tablename__ = "outage_waits"

    case_id: Mapped[str] = mapped_column(ForeignKey("cases.case_id"), primary_key=True)
    service: Mapped[str] = mapped_column(String(32))  # "Bank Service" or "Card Service"
    codes: Mapped[str] = mapped_column(String(200))  # the outage codes, as "http_503, timeout"
    kind: Mapped[str] = mapped_column(String(16))  # transient, configuration or data (errors.OutageKind)
    attempts: Mapped[int] = mapped_column()  # runs that met the outage, the first one included
    asked_before: Mapped[int] = mapped_column()  # questions recorded before the one that met it (CaseWaiting)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


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
    decisions: Mapped[list] = mapped_column(JSON)  # with notes and reasons where given
    action: Mapped[dict | None] = mapped_column(JSON)
    asked: Mapped[list] = mapped_column(JSON)
    text: Mapped[str] = mapped_column(Text)  # what search reads, composed by code
    embedder: Mapped[str | None] = mapped_column(String(64))
    embedding: Mapped[list | None] = mapped_column(JSON)  # the vector as floats: the copy every database can hold
