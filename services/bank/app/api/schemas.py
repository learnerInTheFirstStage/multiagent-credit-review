"""Response shapes for the Bank analysis tools.

Each tool returns the same envelope: `{data: {...}, evidence: [...]}`. `data`
is the tool's answer; `evidence` is every record the tool looked at, in the
shape the Cash Flow agent (in the Orchestrator Service) will inline into its
InvestigateResponse. Fields in `data` are flat, readable numbers so the model
can cite them without arithmetic.

Money is integer US cents (API doc section 1). Dates are ISO 8601.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

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


# ---- data shapes (what goes inside `data` per tool) ------------------------

class CashSummaryData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opening_cents: int
    closing_cents: int
    inflow_total_cents: int
    outflow_total_cents: int
    by_category: dict[str, int]  # category name -> signed total cents


class ListedTransaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    date: date
    amount_cents: int
    category: str
    counterparty_display: str | None
    memo: str | None


class ListTransactionsData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transactions: list[ListedTransaction]
    truncated: bool


class WeeklyInflowPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    week_start: date
    inflow_cents: int


class IncomeTrendData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    weeks: list[WeeklyInflowPoint]
    slope_cents_per_week: float
    zscore_of_last_week: float | None
    enough_history: bool


class PayeeHistoryData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counterparty_id: int
    counterparty_display: str | None
    n_payments: int
    min_amount_cents: int | None
    max_amount_cents: int | None
    mean_amount_cents: float | None
    first_seen: date | None
    last_seen: date | None
    is_new: bool


class AccountReportStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    last_reported_at: date
    days_stale: int
    active: bool


class AccountReportingData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accounts: list[AccountReportStatus]


class BalanceVsTxnData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    balance_delta_cents: int
    txn_sum_cents: int
    discrepancy_cents: int
    reconciled: bool


class PriorYearComparisonData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_inflow_cents: int
    current_outflow_cents: int
    prior_inflow_cents: int
    prior_outflow_cents: int
    similar: bool
    window_start: date
    window_end: date


ToolName = Literal[
    "cash_summary",
    "transactions",
    "income_trend",
    "payee_history",
    "account_reporting",
    "balance_vs_txn",
    "prior_year_compare",
]
