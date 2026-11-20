"""Findings code writes from the records, every figure formatted from the record it cites.

Two uses:
- hard facts: a linked account that is not reporting, balances and transactions that do not reconcile,
  weekly inflows that stopped or are shrinking, and a transfer to the customer's own account at another
  bank or a payment to a new payee, with its share of the fall. They are added to every answer whose
  records hold them, whatever the model wrote. Whether a share is large is left to the guidelines.
- no model, or a run that failed: the answer is written from the records alone (from_records).

Both cover only the records on the topic asked (tools.on_topic: its kinds in tools.TOPIC_KINDS, for the
payee_history topic the payment asked about when it is out of view, and for the accounts topic about one
account the payments that went to it); a record fetched in support of another topic's kind is returned as
evidence but not written about.

Amounts and shares are written with app.graph.facts.usd and .pct, as the case graph shows them, so every
finding here passes the figure check (guardrails.check). Nothing here repeats text a customer wrote: a
value goes into a sentence only if it is an id, a date or a number. A record that lacks a field a sentence
needs gets the shorter sentence, or none.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from app.agents.cash_flow.tools import OUT_OF_VIEW, on_topic
from app.graph.facts import pct, usd
from contracts import Finding

log = logging.getLogger(__name__)

_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OUTFLOWS = {
    "payroll": "A payroll run of {amount} went out in the window.",
    "tax": "{amount} was paid to a tax authority in the window.",
    "transfer_own_account": "{amount} was transferred to an account at another bank held in the customer's own name.",
    "vendor_known": "{amount} was paid to a vendor the customer has paid before.",
    "vendor_new": "{amount} was paid to a payee the customer has never paid before.",
    "transfer_other": "{amount} was transferred to an account that is not the customer's.",
    "operating": "{amount} was paid for everyday running costs in the window.",
}


def _cents(data: dict, key: str) -> int | None:
    value = data.get(key)
    return value if type(value) is int else None


def _id(value: Any) -> str | None:
    return value if isinstance(value, str) and _ID.match(value) else None


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def _found(claim: str, *records: dict, confidence: str = "high") -> Finding:
    return Finding(claim=claim, confidence=confidence, evidence_ids=list(dict.fromkeys(r["id"] for r in records)))


# ---------------------------------------------------------------------------
# one writer per kind
# ---------------------------------------------------------------------------


def daily_balance(record: dict) -> Finding | None:
    d = record["data"]
    before, after, change = _cents(d, "cash_week_ago_cents"), _cents(d, "cash_now_cents"), _cents(d, "change_cents")
    if before is None or after is None:
        return None
    moved = "fell" if after < before else "rose" if after > before else "did not change"
    by = f" by {usd(abs(change))}" if change is not None and change else ""
    return _found(f"Linked cash {moved}{by} in the window, from {usd(before)} to {usd(after)}.", record)


def bank_transaction(record: dict) -> Finding | None:
    d = record["data"]
    amount = _cents(d, "amount_cents")
    if amount is None:
        return None
    if amount > 0 or d.get("category") == "income":
        claim = f"Income of {usd(abs(amount))} arrived in the window."
    else:
        claim = _OUTFLOWS.get(d.get("category"), "{amount} went out in the window.").format(amount=usd(abs(amount)))
        if to := _id(d.get("to_account")):
            claim += f" It went to {to}."
        if (share := _cents(d, "share_of_fall_bp")) is not None:
            claim += f" That is {pct(share)} of the fall."
    return _found(claim, record)


def payment_summary(record: dict) -> Finding | None:
    d = record["data"]
    count, out, came_in = d.get("listed_count"), _cents(d, "listed_out_cents"), _cents(d, "listed_in_cents")
    if type(count) is not int or out is None:
        return None
    if count:
        claim = f"Payments listed for the window: {count}, {usd(out)} in all"
        share = _cents(d, "listed_share_bp")
        claim += f", {pct(share)} of the fall." if share is not None else "."
    else:
        claim = "No payment is listed for the window."
    # Bank lists every movement, so the payments exceed the fall by what came in over the same days.
    if came_in:
        claim += f" Income of {usd(came_in)} came in over the same days."
    return _found(claim, record)


def balance_reconciliation(record: dict) -> Finding | None:
    d = record["data"]
    reconciled = d.get("reconciled")
    if reconciled not in (True, False):
        return None
    change, total = _cents(d, "balance_change_cents"), _cents(d, "transactions_total_cents")
    figures = (f" the balance changed by {usd(change)} and the transactions add up to {usd(total)}"
               if change is not None and total is not None else "")
    if reconciled:
        return _found(f"Balances and transactions reconcile:{figures or ' they agree'}.", record)
    claim = f"Balances and transactions do not reconcile:{figures or ' they disagree'}."
    if (unmatched := _cents(d, "unmatched_cents")) is not None:
        share = _cents(d, "unmatched_share_bp")
        claim += f" {usd(unmatched)}" + (f" ({pct(share)} of the fall)" if share is not None else "")
        claim += " matches no transaction."
    return _found(claim, record)


def weekly_inflow_series(record: dict) -> Finding | None:
    d = record["data"]
    trend, weekly = d.get("income_trend"), _cents(d, "weekly_inflow_cents")
    if not isinstance(trend, str) or not _ID.match(trend):
        return None
    if weekly is None:
        return _found(f"The weekly inflow trend is {trend}.", record)
    level = {"three_months_ago": ("weekly_inflow_3mo_ago_cents", "three months ago"),
             "usual": ("weekly_inflow_typical_cents", "usually")}.get(d.get("compared_with"))
    against = ""
    if level and (base := _cents(d, level[0])) is not None:
        against = f", against {usd(base)} {level[1]}"
        if (share := _cents(d, "vs_compared_bp")) is not None:
            against += f" ({pct(share)} of it)"
    weeks = d.get("weeks")
    over = f" over the last {weeks} weeks" if type(weeks) is int and weeks > 1 else ""
    sentence = {
        "steady": f"Weekly inflows are steady at {usd(weekly)}{over}.",
        "shrinking": f"Weekly inflows are shrinking: {usd(weekly)} a week{against}.",
        "stopped": f"Weekly inflows have almost stopped: {usd(weekly)} a week{against}.",
        "too_short_to_tell": f"Inflows this week were {usd(weekly)}{against}. The history is too short to call a trend.",
    }.get(trend, f"Weekly inflows are {usd(weekly)} a week{against}; the trend is {trend}.")
    return _found(sentence, record, confidence="medium" if trend == "too_short_to_tell" else "high")


def linked_account(record: dict) -> Finding | None:
    d, account = record["data"], record["id"]
    synced = f" It last reported on {d['last_synced']}." if _DAY.match(str(d.get("last_synced", ""))) else ""
    days = d.get("days_stale")
    if d.get("is_stale") is True:
        if d.get("active") is False:
            silent = "is not active"
        elif type(days) is int and days > 0:
            silent = f"has not reported for {_days(days)}"
        else:
            silent = "has not reported"
        return _found(f"Account {account} {silent}.{synced} Its balance since then is not in the figures.", record)
    if d.get("is_stale") is False:
        return _found(f"Account {account} is linked and reporting.{synced}", record)
    return None


def prior_year_comparison(record: dict) -> Finding | None:
    d = record["data"]
    out, came_in = _cents(d, "outflow_cents"), _cents(d, "inflow_cents")
    if out is None or came_in is None:
        return None
    claim = f"In the same weeks last year, {usd(out)} went out and {usd(came_in)} came in."
    window, share = _cents(d, "window_out_cents"), _cents(d, "window_vs_prior_out_bp")
    if window is not None and share is not None:
        claim += f" The payments listed for this window, {usd(window)}, are {pct(share)} of what went out then."
    return _found(claim, record)


def payee_history(record: dict, records: dict[str, dict]) -> Finding | None:
    d = record["data"]
    count, months = d.get("n_prior_payments"), d.get("first_seen_months_ago")
    if type(count) is not int:
        return None
    about = _id(d.get("about"))
    payment = f"The payee of {about}" if about else "The payee"
    claim = f"{payment} was paid {count} time{'s' if count != 1 else ''} before"
    if type(months) is int:
        claim += f" over {months} months"
    low, high, usual = (_cents(d, k) for k in ("typical_amount_cents_min", "typical_amount_cents_max",
                                                "typical_amount_cents"))
    if usual is not None:
        claim += f", usually {usd(usual)}"
    elif low is not None and high is not None:
        claim += f", usually {usd(low)} to {usd(high)}"
    claim += "."
    amount, share, top = _cents(d, "amount_cents"), _cents(d, "vs_usual_bp"), _cents(d, "usual_top_cents")
    if amount is not None and share is not None and top is not None:
        claim += f" This payment of {usd(amount)} is {pct(share)} of the most it was usually paid ({usd(top)})."
    cited = [record] + ([records[about]] if about in records else [])
    return _found(claim, *cited)


_WRITERS: dict[str, Callable[[dict], Finding | None]] = {
    "daily_balance": daily_balance,
    "bank_transaction": bank_transaction,
    "payment_summary": payment_summary,
    "balance_reconciliation": balance_reconciliation,
    "weekly_inflow_series": weekly_inflow_series,
    "linked_account": linked_account,
    "prior_year_comparison": prior_year_comparison,
}

# What a hard fact says when its record lacks the fields a longer sentence needs. No figures.
_SHORT = {
    "linked_account": "Account {id} is not reporting.",
    "balance_reconciliation": "Balances and transactions do not reconcile.",
    "weekly_inflow_series": "Weekly inflows have stopped or are shrinking.",
    "bank_transaction": "A large part of the fall went to an account out of view or to a new payee.",
}


def write(record: dict, records: dict[str, dict]) -> Finding | None:
    """The finding code writes for one record, or None. Never raises: a record of an odd shape gets none."""
    try:
        if record["kind"] == "payee_history":
            return payee_history(record, records)
        writer = _WRITERS.get(record["kind"])
        return writer(record) if writer else None
    except Exception as error:  # noqa: BLE001 - a record Bank shaped oddly must not cost the answer
        # Its kind only: a ValidationError of the Finding quotes the claim, written from Bank's record.
        log.warning("cash_flow could not write a finding for %s: %s", record.get("id"), type(error).__name__)
        return None


# ---------------------------------------------------------------------------
# hard facts and the answer from the records alone
# ---------------------------------------------------------------------------


def is_hard_fact(record: dict) -> bool:
    d, kind = record["data"], record["kind"]
    if kind == "linked_account":
        return d.get("is_stale") is True
    if kind == "balance_reconciliation":
        return d.get("reconciled") is False
    if kind == "weekly_inflow_series":
        return d.get("income_trend") in ("stopped", "shrinking")
    if kind == "bank_transaction":
        # Every one, with its share of the fall stated: whether the share is large is the guidelines' call
        # (a specialist measures and never judges, so nothing here reads app.policy).
        return d.get("category") in OUT_OF_VIEW
    return False


def hard_facts(records: dict[str, dict], request: dict[str, Any]) -> list[Finding]:
    """A finding for every record on the request's topic an answer must not pass over, in the order the
    records came."""
    out = []
    for record in records.values():
        if on_topic(record, request) and is_hard_fact(record):
            finding = write(record, records) or _found(_SHORT[record["kind"]].format(id=record["id"]), record)
            out.append(finding)
    return out


def from_records(records: dict[str, dict], request: dict[str, Any]) -> list[Finding]:
    """What code alone can say from the records fetched, one finding per record of the topic's kinds."""
    account = request.get("account_id")
    if request.get("topic") == "accounts" and account:
        return _about_account(account, records)
    return [finding for record in records.values()
            if on_topic(record, request) and (finding := write(record, records)) is not None]


def _about_account(account: str, records: dict[str, dict]) -> list[Finding]:
    linked = [r for r in records.values() if r["kind"] == "linked_account"]
    out = [f for r in linked if (f := linked_account(r)) is not None]
    if not linked:
        return out
    if account in records and records[account]["kind"] == "linked_account":
        return [_found(f"{account} is one of the linked accounts.", records[account]), *out]
    out.insert(0, _found(f"{account} is not one of the linked accounts.", *linked))
    sent = [r for r in records.values() if r["kind"] == "bank_transaction" and r["data"].get("to_account") == account]
    out += [f for r in sent if (f := bank_transaction(r)) is not None]
    # The linked accounts came back, so Bank answered account_reporting for this id (the topic's one preflight
    # call; the model never starts when it fails), and that answer holds every payment of the review window to
    # it: none among the records is none sent.
    if not sent:
        out.append(_found("No payment listed for the window went to it.", *linked))
    return out
