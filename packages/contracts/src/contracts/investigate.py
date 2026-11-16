"""What the graph passes to a specialist agent and what comes back.

Draft, as in API doc section 2. It is a function's input and output: both
specialists run inside the Orchestrator Service.
"""

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.common import CustomerId, IsoDate

AgentName = Literal["cash_flow", "credit_behavior"]


class InvestigateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    customer_id: CustomerId
    question: str = Field(min_length=1)
    window_start: IsoDate
    window_end: IsoDate
    # What the graph already knows, in a sentence or two. A specialist keeps nothing between calls.
    context: str = ""

    @model_validator(mode="after")
    def _window_runs_forward(self) -> Self:
        if self.window_start > self.window_end:
            raise ValueError("window_start is after window_end")
        return self


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1)
    # A label, not a number: a model's own score is not calibrated.
    confidence: Literal["high", "medium", "low"]
    evidence_ids: list[str] = Field(min_length=1)


class Evidence(BaseModel):
    """A record a tool returned. Code builds it, the model never does."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    data: dict[str, Any]


class InvestigateResponse(BaseModel):
    """Facts with the records behind them. Never a limit action."""

    model_config = ConfigDict(extra="forbid")

    agent: AgentName
    findings: list[Finding]
    evidence: list[Evidence]
    could_not_determine: list[Annotated[str, Field(min_length=1)]]
    suggested_next: str | None = None

    @model_validator(mode="after")
    def _findings_rest_on_the_evidence(self) -> Self:
        ids = [item.id for item in self.evidence]
        if len(set(ids)) != len(ids):
            raise ValueError("an evidence id appears twice")
        cited = {evidence_id for finding in self.findings for evidence_id in finding.evidence_ids}
        missing = sorted(cited - set(ids))
        if missing:
            raise ValueError(f"findings cite evidence that is not in the response: {missing}")
        if not self.findings and not self.could_not_determine:
            raise ValueError("no findings and no reason given in could_not_determine")
        return self
