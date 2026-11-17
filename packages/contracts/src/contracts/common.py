"""Field types used by more than one contract."""

from datetime import date
from typing import Annotated

from pydantic import BeforeValidator, Field


def _parse_iso_date(value: object) -> date:
    # Left to itself Pydantic reads a number, or a string of digits, as a Unix timestamp.
    if not isinstance(value, str):
        raise ValueError("a date must be an ISO 8601 string such as 2026-11-10")
    return date.fromisoformat(value)


IsoDate = Annotated[date, BeforeValidator(_parse_iso_date)]

# The only id shared across services.
CustomerId = Annotated[str, Field(pattern=r"^c_[0-9]{3}$")]
