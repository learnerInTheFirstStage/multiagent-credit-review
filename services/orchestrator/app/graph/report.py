"""The report the graph writes for the analyst. Draft, see API doc section 4.

The cause and recommendation lists (KAN-14) are defined here, and the frontend
keeps its own copy of the report's fields in frontend/src/types.ts. If another
service needs to import them, they move to packages/contracts.
"""

from typing import Literal

from pydantic import BaseModel, Field

from contracts.signal import ReviewReason

LikelyCause = Literal[
    "payroll_or_tax",
    "planned_vendor_payment",
    "seasonal_dip",
    "funds_moved_out_of_view",
    "revenue_loss",
    "unexplained_outflow",
    "no_clear_cause",
    "insufficient_data",
]
Recommendation = Literal[
    "keep_limit", "reduce_limit", "pause_card", "request_documents", "needs_human"
]


class Claim(BaseModel):
    claim: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)


class RuledOut(BaseModel):
    cause: LikelyCause
    reason: str = Field(min_length=1)
    evidence_ids: list[str]


class ReasonAddressed(BaseModel):
    """What the report says about one reason the engine did not act alone."""

    reason: ReviewReason
    finding: str = Field(min_length=1)
    # Empty when the specialists could not determine it.
    evidence_ids: list[str]


class ReportDraft(BaseModel):
    """What the model writes. Code adds the case, the customer and the engine's limit."""

    likely_cause: LikelyCause
    summary: str = Field(min_length=1)
    evidence: list[Claim]
    ruled_out: list[RuledOut]
    other_findings: list[Claim]
    review_reasons: list[ReasonAddressed]
    confidence: Literal["high", "medium", "low"]
    recommendation: Recommendation
    guideline_ids: list[str]
    next_action: str | None = None
