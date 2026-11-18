"""Findings code writes from the records, word for word from the numbers in them.

Two uses:
- hard facts: a failed or late repayment, or a change of the account that pays the card, must
  reach the Orchestrator. Code always adds its own finding for one, whatever the model wrote.
- no model configured: the agent answers from the topic's records alone (see agent.py).

Every figure is formatted from the record it cites, so these pass guardrails.check.
"""

from __future__ import annotations

from contracts import Evidence, Finding

from app.agents.card_service.guardrails import TOO_SHORT, EvidencePool
# The graph's own formats (whole percent, half up), so the graph's figure check reads what it shows.
from app.graph.facts import pct, usd


def _found(claim: str, *records: Evidence, confidence: str = "high") -> Finding:
    return Finding(claim=claim, confidence=confidence, evidence_ids=[r.id for r in records])


def repayment(record: Evidence) -> Finding | None:
    d = record.data
    if d.get("status") == "failed":
        how = f"the bank returned it ({d.get('ach_return_code')}, {d.get('return_reason')})"
        if d.get("returned_on"):
            how += f" on {d['returned_on']}"
        since = (f"It was paid {d['repaid_after_days']} days after it was returned."
                 if d.get("paid_since") == "yes" and "repaid_after_days" in d else "It has not been paid since.")
        return _found(f"The repayment due {d['due']} for {usd(d['amount_cents'])} failed: {how}. {since}", record)
    if d.get("status") == "late":
        late = f"{d['days_late']} days after its due date" if "days_late" in d else "after its due date"
        return _found(f"The repayment due {d['due']} for {usd(d['amount_cents'])} was paid late, {late}.", record)
    return None


def repayment_counts(summary: Evidence, repayments: list[Evidence]) -> Finding:
    d = summary.data
    return _found(
        f"Of the last {d['n']} repayments due (from {d.get('first_due')} to {d.get('last_due')}), "
        f"{d['paid']} were paid on time, {d['late']} late and {d['failed']} failed.",
        summary, *repayments,
    )


def paying_account(record: Evidence) -> Finding:
    d = record.data
    if d.get("changed"):
        when = f" on {d['changed_on']}" if d.get("changed_on") else ""
        return _found(f"The account that pays the card changed from {d['from']} to {d['to']}{when}.", record)
    # The record holds only {changed, from, to}: how many repayments were compared is in the tool's data.
    return _found(f"The account that pays the card, {d.get('to')}, did not change across the repayments compared.",
                  record)


def balance(record: Evidence) -> Finding:
    d = record.data
    claim = f"On {d['date']} the card balance was {usd(d['balance_cents'])}"
    if "used_share_bp" in d:  # Card leaves the share out when there is no limit to divide by
        claim += f", {pct(d['used_share_bp'])} of the {usd(d['credit_limit_cents'])} limit."
    elif "credit_limit_cents" in d:
        claim += f", against a {usd(d['credit_limit_cents'])} limit."
    else:
        claim += "."
    trend = d.get("utilization_trend")
    if trend == TOO_SHORT:
        claim += " The card's history is too short to tell whether that is usual for this customer."
        return _found(claim, record, confidence="medium")
    if "usual_share_bp" in d:
        claim += (f" On the same day of the three months before it averaged {pct(d['usual_share_bp'])}, "
                  f"so utilization is {trend}.")
    return _found(claim, record)


def spend(record: Evidence) -> Finding:
    d = record.data
    current = d["current_cycle"]
    claim = f"Card spend in the cycle {current['start']} to {current['end']} was {usd(current['spend_cents'])}"
    if not current.get("complete"):
        claim += f" so far ({usd(current['projected_cents'])} at this pace for the whole cycle)"
    if d.get("history") == TOO_SHORT:
        return _found(claim + ". There are too few closed cycles to tell what is usual.", record, confidence="medium")
    change = d.get("change_vs_usual_bp")
    direction = "above" if (change or 0) >= 0 else "below"
    claim += (f", against a usual {usd(d['monthly_card_spend_cents'])} a cycle"
              + (f", {pct(abs(change))} {direction} it." if change is not None else "."))
    if d.get("unusual") in ("up", "down"):
        claim += " That is unusual for this customer."
    return _found(claim, record)


def hard_facts(pool: EvidencePool) -> list[Evidence]:
    """The records an answer must not pass over."""
    trouble = [e for e in pool.of_kind("card_repayment") if e.data.get("status") in ("failed", "late")]
    moved = [e for e in pool.of_kind("funding_account_change") if e.data.get("changed") is True]
    return trouble + moved[:1]


def for_hard_fact(record: Evidence) -> Finding | None:
    return repayment(record) if record.kind == "card_repayment" else paying_account(record)


def from_records(pool: EvidencePool) -> list[Finding]:
    """What code alone can say from the records fetched, one finding per record that answers."""
    out: list[Finding] = []
    for record in pool.of_kind("card_balance")[:1]:  # the first is the day asked about
        out.append(balance(record))
    for record in pool.of_kind("card_spend_series")[:1]:
        out.append(spend(record))
    reps = pool.of_kind("card_repayment")
    for summary in pool.of_kind("repayment_summary")[:1]:
        listed = [r for r in reps if summary.data.get("first_due", "") <= r.data.get("due", "") <= summary.data.get("last_due", "")]
        out.append(repayment_counts(summary, listed))
    out += [f for r in reps if (f := repayment(r)) is not None]
    for record in pool.of_kind("funding_account_change")[:1]:
        out.append(paying_account(record))
    return out
