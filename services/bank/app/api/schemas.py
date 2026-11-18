"""Response shapes for the Bank analysis tools.

Each tool returns the same envelope: `{data: {...}, evidence: [...]}`. `data`
says what was asked and which ids answer it; `evidence` is the records the
answer rests on, in the shape the Cash Flow agent (in the Orchestrator Service)
inlines into its InvestigateResponse.

Money is integer US cents (API doc section 1). Dates are ISO 8601.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    data: dict[str, Any]


class ToolResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: dict[str, Any]
    evidence: list[Evidence]
