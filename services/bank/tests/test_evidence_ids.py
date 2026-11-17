"""Evidence id rules — the format Vincent agreed to on PR #4 (Q3)."""

import re

from app.api.evidence import (
    aggregate_evidence_id,
    balance_evidence_id,
    txn_evidence_id,
)
from datetime import date


def test_row_level_id_prefixes():
    assert txn_evidence_id(42) == "txn_42"
    assert balance_evidence_id("acct_c002_a", date(2026, 11, 10)) == "bal_acct_c002_a_20261110"


def test_aggregate_id_format():
    id_ = aggregate_evidence_id("cash_summary", "c_002", "2026-11-03", "2026-11-10")
    assert re.fullmatch(r"agg_[0-9a-f]{4}", id_), id_


def test_aggregate_id_is_deterministic():
    # Same inputs → same id. The report check compares cited vs returned ids
    # as plain strings, so this property is load-bearing.
    a = aggregate_evidence_id("cash_summary", "c_002", "2026-11-03", "2026-11-10")
    b = aggregate_evidence_id("cash_summary", "c_002", "2026-11-03", "2026-11-10")
    assert a == b


def test_aggregate_id_changes_when_inputs_change():
    a = aggregate_evidence_id("cash_summary", "c_002", "2026-11-03", "2026-11-10")
    b = aggregate_evidence_id("cash_summary", "c_002", "2026-11-03", "2026-11-09")  # different end
    c = aggregate_evidence_id("income_trend", "c_002", "2026-11-03", "2026-11-10")  # different tool
    d = aggregate_evidence_id("cash_summary", "c_003", "2026-11-03", "2026-11-10")  # different customer
    assert len({a, b, c, d}) == 4


def test_two_cash_summary_calls_return_the_same_aggregate_id(client, auth_headers):
    url = "/customers/c_002/tools/cash_summary?start=2026-11-03&end=2026-11-10"
    first = client.get(url, headers=auth_headers).json()
    second = client.get(url, headers=auth_headers).json()
    first_agg = [e["id"] for e in first["evidence"] if e["kind"] == "cash_summary"][0]
    second_agg = [e["id"] for e in second["evidence"] if e["kind"] == "cash_summary"][0]
    assert first_agg == second_agg


def test_every_tool_response_passes_the_specialist_contract_rule(client, auth_headers):
    """Mirror InvestigateResponse's validator: every id a tool returns must be
    unique inside its own response. KAN-30 built the contract check on this."""
    urls = [
        "/customers/c_002/tools/cash_summary?start=2026-11-03&end=2026-11-10",
        "/customers/c_002/tools/transactions?start=2026-11-03&end=2026-11-10",
        "/customers/c_002/tools/income_trend?end=2026-11-10&weeks=4",
        "/customers/c_005/tools/account_reporting?as_of=2026-11-10",
        "/customers/c_012/tools/balance_vs_txn?start=2026-11-03&end=2026-11-10",
        "/customers/c_007/tools/prior_year_compare?start=2026-11-03&end=2026-11-10",
    ]
    for url in urls:
        body = client.get(url, headers=auth_headers).json()
        ids = [e["id"] for e in body["evidence"]]
        assert len(set(ids)) == len(ids), f"duplicate evidence id in {url}: {ids}"
