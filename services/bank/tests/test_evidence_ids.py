"""One id, one content: whatever tool returns a record, and whatever it was asked, the id means one thing.

The Orchestrator keeps the first record it sees under an id (graph/facts.py), so a second
content under the same id would be silently dropped or, worse, read in place of the first.
"""

import json
import re

from app.api.evidence import window_suffix
from tests.conftest import ALL_CUSTOMERS, call

_WINDOWS = [
    {},
    {"start": "2026-11-03", "end": "2026-11-10"},
    {"start": "2026-11-03", "end": "2026-11-06"},
    {"start": "2026-11-05", "end": "2026-11-10"},
    {"start": "2026-11-04", "end": "2026-11-05"},
    {"start": "2026-08-01", "end": "2026-11-10"},
    {"start": "2024-01-01", "end": "2026-11-10"},
    {"start": "2026-11-10", "end": "2026-11-20"},
]
_JUNK = {"weeks": 4, "as_of": "2026-11-01", "end": "2026-10-01", "counterparty_id": 1}


def _questions(client, headers, cid: str):
    """Every tool, asked many ways."""
    for window in _WINDOWS:
        yield "transactions", window
        yield "transactions", {**window, "category": "payroll"}
        yield "transactions", {**window, "min_abs_amount_cents": 10_000_000, "limit": 1}
        yield "cash_summary", window
        yield "balance_vs_txn", window
    yield "income_trend", {}
    yield "income_trend", _JUNK
    yield "prior_year_compare", {}
    yield "prior_year_compare", {"start": "2025-01-01", "end": "2025-02-01"}
    yield "account_reporting", {}
    yield "account_reporting", {"as_of": "2026-12-25"}
    accounts = call(client, headers, cid, "account_reporting").json()["data"]["account_ids"]
    for account in [*accounts, f"acct_external_new_{cid.replace('_', '')}", "acct_nobody"]:
        yield "account_reporting", {"account_id": account}
    every = call(client, headers, cid, "transactions", start="2024-01-01", end="2026-11-10", limit=500).json()
    for ref in every["data"]["transaction_ids"]:
        yield "payee_history", {"record_id": ref}


def test_no_id_ever_carries_two_contents(client, auth_headers):
    contents: dict[str, str] = {}
    clashes, answers = [], 0
    for cid in ALL_CUSTOMERS:
        for tool, params in _questions(client, auth_headers, cid):
            response = call(client, auth_headers, cid, tool, **params)
            assert response.status_code == 200, (cid, tool, params, response.text)
            answers += 1
            ids = [record["id"] for record in response.json()["evidence"]]
            assert len(ids) == len(set(ids)), (cid, tool, params, ids)
            for record in response.json()["evidence"]:
                content = json.dumps({"kind": record["kind"], "data": record["data"]}, sort_keys=True)
                if contents.setdefault(record["id"], content) != content:
                    clashes.append(f"{record['id']} from {tool} {params}")
    assert answers > 26 * 50
    assert not clashes, "\n".join(clashes[:20])


def test_ids_follow_the_stand_ins_scheme(client, auth_headers):
    pattern = re.compile(
        r"(txn_c_\d{3}_(\d{2}|p\d{2}|[wy]\d{2}_\d{2})|acct_\w+|bal_c_\d{3}(_[0-9a-f]{8})?"
        r"|agg_c_\d{3}_(payments|reconciliation|inflows|prior_year|payee_history)(_[0-9a-f]{8}|_(p|[wy]\d{2}_)?\d{2})?)"
    )
    for cid in ("c_001", "c_005", "c_007", "c_011", "c_012", "c_106"):
        for tool, params in _questions(client, auth_headers, cid):
            for record in call(client, auth_headers, cid, tool, **params).json()["evidence"]:
                assert pattern.fullmatch(record["id"]), record["id"]


def test_no_transaction_any_tool_returns_is_a_filler_the_rules_read_as_unexplained(client, auth_headers):
    for cid in ("c_001", "c_003", "c_012"):
        for tool, params in _questions(client, auth_headers, cid):
            for record in call(client, auth_headers, cid, tool, **params).json()["evidence"]:
                if record["kind"] == "bank_transaction":
                    assert record["data"]["category"] != "other", record


def test_window_suffix_is_empty_for_the_review_window_and_a_hash_otherwise():
    from datetime import date

    week = (date(2026, 11, 3), date(2026, 11, 10))
    assert window_suffix(*week, week) == ""
    other = window_suffix(date(2026, 11, 3), date(2026, 11, 9), week)
    assert re.fullmatch(r"_[0-9a-f]{8}", other)
    assert other == window_suffix(date(2026, 11, 3), date(2026, 11, 9), week)
    assert other != window_suffix(date(2026, 11, 4), date(2026, 11, 9), week)
