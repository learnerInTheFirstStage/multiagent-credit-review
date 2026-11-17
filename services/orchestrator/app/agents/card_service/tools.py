"""The Card Service's analysis tools, as LangChain tools and as plain calls.

Every tool is one GET to services/card (KAN-25):
    GET {CARD_URL}/customers/{customer_id}/tools/<name>?...  ->  {data, evidence}

The model sees the descriptions below. Code also calls the same endpoints directly, before the
model starts (agent.PREFLIGHT), so the facts a topic rests on are always fetched.

No tool raises on an HTTP error or a timeout. It returns {"error": ..., "message": ...}, so a
Card outage becomes a could-not-determine, not a crashed case. The customer comes from the
request and the token, never from the model: no tool takes a customer_id.
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

TIMEOUT_SECONDS = 10.0


def card_url() -> str:
    return os.environ.get("CARD_URL", "http://card:8000")


class _Repayments(BaseModel):
    count: int = Field(default=6, ge=1, le=13, description="How many of the latest due repayments, 1-13.")


class _PayingAccount(BaseModel):
    count: int = Field(default=3, ge=2, le=13, description="How many of the latest repayments to compare, 2-13.")


class _Utilization(BaseModel):
    on: str | None = Field(default=None, description="Day to read, YYYY-MM-DD. Default: the latest day the data covers.")


class _SpendByCycle(BaseModel):
    cycles: int = Field(default=6, ge=3, le=13, description="How many closed statement cycles, 3-13.")


class _Window(BaseModel):
    start: str = Field(description="First day, YYYY-MM-DD.")
    end: str = Field(description="Last day, YYYY-MM-DD.")


class _Transactions(_Window):
    merchant: str | None = Field(default=None, description="Part of the merchant name or descriptor.")
    category: str | None = Field(default=None, description="A spend category exactly as an earlier result showed it.")
    mcc: str | None = Field(default=None, description="A 4-digit merchant category code.")
    card_id: str | None = Field(default=None, description="One card, such as crd_c_003_2.")
    state: Literal["approved", "declined", "any"] = "any"
    min_amount_cents: int | None = Field(default=None, ge=0, description="Only transactions at least this large.")
    limit: int = Field(default=20, ge=1, le=50, description="Most rows to return.")


class _SpendBreakdown(_Window):
    by: Literal["category", "merchant", "card", "mcc"] = "category"


# name -> (path on the Card Service, argument schema, what the model is told)
SPECS: dict[str, tuple[str, type[BaseModel], str]] = {
    "card_repayments": ("repayments", _Repayments, (
        "The latest repayments that are due, oldest first. Each is paid, late (with days_late) or failed "
        "(with the bank's return code and paid_since: whether it has been paid since). data.counts has the "
        "counts, worked out in code. Evidence: one card_repayment per repayment (rep_...)."
    )),
    "card_paying_account": ("paying_account", _PayingAccount, (
        "Whether the bank account the card is paid from changed across the latest repayments: changed, from, "
        "to, changed_on. Evidence: one funding_account_change, plus the repayments compared."
    )),
    "card_utilization": ("utilization", _Utilization, (
        "The card balance over the limit at the end of a day, and utilization_trend against the same day of "
        "the three months before: spiking, rising, falling, steady, or too_short_to_tell. Evidence: "
        "card_balance records (bal_card_...)."
    )),
    "card_spend_by_cycle": ("spend_by_cycle", _SpendByCycle, (
        "Card spend per statement cycle. The current cycle against the median of the closed ones "
        "(monthly_card_spend_cents), change_vs_usual_bp, and unusual (up, down or no). history is "
        "too_short_to_tell when there are fewer than three closed cycles. Evidence: one card_spend_series."
    )),
    "card_transactions": ("transactions", _Transactions, (
        "Card transactions in a window that match every filter given, largest first, with totals and "
        "declines by reason. Use it to see what a change in spend or a run of declines is made of. "
        "Evidence: one card_transaction per row (ctx_...) and a summary."
    )),
    "card_spend_breakdown": ("spend_breakdown", _SpendBreakdown, (
        "Approved spend in a window grouped by category, merchant, card or MCC, against the period of the "
        "same length just before; new groups are flagged. Evidence: one card_spend_breakdown."
    )),
}


class CardTools:
    """The tools for one request: one customer, one token, one HTTP client."""

    def __init__(self, *, customer_id: str, token: str, http: httpx.Client | None = None):
        self.customer_id, self.token = customer_id, token
        self._owns = http is None
        self.http = http or httpx.Client()

    def call(self, name: str, **args: Any) -> dict[str, Any]:
        """One tool call. Returns Card's {data, evidence}, or {error, message}."""
        spec = SPECS.get(name)
        if spec is None:
            return {"error": "unknown_tool", "message": f"No such tool: {name}. Tools: {', '.join(SPECS)}."}
        path, schema, _ = spec
        try:
            params = schema.model_validate(args).model_dump(exclude_none=True)
        except ValueError as error:
            return {"error": "invalid_arguments", "message": str(error)[:400]}
        url = f"{card_url()}/customers/{self.customer_id}/tools/{path}"
        try:
            response = self.http.get(url, params=params, headers={"Authorization": f"Bearer {self.token}"},
                                     timeout=TIMEOUT_SECONDS)
        except httpx.HTTPError as error:
            return {"error": "card_unreachable", "message": str(error)[:200] or type(error).__name__}
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code == 200 and isinstance(body, dict) and "data" in body:
            return body
        # Card's errors are {"detail": {"error", "message"}}; FastAPI's own 422s are {"detail": [...]}.
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, dict) and "error" in detail:
            return {"error": str(detail["error"]), "message": str(detail.get("message", ""))[:300]}
        return {"error": f"http_{response.status_code}", "message": (json.dumps(detail) if detail else response.text)[:300]}

    def langchain_tools(self) -> list[StructuredTool]:
        """The tools as the model sees them, for bind_tools. The graph's `tools` node runs the calls
        itself, through call(), so it can keep the budget and the cache."""
        return [
            StructuredTool.from_function(name=name, description=description, args_schema=schema,
                                         func=lambda _n=name, **kwargs: self.call(_n, **kwargs))
            for name, (_, schema, description) in SPECS.items()
        ]

    def close(self) -> None:
        if self._owns:
            self.http.close()
