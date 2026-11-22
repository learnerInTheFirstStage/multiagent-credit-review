from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from contracts.common import CustomerId, IsoDate

# Money is an integer number of US cents. Strict: "9600000", 9600000.0 and true are refused.
Cents = Annotated[int, Field(strict=True)]  # may be negative: an overdrawn account
NonNegativeCents = Annotated[int, Field(strict=True, ge=0)]

ReviewReason = Literal["large_cut", "failed_repayment", "incomplete_data", "large_limit"]


class LinkedAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    # Shows a feed that stopped reporting.
    last_synced: IsoDate
    # How a person knows the account, as a bank feed names it: "Chase Business Checking", and the last digits of
    # its number, "4821". The id stays the key.
    name: str | None = None
    mask: str | None = None


class BankSnapshot(BaseModel):
    # Extra fields are allowed: what else the engine saw is the Bank owner's call.
    model_config = ConfigDict(extra="allow")

    cash_now_cents: Cents
    cash_7d_ago_cents: Cents
    cash_30d_avg_cents: Cents
    accounts: list[LinkedAccount] = Field(min_length=1)


class Repayment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    due: IsoDate
    amount_cents: NonNegativeCents
    status: Literal["paid", "late", "failed"]


class CardSnapshot(BaseModel):
    # Extra fields are allowed: what else the engine saw is the Card owner's call.
    model_config = ConfigDict(extra="allow")

    balance_cents: NonNegativeCents
    repayments: list[Repayment]


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bank: BankSnapshot
    card: CardSnapshot


class Signal(BaseModel):
    """An engine decision held for review. Proposed shape, see API doc section 3.

    It says what the engine would do and why a person must look. It never says
    why the customer's numbers moved.
    """

    model_config = ConfigDict(extra="forbid")

    signal_id: str = Field(min_length=1, max_length=64)
    customer_id: CustomerId
    # The day the numbers were read.
    as_of: IsoDate
    current_limit_cents: NonNegativeCents
    # The limit the engine would set on its own.
    engine_limit_cents: NonNegativeCents
    # Why the engine did not act alone. The report must address each one.
    review_reasons: list[ReviewReason] = Field(min_length=1, max_length=4)
    snapshot: Snapshot

    @field_validator("review_reasons")
    @classmethod
    def _each_reason_once(cls, reasons: list[str]) -> list[str]:
        if len(set(reasons)) != len(reasons):
            raise ValueError("a review reason is listed twice")
        return reasons
