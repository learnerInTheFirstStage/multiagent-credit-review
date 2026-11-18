"""The Bank Service's analysis tools, as LangChain tools, and the table of what each topic fetches.

Every tool is one GET to services/bank:
    GET {BANK_URL}/customers/{customer_id}/tools/<name>?...  ->  {data, evidence: [{id, kind, data}]}

The model sees the descriptions below. Code calls the same tools before the model starts (PREFLIGHT),
so the records a topic rests on are always fetched. Both go through the LangChain tool objects
(`.invoke`), so callbacks and LangFuse see every Bank call as a tool run.

No call raises. An HTTP error, a refusal, a timeout or a body that is not the envelope comes back as
{"error": <code>, "message": ...}, so a Bank failure becomes a could-not-determine, not a crashed case. When
every call of a run failed as an outage (BankTools.outage), the agent's answer is an errors.Unanswered,
so the case waits and asks again, or fails with the reason. Each outage is logged as a warning.
The customer comes from the request and the token, never from the model: no tool takes a customer_id.
Every call waits at most TIMEOUT_SECONDS, and never past the run's deadline.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Literal

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, ValidationError

from app.agents.errors import outage
from app.graph.causes import KIND_TOPIC

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10.0
_DAY = r"^\d{4}-\d{2}-\d{2}$"
_CODE = re.compile(r"[^A-Za-z0-9_]")


def bank_url() -> str:
    return os.environ.get("BANK_URL", "http://bank:8000")


# The interface is services/bank/README.md, "Tool interface for the Cash Flow agent". A tool takes only what
# Bank reads: Bank ignores any other parameter, so a date it does not take would be claimed for figures it
# never limited, and a required one Bank has a default for would cost a call when left out.
_CATEGORIES = ("payroll", "tax", "vendor_known", "vendor_new", "transfer_own_account", "transfer_other", "income",
               "other", "operating")


class _Window(BaseModel):
    start: str | None = Field(default=None, pattern=_DAY, description=(
        "Window start, YYYY-MM-DD: the cash at the end of this day. Left out with end: the review window."))
    end: str | None = Field(default=None, pattern=_DAY, description=(
        "Window end, YYYY-MM-DD, inclusive. The same day as start is that one day."))


class _Transactions(_Window):
    category: Literal[_CATEGORIES] | None = Field(default=None, description=(  # type: ignore[valid-type]
        "List only this category: " + ", ".join(_CATEGORIES) + "."))
    min_abs_amount_cents: int | None = Field(default=None, ge=0, description="List only movements this large.")
    limit: int | None = Field(default=None, ge=1, le=500, description="Most transactions to list, 1-500 (100).")


class _NoArguments(BaseModel):
    pass


class _PayeeHistory(BaseModel):
    record_id: str = Field(pattern=r"^txn_[A-Za-z0-9_]+$", description="The id of one transaction, txn_...")


class _AccountReporting(BaseModel):
    account_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]{1,64}$", description=(
        "Left out: every linked account. A linked account's id: that one. Any other id: every linked account, "
        "and each payment of the review window that went to that id."))


# name -> (path on the Bank Service, argument schema, what the model is told).
SPECS: dict[str, tuple[str, type[BaseModel], str]] = {
    "cash_summary": ("cash_summary", _Window, (
        "Linked cash at both ends of a window, and what the window's payments add up to, without the list. "
        "Evidence: daily_balance (bal_...) and payment_summary (agg_..._payments).")),
    "transactions": ("transactions", _Transactions, (
        "Linked cash at both ends of a window, its itemised transactions, and what the payments add up to. The "
        "filters choose which transactions are listed; the summary is always the whole window's. A payment of "
        "the review window carries share_of_fall_bp. Evidence: daily_balance, one bank_transaction per listed "
        "transaction (txn_...), payment_summary.")),
    "income_trend": ("income_trend", _NoArguments, (
        "Weekly inflows over the 13 weeks that end on the service's as-of date, whatever window the question "
        "is about: this week's level, the level it is compared with, and income_trend (steady, shrinking, "
        "stopped or too_short_to_tell). Takes no arguments. Evidence: weekly_inflow_series (agg_..._inflows).")),
    "payee_history": ("payee_history", _PayeeHistory, (
        "How the payee of one payment was paid before: how often, since when, the usual amount, and this payment "
        "against it. Pass the transaction's id. Evidence: payee_history (agg_..._payee_history) when the payee "
        "was paid before, and the payment.")),
    "account_reporting": ("account_reporting", _AccountReporting, (
        "Linked accounts: whether each is active, days since it last reported, and is_stale, as of the "
        "service's as-of date. Evidence: linked_account (acct_...), and with an id that is not linked, the "
        "review window's payments to it (bank_transaction).")),
    "balance_vs_txn": ("balance_vs_txn", _Window, (
        "Whether the change in balance over a window matches its transactions: reconciled, and what matches no "
        "transaction. No verdict when an account is not reporting or cash did not fall. Evidence: "
        "balance_reconciliation (agg_..._reconciliation) and daily_balance.")),
    "prior_year_compare": ("prior_year_compare", _NoArguments, (
        "What went out and came in over the same weeks last year, against the review window's listed payments. "
        "Takes no arguments. Evidence: prior_year_comparison (agg_..._prior_year).")),
}

# What code fetches before the model starts, per topic: (tool, {tool parameter: request field}, the request
# field the call needs, or None). A parameter whose field the request leaves out is not sent. The one place
# that maps a topic to Bank calls. None is "whatever you hold".
_WINDOW = {"start": "window_start", "end": "window_end"}
PREFLIGHT: dict[str | None, list[tuple[str, dict[str, str], str | None]]] = {
    "transactions": [("cash_summary", _WINDOW, None), ("transactions", _WINDOW, None)],
    "income": [("income_trend", {}, None)],
    "payee_history": [("payee_history", {"record_id": "record_id"}, "record_id")],
    # An account that is not linked: Bank adds the review window's payments that went to it.
    "accounts": [("account_reporting", {"account_id": "account_id"}, None)],
    "reconciliation": [("balance_vs_txn", _WINDOW, None)],
    "prior_year": [("prior_year_compare", {}, None)],
}
PREFLIGHT[None] = [
    *PREFLIGHT["transactions"], *PREFLIGHT["income"], PREFLIGHT["accounts"][0],
    *PREFLIGHT["reconciliation"], *PREFLIGHT["prior_year"],
]  # fmt: skip
TOPICS = tuple(topic for topic in PREFLIGHT if topic is not None)
# The kinds of record each topic answers with, from the Orchestrator's own mapping. Code-written findings
# (hard facts, and the answer written from the records) cover only these: a record fetched in support of
# another kind (the daily balance balance_vs_txn returns) is returned as evidence, but its finding is written
# under its own topic. With no topic every kind is in scope.
TOPIC_KINDS: dict[str, tuple[str, ...]] = {
    topic: tuple(kind for kind, (tool, of) in KIND_TOPIC.items() if (tool, of) == ("AskCashFlow", topic))
    for topic in TOPICS
}


# Payments that move money out of view or to someone never paid before: always a hard fact (findings.py).
OUT_OF_VIEW = ("transfer_own_account", "vendor_new")


def on_topic(record: dict, request: dict[str, Any]) -> bool:
    """Whether code may write a finding from the record for this request. The payee_history topic is also
    about the payment asked about when it went out of view or to a payee never paid before: Bank returns no
    history record for one, and the payment's hard fact is the answer. Any other payment's sentence is the
    transactions topic's. The accounts topic about one account also answers which payments of the window
    went to it."""
    topic = request.get("topic")
    if topic is None or record["kind"] in TOPIC_KINDS[topic]:
        return True
    if topic == "payee_history":
        return record["id"] == request.get("record_id") and record["data"].get("category") in OUT_OF_VIEW
    account = request.get("account_id")
    return (topic == "accounts" and account is not None and record["kind"] == "bank_transaction"
            and record["data"].get("to_account") == account)


def nothing_on_topic(records: dict[str, dict], request: dict[str, Any], said: list[str]) -> str | None:
    """What to say when records came back but none of them answers the topic asked, and Bank did not say why.
    When it did (`said`, from gaps()), its reason is said instead: "Linked cash did not fall in the window"
    is why balance_vs_txn returns no reconciliation."""
    topic = request.get("topic")
    if topic is None or said or not records or any(on_topic(r, request) for r in records.values()):
        return None
    return f"The Bank Service returned no {topic} record: what it returned is about other topics."


def gaps(result: Any) -> list[str]:
    """What Bank said it could not work out in a tool result: each line of its data.not_available, whitespace
    collapsed, and for a payee it says was never paid before with no such line, that there is no history."""
    data = result.get("data") if isinstance(result, dict) and "error" not in result else None
    if not isinstance(data, dict):
        return []
    lines = data["not_available"] if isinstance(data.get("not_available"), list) else []
    said = [" ".join(line.split())[:300] for line in lines if isinstance(line, str) and line.strip()]
    if not said and data.get("paid_before") is False:
        said = ["The Bank Service has no earlier payment to this payee on record."]
    return said


def call_key(name: str, args: Any) -> str:
    """One call, as the run's cache and BankTools.gaps key it."""
    return name + json.dumps(args, sort_keys=True, default=str)


def preflight_calls(request: dict[str, Any]) -> tuple[list[tuple[str, dict[str, Any]]], list[str]]:
    """The calls the request's topic rests on, with their arguments, and why any of them cannot be made."""
    calls, missing = [], []
    for tool, params, needs in PREFLIGHT[request.get("topic")]:
        if needs is not None and not request.get(needs):
            if needs == "record_id":
                missing.append("A payee's history needs record_id: the id of one transaction.")
            continue
        calls.append((tool, {param: request[field] for param, field in params.items() if request.get(field)}))
    return calls, missing


def now() -> float:
    return time.monotonic()


def error_code(result: Any) -> str | None:
    """The error code of a failed call, as letters, digits and underscores only. None for a result."""
    if isinstance(result, dict) and "error" in result:
        return _CODE.sub("_", str(result["error"]))[:40] or "error"
    return None


class BankTools:
    """The tools for one request: one customer, one token, one HTTP client, one deadline.

    Keeps every record any call returned, first one kept, and what Bank said it could not work out for each
    call (`said`, from gaps()), so the entry function can still answer from them if the graph fails part way.
    """

    def __init__(self, *, customer_id: str, token: str, deadline: float, http: httpx.Client | None = None):
        self.customer_id, self.token, self.deadline = customer_id, token, deadline
        self._owns = http is None
        self.http = http or httpx.Client()
        self.records: dict[str, dict] = {}
        self.said: dict[str, list[str]] = {}  # call_key(name, params) -> gaps(), first answer kept
        # One entry per call that went to Bank: None when it returned data, an outage code (outage()), or "other".
        self.reached: list[str | None] = []
        self.tools = [
            StructuredTool.from_function(name=name, description=description, args_schema=schema,
                                         func=lambda _name=name, **kwargs: self.call(_name, **kwargs))
            for name, (_, schema, description) in SPECS.items()
        ]
        self.by_name = {tool.name: tool for tool in self.tools}

    def call(self, name: str, **args: Any) -> dict[str, Any]:
        """One call to Bank. Returns Bank's {data, evidence}, or {error, message}."""
        spec = SPECS.get(name)
        if spec is None:
            return {"error": "unknown_tool", "message": f"No such tool: {name}. Tools: {', '.join(SPECS)}."}
        path, schema, _ = spec
        try:
            params = schema.model_validate(args).model_dump(exclude_none=True)
        except ValidationError as error:
            return {"error": "invalid_arguments", "message": str(error)[:300]}
        remaining = self.deadline - now()
        if remaining <= 0:
            return {"error": "deadline_passed", "message": "The run's time was used up before this call."}
        url = f"{bank_url()}/customers/{self.customer_id}/tools/{path}"
        try:
            response = self.http.get(url, params=params, headers={"Authorization": f"Bearer {self.token}"},
                                     timeout=min(TIMEOUT_SECONDS, remaining))
        except httpx.TimeoutException:
            self._down(name, "timeout")
            return {"error": "bank_timeout", "message": "The Bank Service did not answer in time."}
        except httpx.HTTPError as error:
            self._down(name, "unreachable")
            return {"error": "bank_unreachable", "message": type(error).__name__}
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code == 200 and isinstance(body, dict) and isinstance(body.get("evidence", []), list):
            if "data" in body:
                self.reached.append(None)
                self._keep(body)
                self.said.setdefault(call_key(name, params), gaps(body))
                return body
        # FastAPI's errors are {"detail": {"error", "message"}} or {"detail": [...]}; some are {"error", ...}.
        detail = body.get("detail") if isinstance(body, dict) else None
        found = detail if isinstance(detail, dict) else body if isinstance(body, dict) else {}
        if response.status_code == 200:
            self.reached.append("other")
            return {"error": "bad_bank_response", "message": "The answer was not {data, evidence}."}
        if down := outage(response.status_code, detail if isinstance(detail, dict) else {}):
            self._down(name, down)
        else:
            self.reached.append("other")
        return {"error": str(found.get("error") or f"http_{response.status_code}"),
                "message": str(found.get("message", ""))[:300], "status": response.status_code}

    def _down(self, name: str, code: str) -> None:
        self.reached.append(code)
        log.warning("cash_flow: the Bank Service did not answer %s for %s: %s", name, self.customer_id, code)

    def outage(self) -> str | None:
        """The error codes, when Bank was asked and every call failed as an outage; None otherwise."""
        if self.reached and all(code not in (None, "other") for code in self.reached):
            return ", ".join(dict.fromkeys(self.reached))
        return None

    def _keep(self, body: dict) -> None:
        for item in records_in(body):
            self.records.setdefault(item["id"], item)

    def close(self) -> None:
        if self._owns:
            self.http.close()


def records_in(result: Any) -> list[dict]:
    """The records of a tool result as Bank returned them: {id, kind, data}, anything else in an item left out.

    An item without a text id, a text kind and a data object is not a record and cannot be cited.
    """
    if not isinstance(result, dict) or not isinstance(result.get("evidence"), list):
        return []
    out = []
    for item in result["evidence"]:
        if (isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
                and isinstance(item.get("kind"), str) and item["kind"].strip() and isinstance(item.get("data"), dict)):
            out.append({"id": item["id"], "kind": item["kind"], "data": item["data"]})
    return out
