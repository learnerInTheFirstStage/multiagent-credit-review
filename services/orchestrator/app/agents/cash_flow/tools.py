"""Thin LangChain @tool wrappers over the Bank Service's analysis endpoints.

Every tool here is one HTTP GET to services/bank. Shape per KAN-20:
    GET /customers/{customer_id}/tools/<tool_name>?...
    → {data: {...}, evidence: [{id, kind, data}, ...]}

The specialist's LLM sees the tool docstrings as instructions, so they describe
the result's shape in words the model can act on. Each tool ends by handing the
response dict straight back — the agent layer will pick findings-worthy fields
out of `data` and accumulate `evidence` into the InvestigateResponse.

Design notes:
- The tools do NOT raise on HTTP errors or timeouts. They return a small
  structured observation `{"error": ..., "message": ...}` so the model can
  record a could-not-determine and move on, rather than killing the run.
- Evidence ids are plain strings (per Vincent's PR #4 Q3 lock-in). Nothing
  here invents them; Bank returns them.
- Memo sanitation is done ONE LEVEL UP, in guardrails.sanitize_tool_result,
  before the result is shown to the model.

All seven tools share the same signature pattern. One factory keeps the
duplication down.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


class _WindowArgs(BaseModel):
    """Arguments shared by the window-scoped bank tools."""

    start: str = Field(description="First day to include, YYYY-MM-DD.")
    end: str = Field(description="Last day to include, YYYY-MM-DD.")


class _CashSummaryArgs(_WindowArgs):
    pass


class _TransactionsArgs(_WindowArgs):
    min_abs_amount_cents: int | None = Field(
        default=None,
        description="If set, only transactions whose |amount| is at least this many cents.",
    )
    category: str | None = Field(
        default=None,
        description=(
            "If set, keep only transactions of this category. One of: "
            "payroll, tax, vendor_known, vendor_new, transfer_own_account, "
            "transfer_other, income, other."
        ),
    )
    limit: int = Field(default=100, description="Maximum rows to return.")


class _IncomeTrendArgs(BaseModel):
    end: str = Field(description="Trend ends at the week containing this day, YYYY-MM-DD.")
    weeks: int = Field(default=13, description="How many trailing weeks to include (2–52).")


class _PayeeHistoryArgs(BaseModel):
    counterparty_id: int = Field(
        description=(
            "A Counterparty row id, taken from an earlier transactions result. "
            "Look up the payee by this id to see its payment history."
        )
    )


class _AccountReportingArgs(BaseModel):
    as_of: str = Field(description="Compute days_stale against this day, YYYY-MM-DD.")


class _BalanceVsTxnArgs(_WindowArgs):
    pass


class _PriorYearCompareArgs(_WindowArgs):
    pass


def _bank_url() -> str:
    return os.environ.get("BANK_URL", "http://bank:8000")


def _call(
    http: httpx.Client, customer_id: str, token: str, name: str, **params: Any
) -> dict[str, Any]:
    url = f"{_bank_url()}/customers/{customer_id}/tools/{name}"
    try:
        response = http.get(
            url,
            params={k: v for k, v in params.items() if v is not None},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
    except httpx.HTTPError as error:
        return {"error": "bank_unreachable", "message": str(error)}
    if response.status_code == 200:
        try:
            return response.json()
        except ValueError as error:
            return {"error": "bad_bank_response", "message": str(error)}
    # Non-200: surface the structured error from Bank or wrap a plain string.
    try:
        body = response.json()
    except ValueError:
        body = {"error": "http_" + str(response.status_code), "message": response.text[:200]}
    return body


def build_tools(
    *, customer_id: str, token: str, http: httpx.Client | None = None
) -> tuple[list[StructuredTool], Any]:
    """Build the seven tools, pre-bound to this request's customer and token.

    Returns the tool list and a close() callable. close() closes the httpx
    client only when this function created it (not when the caller injected
    one). The agent wraps the main loop in try/finally to always call it.
    """
    owns_client = http is None
    client = http or httpx.Client()

    def _bind(name: str):
        def fn(**kwargs: Any) -> dict[str, Any]:
            return _call(client, customer_id, token, name, **kwargs)

        return fn

    tools = [
        StructuredTool.from_function(
            name="cash_summary",
            description=(
                "Summarise cash for a date window: opening and closing balances, "
                "inflow and outflow totals, and totals by category. Start by "
                "calling this when you need to know the size and shape of the change."
            ),
            args_schema=_CashSummaryArgs,
            func=_bind("cash_summary"),
        ),
        StructuredTool.from_function(
            name="transactions",
            description=(
                "List the raw transactions in a window, newest last. Pass "
                "min_abs_amount_cents to focus on large payments, or category "
                "to filter by kind. Each listed transaction also appears in "
                "evidence so a finding can cite it by id (txn_...)."
            ),
            args_schema=_TransactionsArgs,
            func=_bind("transactions"),
        ),
        StructuredTool.from_function(
            name="income_trend",
            description=(
                "Weekly inflow series ending at `end`, with slope and the last "
                "week's z-score against the earlier weeks. enough_history is "
                "false when any requested week has no inflow record — treat the "
                "trend as unreliable in that case."
            ),
            args_schema=_IncomeTrendArgs,
            func=_bind("income_trend"),
        ),
        StructuredTool.from_function(
            name="payee_history",
            description=(
                "Payment history with one counterparty: count, min/max/mean amount, "
                "first and last seen, and an is_new flag. Needs counterparty_id, "
                "which you take from an earlier transactions result."
            ),
            args_schema=_PayeeHistoryArgs,
            func=_bind("payee_history"),
        ),
        StructuredTool.from_function(
            name="account_reporting",
            description=(
                "For each linked account: last_reported_at and days_stale against "
                "`as_of`. Use this when an account may have stopped reporting and "
                "the balance only looks lower because it is missing."
            ),
            args_schema=_AccountReportingArgs,
            func=_bind("account_reporting"),
        ),
        StructuredTool.from_function(
            name="balance_vs_txn",
            description=(
                "Compare balance delta over a window with the sum of the window's "
                "transactions. Returns discrepancy_cents and a reconciled flag. "
                "Use this to answer 'do balances and transactions agree?'."
            ),
            args_schema=_BalanceVsTxnArgs,
            func=_bind("balance_vs_txn"),
        ),
        StructuredTool.from_function(
            name="prior_year_compare",
            description=(
                "Current window's inflow/outflow alongside the same ISO weeks of "
                "the prior year, plus a `similar` flag (within 20% on both sides). "
                "Use this for seasonal-dip questions."
            ),
            args_schema=_PriorYearCompareArgs,
            func=_bind("prior_year_compare"),
        ),
    ]

    def close() -> None:
        if owns_client:
            client.close()

    return tools, close
