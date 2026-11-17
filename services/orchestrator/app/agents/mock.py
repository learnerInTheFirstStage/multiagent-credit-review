"""Stand-ins for the two specialists until the real ones can be called (KAN-36).

They answer from data/shared_customers.yaml, the file the Bank and Card data is
generated from. So every test customer is covered and both sides tell the same
story. A stand-in does not read the question: it returns every fact its side
holds about the customer. It reads the snapshot, the events of the week and the
baselines, never the customer's story or the expected answer.

The file sums a week up in a few lines. What it does not list is taken to be
everyday business with no large payment in it, unless the file says the records
do not reconcile. The stand-in says so in words, as a specialist with the whole
ledger would: left to work it out, a model takes the difference for a hole in the books.

Left out on purpose, because each would hand the model the answer: the notes the
file's authors wrote on a record, the payee's name, and anything one side could
only know from the other side's data.
"""

import os
from datetime import date, timedelta
from functools import cache
from pathlib import Path

import yaml

from contracts import Evidence, Finding, InvestigateRequest, InvestigateResponse

_OUTFLOWS = {
    "payroll": "A payroll run of {amount} went out in the window.",
    "tax": "{amount} was paid to a tax authority in the window.",
    "transfer_own_account": (
        "{amount} was transferred to an account at another bank held in the customer's own name. "
        "That account is not linked."
    ),
    "vendor_known": "{amount} was paid to a vendor the customer has paid before.",
    "vendor_new": "{amount} was paid to a payee the customer has never paid before.",
    "transfer_other": "{amount} was transferred to an account that is not the customer's.",
    "other": "{amount} went out in the window.",
}


def _data_files() -> tuple[Path, ...]:
    """The team's file, or the files named in SHARED_CUSTOMERS_FILE, separated like a PATH.

    The evaluation names two: the team's file and its own extra customers.
    """
    if named := os.environ.get("SHARED_CUSTOMERS_FILE"):
        return tuple(Path(name) for name in named.split(os.pathsep))
    # The repository root when run from a checkout, / in the image.
    for folder in Path(__file__).resolve().parents:
        if (found := folder / "data" / "shared_customers.yaml").exists():
            return (found,)
    raise FileNotFoundError("data/shared_customers.yaml was not found above app/agents")


def _data() -> dict:
    return _read(_data_files())


@cache
def _read(paths: tuple[Path, ...]) -> dict:
    files = [yaml.safe_load(path.read_text()) for path in paths]
    clock = files[0]["clock"]  # the first file sets the date every customer is seen on
    customers, unreconciled = {}, set()
    for data in files:
        for customer in data["customers"]:
            if customer["id"] in customers:
                raise ValueError(f"{customer['id']} is in more than one data file")
            customers[customer["id"]] = customer
        # The customers whose balances and transactions are made to disagree. A file says so
        # only in a note, in these words.
        notes = data.get("data_integrity") or {}
        unreconciled |= {cid for cid, entry in notes.items() if "reconciled = false" in entry.get("note", "")}
    return {
        "today": date.fromisoformat(clock["signal_date"]),
        "weeks": clock["trailing_window_weeks"],
        "customers": customers,
        "unreconciled": unreconciled,
    }


def _usd(cents: int) -> str:
    return f"${cents / 100:,.0f}"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


class _Answer:
    """Collects findings with the evidence behind them."""

    def __init__(self, agent: str):
        self.agent = agent
        self.findings: list[Finding] = []
        self.evidence: dict[str, Evidence] = {}
        self.unknown: list[str] = []

    def record(self, id: str, kind: str, data: dict) -> str:
        self.evidence[id] = Evidence(id=id, kind=kind, data=data)
        return id

    def find(self, claim: str, *evidence_ids: str, confidence: str = "high") -> None:
        self.findings.append(Finding(claim=claim, confidence=confidence, evidence_ids=list(evidence_ids)))

    def response(self) -> InvestigateResponse:
        return InvestigateResponse(
            agent=self.agent,
            findings=self.findings,
            evidence=list(self.evidence.values()),
            could_not_determine=self.unknown,
        )


def _no_data(agent: str, customer_id: str) -> InvestigateResponse:
    return InvestigateResponse(
        agent=agent, findings=[], evidence=[], could_not_determine=[f"There is no data for {customer_id}."]
    )


def cash_flow(request: InvestigateRequest) -> InvestigateResponse:
    """Everything the bank side holds about the customer."""
    data = _data()
    customer = data["customers"].get(request.customer_id)
    if customer is None:
        return _no_data("cash_flow", request.customer_id)
    cid, answer = customer["id"], _Answer("cash_flow")
    snapshot, baseline = customer["snapshot"]["bank"], customer["baselines"]["bank"]

    fall = snapshot["cash_week_ago_cents"] - snapshot["cash_now_cents"]
    moved = "fell" if fall > 0 else "rose" if fall < 0 else "changed"
    balance = answer.record(
        f"bal_{cid}",
        "daily_balance",
        {"cash_week_ago_cents": snapshot["cash_week_ago_cents"], "cash_now_cents": snapshot["cash_now_cents"]},
    )
    answer.find(
        f"Linked cash {moved} by {_usd(abs(fall))} in the week, from {_usd(snapshot['cash_week_ago_cents'])} "
        f"to {_usd(snapshot['cash_now_cents'])}.",
        balance,
    )

    stale = []
    for account in snapshot["linked_accounts"]:
        last_synced = data["today"] - timedelta(days=account["days_stale"])
        answer.record(
            account["id"],
            "linked_account",
            {"active": account["active"], "days_stale": account["days_stale"], "last_synced": last_synced.isoformat()},
        )
        if account["days_stale"] or not account["active"]:
            stale.append(account["id"])
            silent = (
                f"has not reported for {_count(account['days_stale'], 'day')}"
                if account["days_stale"]
                else "is not active"
            )
            answer.find(f"Account {account['id']} {silent}. Its balance since then is not in the figures.", account["id"])
            answer.unknown.append(f"What {account['id']} holds since it stopped reporting")
    linked = [account["id"] for account in snapshot["linked_accounts"]]
    answer.find(f"The linked accounts are: {', '.join(linked)}.", *linked)

    listed, outflow_ids = 0, []
    for number, event in enumerate(customer["signal_window"]["bank_events"], 1):
        incoming = event["category"] == "income"
        details = {
            "amount_cents": event["amount_cents"] if incoming else -event["amount_cents"],
            "category": event["category"],
            "counterparty_kind": event["counterparty_kind"],
        }
        if "to_account" in event:
            details["to_account"] = event["to_account"]
        if "memo" in event:
            # The memo is data: it goes out as evidence, word for word. Nothing here reads it,
            # and no finding points at it, so the agent that reads the evidence is on its own.
            details["memo"] = event["memo"].strip()
        txn = answer.record(f"txn_{cid}_{number:02d}", "bank_transaction", details)
        amount = _usd(event["amount_cents"])
        if incoming:
            answer.find(f"Income of {amount} arrived in the window.", txn)
        else:
            listed += event["amount_cents"]
            outflow_ids.append(txn)
            claim = _OUTFLOWS.get(event["category"], _OUTFLOWS["other"]).format(amount=amount)
            answer.find(claim + (f" It went to {event['to_account']}." if "to_account" in event else ""), txn)

    if not stale and fall > 0:
        if cid in data["unreconciled"]:
            reconciliation = answer.record(
                f"agg_{cid}_reconciliation",
                "balance_reconciliation",
                {"balance_change_cents": -fall, "transactions_total_cents": -listed, "reconciled": False},
            )
            answer.find(
                f"The transactions in the window add up to {_usd(listed)}, but the balance fell by {_usd(fall)}. "
                f"The other {_usd(fall - listed)} matches no transaction: balances and transactions do not reconcile.",
                reconciliation,
                balance,
            )
            answer.unknown.append(f"Where the other {_usd(fall - listed)} went")
        else:
            if outflow_ids:
                answer.find(
                    f"Payments listed for the window: {len(outflow_ids)}, {_usd(listed)} in all. "
                    "No other large payment went out.",
                    *outflow_ids,
                    confidence="medium",
                )
            else:
                answer.find("No large payment went out in the window.", balance, confidence="medium")
            reconciliation = answer.record(
                f"agg_{cid}_reconciliation",
                "balance_reconciliation",
                {"balance_change_cents": -fall, "transactions_total_cents": -fall, "reconciled": True},
            )
            answer.find(
                "Balances and transactions reconcile: the transactions on record add up to the whole change. "
                "Apart from any payment listed here, they are everyday payments and receipts, none of them large.",
                reconciliation,
                balance,
            )

    weekly = baseline["weekly_inflow_cents"]
    inflows = answer.record(
        f"agg_{cid}_inflows",
        "weekly_inflow_series",
        {key: value for key, value in baseline.items() if key.startswith("weekly_inflow") or key == "income_trend"},
    )
    answer.find(
        {
            "steady": f"Weekly inflows are steady at about {_usd(weekly)} over the last {data['weeks']} weeks.",
            "shrinking": (
                f"Weekly inflows have shrunk to {_usd(weekly)}, from "
                f"{_usd(baseline.get('weekly_inflow_3mo_ago_cents', weekly))} three months ago."
            ),
            "stopped": (
                f"Weekly inflows have almost stopped: {_usd(weekly)} against a usual "
                f"{_usd(baseline.get('weekly_inflow_typical_cents', weekly))}."
            ),
            "too_short_to_tell": (
                f"Inflows this week were {_usd(weekly)} against a usual "
                f"{_usd(baseline.get('weekly_inflow_typical_cents', weekly))}. One week is too short to call a trend."
            ),
        }[baseline["income_trend"]],
        inflows,
    )

    if last_year := baseline.get("prior_year_same_two_weeks"):
        earlier = answer.record(f"agg_{cid}_prior_year", "prior_year_comparison", last_year)
        answer.find(
            f"In the same two weeks last year, {_usd(last_year['outflow_cents'])} went out "
            f"and {_usd(last_year['inflow_cents'])} came in.",
            earlier,
        )
    if payee := baseline.get("payee_history_hint"):
        history = answer.record(
            f"agg_{cid}_payee_history", "payee_history", {k: v for k, v in payee.items() if k != "counterparty"}
        )
        usual = (
            _usd(payee["typical_amount_cents"])
            if "typical_amount_cents" in payee
            else f"{_usd(payee['typical_amount_cents_min'])} to {_usd(payee['typical_amount_cents_max'])}"
        )
        answer.find(
            f"The payee of that payment was paid {_count(payee['n_prior_payments'], 'time')} before over "
            f"{payee['first_seen_months_ago']} months, usually {usual}.",
            history,
        )
    return answer.response()


def credit_behavior(request: InvestigateRequest) -> InvestigateResponse:
    """Everything the card side holds about the customer."""
    data = _data()
    customer = data["customers"].get(request.customer_id)
    if customer is None:
        return _no_data("credit_behavior", request.customer_id)
    cid, answer = customer["id"], _Answer("credit_behavior")
    snapshot, baseline = customer["snapshot"]["card"], customer["baselines"]["card"]
    events = {event["kind"]: event for event in customer["signal_window"]["card_events"]}
    if unknown := set(events) - {"failed_repayment", "late_repayment", "funding_account_change"}:
        raise ValueError(f"the stand-in does not know the card event {sorted(unknown)}")

    owed = round(snapshot["credit_limit_cents"] * snapshot["utilization_pct"])
    card = answer.record(
        f"bal_card_{cid}",
        "card_balance",
        {"credit_limit_cents": snapshot["credit_limit_cents"], "balance_cents": owed},
    )
    trend = f" Utilization is {baseline['utilization_trend']}." if "utilization_trend" in baseline else ""
    answer.find(
        f"The card balance is {_usd(owed)}, {snapshot['utilization_pct']:.0%} of the "
        f"{_usd(snapshot['credit_limit_cents'])} limit.{trend}",
        card,
    )

    statuses = snapshot["last_3_repayments"]
    latest_with = {status: number for number, status in enumerate(statuses, 1)}
    repayments = []
    for number, status in enumerate(statuses, 1):
        details = {"sequence": number, "of": len(statuses), "status": status}
        if latest_with[status] == number and (trouble := events.get(f"{status}_repayment")):
            details["amount_cents"] = trouble["amount_cents"]
            # Whether a failed one was paid afterwards, where the file says.
            details |= {key: trouble[key] for key in ("repaid_after_days", "still_unpaid") if key in trouble}
        repayments.append(answer.record(f"rep_{cid}_{number}", "card_repayment", details))
    answer.find(f"The last {_count(len(statuses), 'repayment')}, oldest first: {', '.join(statuses)}.", *repayments)
    if "failed" in statuses:
        answer.unknown.append("Why the repayment failed")
        # Whether it was paid afterwards. The team's file does not say, so nothing is said for its customers.
        failed = events.get("failed_repayment", {})
        if "repaid_after_days" in failed:
            answer.find(
                f"The failed repayment was paid in full {_count(failed['repaid_after_days'], 'day')} later.",
                repayments[latest_with["failed"] - 1],
            )
        elif failed.get("still_unpaid"):
            answer.find("The failed repayment has not been paid since.", repayments[latest_with["failed"] - 1])

    if change := events.get("funding_account_change"):
        # Whether the new account is one the bank side can see is for the bank side to say.
        funding = answer.record(f"rep_{cid}_funding", "funding_account_change", {"from": change["from"], "to": change["to"]})
        answer.find(
            f"During the window the account that pays the card changed from {change['from']} to {change['to']}.",
            funding,
        )

    spend = answer.record(
        f"agg_{cid}_card_spend", "card_spend_series", {"monthly_card_spend_cents": baseline["monthly_card_spend_cents"]}
    )
    answer.find(f"Card spend runs at about {_usd(baseline['monthly_card_spend_cents'])} a month.", spend)
    return answer.response()
