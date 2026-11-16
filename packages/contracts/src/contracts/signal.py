from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Snapshot(BaseModel):
    """Raw numbers the engine saw. The exact fields are the Bank and Card owners' call."""

    model_config = ConfigDict(extra="forbid")

    bank: dict[str, Any]
    card: dict[str, Any]


class Signal(BaseModel):
    """A review signal from the risk engine. See API doc section 3."""

    model_config = ConfigDict(extra="forbid")

    signal_id: str = Field(min_length=1, max_length=64)
    customer_id: str = Field(pattern=r"^c_[0-9]{3}$")
    # The POC handles one signal type.
    type: Literal["balance_drop"]
    # Strict: "9600000", 9600000.0 and true are refused, not converted.
    engine_limit_cents: int = Field(ge=0, strict=True)
    snapshot: Snapshot
    # Required even when empty, so a sender that forgets it is told.
    hard_facts: list[str]
    # Proposed in the Orchestrator PRD, not in the API doc yet.
    review_reason: str | None = None
    as_of: date | None = None
