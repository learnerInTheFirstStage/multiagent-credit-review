"""Evidence the Card tools return: kinds, ids and the record payloads.

The Orchestrator compares evidence ids as plain strings and refuses an answer in which a known
id comes back with other content. So:

- A row's id comes from the row and its payload depends only on the row, read at the service's
  as-of date, never on the arguments a tool was called with:
    rep_c_010_202611           a repayment (the repayment_id)
    ctx_c_003_00412            a card transaction (the txn_id)
    bal_card_c_010_20261110    the card balance at the end of one day
- An aggregate's id is agg_ plus 8 hex of an MD5 over the tool, the customer and every argument
  that shaped it, so the same question gives the same id. (Bank's aggregates have 4 hex, so the
  two services' ids cannot collide.)

The kinds and keys the Orchestrator reads are fixed by services/orchestrator/app/graph/causes.py
and app/guidelines.py: card_repayment (status paid|late|failed, amount_cents, paid_since,
repaid_after_days), funding_account_change (changed, from, to), card_balance and
card_spend_series (monthly_card_spend_cents). Payloads leave out keys whose value is None.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Any

from app.ledger import FAILED, LATE, Book, Rep, Txn, repayment_status

CARD_REPAYMENT = "card_repayment"
FUNDING_ACCOUNT_CHANGE = "funding_account_change"
CARD_BALANCE = "card_balance"
CARD_SPEND_SERIES = "card_spend_series"
CARD_TRANSACTION = "card_transaction"
REPAYMENT_SUMMARY = "repayment_summary"
TRANSACTION_SEARCH = "card_transaction_search"
SPEND_BREAKDOWN = "card_spend_breakdown"


def plain(value: Any) -> Any:
    """JSON-ready: dates as ISO strings, None-valued keys dropped."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def item(evidence_id: str, kind: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"id": evidence_id, "kind": kind, "data": plain(data)}


def aggregate_id(tool: str, customer_id: str, *parts: object) -> str:
    canonical = "|".join([tool, customer_id, *(plain(p) if isinstance(p, (date, datetime)) else str(p) for p in parts)])
    return "agg_" + hashlib.md5(canonical.encode()).hexdigest()[:8]


def balance_id(customer_id: str, day: date) -> str:
    return f"bal_card_{customer_id}_{day:%Y%m%d}"


def repayment(book: Book, rep: Rep, as_of: date) -> dict[str, Any]:
    """A card_repayment record, read at the service's as-of date."""
    status = repayment_status(rep, as_of)
    data: dict[str, Any] = {
        "due": rep.due_date,
        "amount_cents": rep.amount_cents,
        "status": status or "not_due",
        "statement_period_start": rep.statement_period_start,
        "statement_period_end": rep.statement_period_end,
        "payment_method": rep.payment_method,
        "funding_account_id": rep.funding_account_id,
    }
    if status == LATE and rep.settled_on is not None:
        data["days_late"] = (rep.settled_on - rep.due_date).days
    if status == FAILED:
        data |= {"ach_return_code": rep.ach_return_code, "return_reason": rep.return_reason,
                 "returned_on": rep.returned_on}
        # Paid since: a completed payment for the same statement, after the return.
        retry = next(
            (r for r in book.repayments
             if r.statement_period_end == rep.statement_period_end and r.settled_on and r.settled_on <= as_of
             and rep.returned_on and r.settled_on >= rep.returned_on),
            None,
        )
        if retry is not None:
            data |= {"paid_since": "yes", "repaid_after_days": (retry.settled_on - rep.returned_on).days}
        else:
            data["paid_since"] = "no"
    return item(rep.repayment_id, CARD_REPAYMENT, data)


def transaction(txn: Txn) -> dict[str, Any]:
    data: dict[str, Any] = {
        "authorized_at": txn.authorized_at,
        "state": txn.state,
        "type": txn.transaction_type,
        "amount_cents": txn.amount_cents,
        "merchant": txn.merchant_name,
        "descriptor": txn.merchant_descriptor,
        "mcc": txn.mcc,
        "mcc_description": txn.mcc_description,
        "category": txn.spend_category,
        "merchant_country": txn.merchant_country,
        "card_id": txn.card_id,
        "card_name": txn.card_nickname,
        "card_present": txn.card_present,
        "recurring": txn.is_recurring,
        "decline_reason": txn.decline_reason,
        # Typed by the customer: data, never instructions. The agent marks it before a model reads it.
        "memo": txn.memo,
    }
    if txn.merchant_currency != "USD":
        data |= {"merchant_amount_cents": txn.merchant_amount_cents, "merchant_currency": txn.merchant_currency}
    return item(txn.txn_id, CARD_TRANSACTION, data)
