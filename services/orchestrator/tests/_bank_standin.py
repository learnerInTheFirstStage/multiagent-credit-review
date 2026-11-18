"""Bank tool responses recorded from the real Bank Service, and an HTTP stub that serves them.

tests/fixtures/bank_tools_standin.json holds Bank's {data, evidence} answers for every customer the stand-in
(mock._Bank) holds, keyed "<customer>/<tool>", "<customer>/payee_history/<transaction id>" and
"<customer>/account_reporting/<account id>" (each linked account, and each account a payment of the review
window went to). Bank
returns exactly the stand-in's records, so test_cash_flow_fixture.py checks the evidence against the
stand-in (build(), which groups the stand-in's records into the tools by TOOL_KINDS).

Re-record from a Bank started from this repository (fresh database, the team's and the evaluation's
customer files, JWT_SECRET set), then stop it:

    python -m tests._bank_standin --from-bank http://127.0.0.1:<port> > tests/fixtures/bank_tools_standin.json

The stub keeps every request, so a test can assert exactly what the agent sent (BankStandin.params).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable

import httpx

from app.agents import mock
from contracts import InvestigateRequest

FIXTURE = Path(__file__).parent / "fixtures" / "bank_tools_standin.json"

# Tool -> the kinds of record it returns. One kind may come back from two tools (daily_balance).
TOOL_KINDS = {
    "cash_summary": ("daily_balance", "payment_summary"),
    "transactions": ("bank_transaction",),
    "income_trend": ("weekly_inflow_series",),
    "account_reporting": ("linked_account",),
    "balance_vs_txn": ("balance_reconciliation", "daily_balance"),
    "prior_year_compare": ("prior_year_comparison",),
}


def _records(customer_id: str) -> dict[str, tuple[str, dict]]:
    data = mock._data()
    start, end = data["week"]
    request = InvestigateRequest(case_id="fixture", customer_id=customer_id, question="fixture",
                                 window_start=start.isoformat(), window_end=end.isoformat())
    return mock._Bank(data, data["customers"][customer_id], request).records


def _envelope(items: list[dict]) -> dict:
    return {"data": {"record_ids": [item["id"] for item in items]}, "evidence": items}


def build() -> dict[str, Any]:
    """Every response the stub serves, from the stand-in's records of every customer it holds."""
    out: dict[str, Any] = {}
    for customer_id in sorted(mock.customer_ids()):
        records = _records(customer_id)
        items = [{"id": id, "kind": kind, "data": data} for id, (kind, data) in records.items()]
        for tool, kinds in TOOL_KINDS.items():
            out[f"{customer_id}/{tool}"] = _envelope([item for item in items if item["kind"] in kinds])
        histories = {item["data"].get("about"): item for item in items if item["kind"] == "payee_history"}
        for item in items:
            if item["kind"] == "bank_transaction":
                found = [histories[item["id"]]] if item["id"] in histories else []
                out[f"{customer_id}/payee_history/{item['id']}"] = _envelope([*found, item])
    return out


def from_bank(base_url: str, secret: str) -> dict[str, Any]:
    """Every response the stub serves, asked of a running Bank Service with the arguments the agent sends for
    the review window, and for each payment's history and each account a payment went to."""
    import time

    import jwt

    data = mock._data()
    start, end = (day.isoformat() for day in data["week"])
    customers = sorted(mock.customer_ids())
    token = jwt.encode({"sub": "recorder", "role": "service", "customers": customers, "exp": int(time.time()) + 600},
                       secret, algorithm="HS256")
    out: dict[str, Any] = {}
    with httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=30) as http:
        def ask(customer_id: str, tool: str, **params: str) -> dict:
            response = http.get(f"/customers/{customer_id}/tools/{tool}", params=params)
            response.raise_for_status()
            return response.json()

        for customer_id in customers:
            window = {"start": start, "end": end}
            for tool, params in (("cash_summary", window), ("transactions", window), ("income_trend", {}),
                                 ("account_reporting", {}), ("balance_vs_txn", window), ("prior_year_compare", {})):
                out[f"{customer_id}/{tool}"] = ask(customer_id, tool, **params)
            listed = [item for item in out[f"{customer_id}/transactions"]["evidence"]
                      if item["kind"] == "bank_transaction"]
            for item in listed:
                out[f"{customer_id}/payee_history/{item['id']}"] = ask(customer_id, "payee_history",
                                                                       record_id=item["id"])
            linked = [item["id"] for item in out[f"{customer_id}/account_reporting"]["evidence"]]
            sent_to = {item["data"]["to_account"] for item in listed if item["data"].get("to_account")}
            for account in sorted({*linked, *sent_to}):
                out[f"{customer_id}/account_reporting/{account}"] = ask(customer_id, "account_reporting",
                                                                        account_id=account)
    return out


def recorded() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


class BankStandin:
    """httpx.MockTransport over the fixture. Answers for the customer in the URL, as Bank does, and keeps every
    request. .when(tool, ...) replaces one tool's answer."""

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses if responses is not None else recorded()
        self.requests: list[httpx.Request] = []
        self._overrides: dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    def when(self, tool: str, body: Any = None, *, status_code: int = 200,
             handler: Callable[[httpx.Request], httpx.Response] | None = None) -> "BankStandin":
        self._overrides[tool] = handler or (lambda _request: httpx.Response(status_code, json=body))
        return self

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parts = request.url.path.strip("/").split("/")  # customers/<id>/tools/<tool>
        customer_id, tool = parts[1], parts[-1]
        if tool in self._overrides:
            return self._overrides[tool](request)
        key = f"{customer_id}/{tool}"
        if tool == "payee_history":
            key += f"/{request.url.params.get('record_id')}"
        elif tool == "account_reporting" and (account := request.url.params.get("account_id")):
            # Recorded for each linked account and each account a payment went to. Any other id gets what Bank
            # gives it: every linked account, and no payment.
            if f"{key}/{account}" in self.responses:
                key += f"/{account}"
        if key not in self.responses:
            return httpx.Response(404, json={"detail": {"error": "not_found", "message": f"Nothing for {key}"}})
        return httpx.Response(200, json=copy.deepcopy(self.responses[key]))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._dispatch))

    def called(self) -> list[str]:
        """The tools asked, in order."""
        return [request.url.path.rsplit("/", 1)[-1] for request in self.requests]

    def params(self, tool: str) -> list[dict[str, str]]:
        return [dict(request.url.params) for request in self.requests if request.url.path.endswith(f"/{tool}")]


if __name__ == "__main__":
    import os
    import sys

    if sys.argv[1:2] == ["--from-bank"]:
        print(json.dumps(from_bank(sys.argv[2], os.environ["JWT_SECRET"]), indent=1, sort_keys=True))
    else:
        print(json.dumps(build(), indent=1, sort_keys=True))
