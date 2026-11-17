"""Stand-ins for the two specialists until the real ones can be called (KAN-36).

They answer from data/shared_customers.yaml, the file the Bank and Card data is
generated from. So every test customer is covered and both sides tell the same
story. A stand-in does not read the question or the context. It reads the topic
asked about and the focus (an account, a record, the dates), and returns its
side's facts on that topic. With no topic it returns every fact its side holds
about the customer, as it did before requests had topics. It reads the snapshot,
the events of the week and the baselines, never the customer's story or the
expected answer.

A stand-in measures and never judges: it works out a share of the fall, a ratio
against the usual, a count of days, and leaves every threshold to the guidelines.
So it imports nothing from the graph or the policy.

The file sums a week up in a few lines. What it does not list is taken to be
everyday business with no large payment in it, unless the file says the records
do not reconcile. The stand-in says so in words, as a specialist with the whole
ledger would: left to work it out, a model takes the difference for a hole in the books.
Only the reconciliation topic says it, and a request with no topic, as before.

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

BANK_TOPICS = ("transactions", "income", "payee_history", "accounts", "reconciliation", "prior_year")
CARD_TOPICS = ("repayments", "paying_account", "card_use")
# The topics about the week the events happened in. The others ignore the dates asked for, except the
# accounts topic on what went to an account that is not linked, which is a fact about the week too.
_WEEK_TOPICS = ("transactions", "reconciliation")
# Said instead when the dates asked about are not that week.
_ONLY_THE_WEEK = "The records held cover {} to {} only."

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
    if named := [name for name in os.environ.get("SHARED_CUSTOMERS_FILE", "").split(os.pathsep) if name]:
        return tuple(Path(name) for name in named)
    # The repository root when run from a checkout, / in the image.
    for folder in Path(__file__).resolve().parents:
        if (found := folder / "data" / "shared_customers.yaml").exists():
            return (found,)
    raise FileNotFoundError("data/shared_customers.yaml was not found above app/agents")


def _data() -> dict:
    return _read(_data_files())


def customer_ids() -> frozenset[str]:
    """The customers the data files hold: the only ones the stand-ins have records for."""
    return frozenset(_data()["customers"])


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
    today = date.fromisoformat(clock["signal_date"])
    return {
        "today": today,
        # The week the events in the files happened in.
        "week": (today - timedelta(days=clock["signal_window_days"]), today),
        "weeks": clock["trailing_window_weeks"],
        "customers": customers,
        "unreconciled": unreconciled,
    }


# Money and shares are printed as the Orchestrator shows them to a model, so that a figure in a
# sentence reads the same as the field of the record it rests on.


def _usd(cents: int) -> str:
    """Whole dollars, with cents only when they are not zero."""
    dollars, rest = divmod(abs(cents), 100)
    return f"{'-' if cents < 0 else ''}${dollars:,}" + (f".{rest:02d}" if rest else "")


def _bp(part: int, whole: int) -> int | None:
    """part as a share of whole, in basis points rounded half up. None when there is no whole."""
    return (10000 * part + whole // 2) // whole if whole > 0 else None


def _pct(bp: int) -> str:
    """Whole percent, rounded half up. A share above nothing and under half a percent is "under 1%"."""
    if bp < 0:
        return "-" + _pct(-bp)
    return "under 1%" if 0 < bp < 50 else f"{(bp + 50) // 100}%"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


class _Answer:
    """Collects findings and returns the records they cite.

    `records` holds every record the side has about the customer, id to (kind, data), in the
    order of the full lists. So a record has the same id and content whichever topic returns
    it, and an answer returns exactly the records its findings stand on, in that order.
    """

    def __init__(self, agent: str, records: dict[str, tuple[str, dict]]):
        self.agent = agent
        self.records = records
        self.findings: list[Finding] = []
        self.unknown: list[str] = []

    def find(self, claim: str, *evidence_ids: str, confidence: str = "high") -> None:
        self.findings.append(Finding(claim=claim, confidence=confidence, evidence_ids=list(evidence_ids)))

    def response(self) -> InvestigateResponse:
        cited = {evidence_id for finding in self.findings for evidence_id in finding.evidence_ids}
        evidence = [Evidence(id=id, kind=kind, data=data) for id, (kind, data) in self.records.items() if id in cited]
        return InvestigateResponse(
            agent=self.agent, findings=self.findings, evidence=evidence, could_not_determine=self.unknown
        )


def _cannot(agent: str, reason: str) -> InvestigateResponse:
    return InvestigateResponse(agent=agent, findings=[], evidence=[], could_not_determine=[reason])


def cash_flow(request: InvestigateRequest) -> InvestigateResponse:
    """The bank side's facts on the topic asked, or everything it holds when no topic is given."""
    data = _data()
    customer = data["customers"].get(request.customer_id)
    if customer is None:
        return _cannot("cash_flow", f"There is no data for {request.customer_id}.")
    if request.topic is not None and request.topic not in BANK_TOPICS:
        return _cannot("cash_flow", f"The bank side has no such topic. It has: {', '.join(BANK_TOPICS)}.")
    if request.topic in _WEEK_TOPICS and (request.window_start, request.window_end) != data["week"]:
        return _cannot("cash_flow", _ONLY_THE_WEEK.format(*data["week"]))
    bank = _Bank(data, customer, request)
    # Each topic is the method of that name.
    getattr(bank, request.topic or "everything")()
    return bank.answer.response()


class _Bank:
    """The bank side's records of one customer, and what it says on each topic."""

    def __init__(self, data: dict, customer: dict, request: InvestigateRequest):
        cid, snapshot, baseline = customer["id"], customer["snapshot"]["bank"], customer["baselines"]["bank"]
        self.request, self.baseline, self.weeks, self.week = request, baseline, data["weeks"], data["week"]
        self.unreconciled = cid in data["unreconciled"]
        self.before, self.now = snapshot["cash_week_ago_cents"], snapshot["cash_now_cents"]
        self.fall = fall = self.before - self.now
        accounts = snapshot["linked_accounts"]
        self.linked = [account["id"] for account in accounts]
        self.stale = [account["id"] for account in accounts if account["days_stale"] or not account["active"]]
        # Shares of the fall are worked out only when there is a fall and every linked account reports.
        shares = fall > 0 and not self.stale

        records = self.records = {}
        self.balance = f"bal_{cid}"
        records[self.balance] = (
            "daily_balance",
            {"cash_week_ago_cents": self.before, "cash_now_cents": self.now, "change_cents": self.now - self.before},
        )
        for account in accounts:
            last_synced = data["today"] - timedelta(days=account["days_stale"])
            records[account["id"]] = (
                "linked_account",
                {
                    "active": account["active"],
                    "days_stale": account["days_stale"],
                    "last_synced": last_synced.isoformat(),
                    "is_stale": account["id"] in self.stale,
                },
            )

        self.events = {}
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
            if shares and not incoming:
                details["share_of_fall_bp"] = _bp(event["amount_cents"], fall)
            txn = f"txn_{cid}_{number:02d}"
            self.events[txn] = event
            records[txn] = ("bank_transaction", details)
        self.outflows = [txn for txn, event in self.events.items() if event["category"] != "income"]
        self.listed = listed = sum(self.events[txn]["amount_cents"] for txn in self.outflows)

        self.payments, self.books = f"agg_{cid}_payments", f"agg_{cid}_reconciliation"
        if shares:
            not_listed = max(0, fall - listed)
            summary = {
                "listed_count": len(self.outflows),
                "listed_out_cents": listed,
                "listed_share_bp": _bp(listed, fall),
                "not_listed_cents": not_listed,
                "not_listed_share_bp": _bp(not_listed, fall),
            }
            # Listed income is set out on its own, never netted against the payments.
            if income := [event["amount_cents"] for event in self.events.values() if event["category"] == "income"]:
                summary["listed_in_cents"] = sum(income)
            by_category = {}
            for txn in self.outflows:
                category = by_category.setdefault(self.events[txn]["category"], {"count": 0, "out_cents": 0})
                category["count"] += 1
                category["out_cents"] += self.events[txn]["amount_cents"]
            if payroll_and_tax := [by_category[name]["out_cents"] for name in ("payroll", "tax") if name in by_category]:
                summary["payroll_and_tax_cents"] = sum(payroll_and_tax)
                summary["payroll_and_tax_share_bp"] = _bp(sum(payroll_and_tax), fall)
            for category in by_category.values():
                category["share_bp"] = _bp(category["out_cents"], fall)
            summary["by_category"] = by_category
            records[self.payments] = ("payment_summary", summary)

            books = {"balance_change_cents": -fall, "transactions_total_cents": -fall, "reconciled": True}
            if self.unreconciled:
                unmatched = fall - listed
                books = {
                    "balance_change_cents": -fall,
                    "transactions_total_cents": -listed,
                    "reconciled": False,
                    "unmatched_cents": unmatched,
                    "unmatched_share_bp": _bp(unmatched, fall),
                }
            records[self.books] = ("balance_reconciliation", books)

        self.inflows = f"agg_{cid}_inflows"
        inflows = {
            key: value for key, value in baseline.items() if key.startswith("weekly_inflow") or key == "income_trend"
        }
        inflows["weeks"] = data["weeks"]
        # The level this week's inflows are set against, when the file holds one.
        levels = (("weekly_inflow_3mo_ago_cents", "three_months_ago"), ("weekly_inflow_typical_cents", "usual"))
        for key, level in levels:
            if key in baseline:
                inflows["compared_with"] = level
                if (share := _bp(baseline["weekly_inflow_cents"], baseline[key])) is not None:
                    inflows["vs_compared_bp"] = share
                break
        records[self.inflows] = ("weekly_inflow_series", inflows)

        self.last_year = f"agg_{cid}_prior_year"
        if last_year := baseline.get("prior_year_same_two_weeks"):
            start, end = data["week"]
            # The file holds two weeks of last year. This window is one.
            prior = {**last_year, "period_days": 14, "window_days": (end - start).days, "window_out_cents": listed}
            if (share := _bp(listed, last_year["outflow_cents"])) is not None:
                prior["window_vs_prior_out_bp"] = share
            records[self.last_year] = ("prior_year_comparison", prior)

        self.history, self.with_history = f"agg_{cid}_payee_history", None
        if payee := baseline.get("payee_history_hint"):
            history = {key: value for key, value in payee.items() if key != "counterparty"}
            # The history is of the payee of the first payment to a vendor paid before.
            if paid := next((txn for txn in self.outflows if self.events[txn]["category"] == "vendor_known"), None):
                self.with_history, amount = paid, self.events[paid]["amount_cents"]
                top = payee.get("typical_amount_cents", payee.get("typical_amount_cents_max"))
                history |= {"about": paid, "amount_cents": amount, "usual_top_cents": top}
                if (share := _bp(amount, top)) is not None:
                    history["vs_usual_bp"] = share
            records[self.history] = ("payee_history", history)

        self.answer = _Answer("cash_flow", records)

    def everything(self) -> None:
        """Every fact the side holds, in the words and order of the time before topics."""
        self._say_fall()
        self._say_accounts()
        self._say_events()
        if self.payments in self.records:
            if not self.unreconciled:
                if self.outflows:
                    self.answer.find(
                        f"Payments listed for the window: {len(self.outflows)}, {_usd(self.listed)} in all. "
                        "No other large payment went out.",
                        self.payments,
                        *self.outflows,
                        confidence="medium",
                    )
                else:
                    self.answer.find("No large payment went out in the window.", self.balance, confidence="medium")
            self._say_share()
            self._say_books()
        self.income()
        if self.last_year in self.records:
            self._say_last_year()
        if self.history in self.records:
            self._say_history()

    def transactions(self) -> None:
        self._say_fall()
        self._say_events()
        if self.payments in self.records:
            if self.outflows:
                self.answer.find(
                    f"Payments listed for the window: {len(self.outflows)}, {_usd(self.listed)} in all.",
                    self.payments,
                    *self.outflows,
                )
            self._say_share()
        # Whether the books agree is for the reconciliation topic. Here only what a silent account hides.
        for account in self.stale:
            self.answer.unknown.append(f"Which payments left through {account} since it stopped reporting")

    def income(self) -> None:
        baseline, weekly = self.baseline, self.baseline["weekly_inflow_cents"]
        self.answer.find(
            {
                "steady": f"Weekly inflows are steady at about {_usd(weekly)} over the last {self.weeks} weeks.",
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
            self.inflows,
        )

    def payee_history(self) -> None:
        txn = self.request.record_id
        event = self.events.get(txn)
        if event is None:
            self.answer.unknown.append(
                "There is no such payment to look up. Pass record_id, the id of one transaction."
            )
        elif event["category"] == "vendor_new":
            self.answer.find(f"The payee of {txn} was never paid before.", txn)
        elif event["category"] != "vendor_known":
            self.answer.unknown.append("Payee history is kept for vendors only.")
        elif txn == self.with_history:
            self._say_history()
        else:
            self.answer.unknown.append("The payment history of that payee is not in the data.")

    def accounts(self) -> None:
        account = self.request.account_id
        if account is None:
            self._say_accounts()
        elif account in self.linked:
            self.answer.find(f"{account} is one of the linked accounts.", account)
            if account in self.stale:
                self._say_stale(account)
        else:
            self.answer.find(f"{account} is not one of the linked accounts.", *self.linked)
            if (self.request.window_start, self.request.window_end) != self.week:
                self.answer.unknown.append(_ONLY_THE_WEEK.format(*self.week))
                return
            sent = [txn for txn in self.outflows if self.events[txn].get("to_account") == account]
            for txn in sent:
                self.answer.find(f"{_usd(self.events[txn]['amount_cents'])} went to it in the window.", txn)
            if not sent:
                self.answer.find("No payment listed for the window went to it.", *self.linked)

    def reconciliation(self) -> None:
        if self.stale:
            for account in self.stale:
                self.answer.unknown.append(f"Whether balances and transactions agree: {account} is not reporting.")
        elif self.fall <= 0:
            self.answer.unknown.append("Linked cash did not fall in the window, so there is no fall to reconcile.")
        else:
            self._say_books()

    def prior_year(self) -> None:
        if self.last_year in self.records:
            self._say_last_year()
        else:
            # No data, which says nothing about whether the same fall happened.
            self.answer.unknown.append("Figures for the same weeks last year are not in the data.")

    def _say_fall(self) -> None:
        moved = "fell" if self.fall > 0 else "rose" if self.fall < 0 else "changed"
        self.answer.find(
            f"Linked cash {moved} by {_usd(abs(self.fall))} in the week, from {_usd(self.before)} to {_usd(self.now)}.",
            self.balance,
        )

    def _say_accounts(self) -> None:
        for account in self.stale:
            self._say_stale(account)
        self.answer.find(f"The linked accounts are: {', '.join(self.linked)}.", *self.linked)

    def _say_stale(self, account: str) -> None:
        days = self.records[account][1]["days_stale"]
        silent = f"has not reported for {_count(days, 'day')}" if days else "is not active"
        self.answer.find(f"Account {account} {silent}. Its balance since then is not in the figures.", account)
        self.answer.unknown.append(f"What {account} holds since it stopped reporting")

    def _say_events(self) -> None:
        for txn, event in self.events.items():
            amount = _usd(event["amount_cents"])
            if event["category"] == "income":
                self.answer.find(f"Income of {amount} arrived in the window.", txn)
            else:
                claim = _OUTFLOWS.get(event["category"], _OUTFLOWS["other"]).format(amount=amount)
                self.answer.find(claim + (f" It went to {event['to_account']}." if "to_account" in event else ""), txn)

    def _say_share(self) -> None:
        summary = self.records[self.payments][1]
        if summary["listed_count"]:
            claim = (
                f"The listed payments are {_pct(summary['listed_share_bp'])} of the fall. "
                f"The other {_usd(summary['not_listed_cents'])} is not in this list."
            )
        else:
            claim = (
                f"No payment is listed for the window, so the whole fall of {_usd(summary['not_listed_cents'])} "
                "is not in this list."
            )
        self.answer.find(claim, self.payments)

    def _say_books(self) -> None:
        books = self.records[self.books][1]
        if self.unreconciled:
            other = _usd(books["unmatched_cents"])
            self.answer.find(
                f"The transactions in the window add up to {_usd(self.listed)}, but the balance fell by {_usd(self.fall)}. "
                f"The other {other} matches no transaction: balances and transactions do not reconcile.",
                self.books,
                self.balance,
            )
            self.answer.unknown.append(f"Where the other {other} went")
        else:
            self.answer.find(
                "Balances and transactions reconcile: the transactions on record add up to the whole change. "
                "Apart from any payment listed here, they are everyday payments and receipts, none of them large.",
                self.books,
                self.balance,
            )

    def _say_last_year(self) -> None:
        last_year = self.records[self.last_year][1]
        self.answer.find(
            f"In the same two weeks last year, {_usd(last_year['outflow_cents'])} went out "
            f"and {_usd(last_year['inflow_cents'])} came in.",
            self.last_year,
        )

    def _say_history(self) -> None:
        payee, history = self.baseline["payee_history_hint"], self.records[self.history][1]
        usual = (
            _usd(payee["typical_amount_cents"])
            if "typical_amount_cents" in payee
            else f"{_usd(payee['typical_amount_cents_min'])} to {_usd(payee['typical_amount_cents_max'])}"
        )
        self.answer.find(
            f"The payee of that payment was paid {_count(payee['n_prior_payments'], 'time')} before over "
            f"{payee['first_seen_months_ago']} months, usually {usual}.",
            self.history,
        )
        if "vs_usual_bp" in history:
            self.answer.find(
                f"This payment of {_usd(history['amount_cents'])} is {_pct(history['vs_usual_bp'])} of the most it "
                f"was usually paid ({_usd(history['usual_top_cents'])}).",
                self.history,
                history["about"],
            )


def credit_behavior(request: InvestigateRequest) -> InvestigateResponse:
    """The card side's facts on the topic asked, or everything it holds when no topic is given."""
    data = _data()
    customer = data["customers"].get(request.customer_id)
    if customer is None:
        return _cannot("credit_behavior", f"There is no data for {request.customer_id}.")
    if request.topic is not None and request.topic not in CARD_TOPICS:
        return _cannot("credit_behavior", f"The card side has no such topic. It has: {', '.join(CARD_TOPICS)}.")
    cid, topic = customer["id"], request.topic
    snapshot, baseline = customer["snapshot"]["card"], customer["baselines"]["card"]
    events = {event["kind"]: event for event in customer["signal_window"]["card_events"]}
    if unknown := set(events) - {"failed_repayment", "late_repayment", "funding_account_change"}:
        raise ValueError(f"the stand-in does not know the card event {sorted(unknown)}")

    # Every record the side holds about the customer, as on the bank side.
    card, funding, spend = f"bal_card_{cid}", f"rep_{cid}_funding", f"agg_{cid}_card_spend"
    owed = round(snapshot["credit_limit_cents"] * snapshot["utilization_pct"])
    balance = {
        "credit_limit_cents": snapshot["credit_limit_cents"],
        "balance_cents": owed,
        "used_share_bp": round(10000 * snapshot["utilization_pct"]),
    }
    if "utilization_trend" in baseline:
        balance["utilization_trend"] = baseline["utilization_trend"]
    records = {card: ("card_balance", balance)}

    statuses = snapshot["last_3_repayments"]
    latest_with = {status: number for number, status in enumerate(statuses, 1)}
    repayments = []
    for number, status in enumerate(statuses, 1):
        details = {"sequence": number, "of": len(statuses), "status": status}
        if latest_with[status] == number and (trouble := events.get(f"{status}_repayment")):
            details["amount_cents"] = trouble["amount_cents"]
            # Whether a failed one was paid afterwards, where the file says.
            details |= {key: trouble[key] for key in ("repaid_after_days", "still_unpaid") if key in trouble}
        if status == "failed":
            # The same in one field, with "not_recorded" where the file does not say.
            unpaid = details.get("still_unpaid") is True
            details["paid_since"] = "yes" if "repaid_after_days" in details else "no" if unpaid else "not_recorded"
        repayments.append(f"rep_{cid}_{number}")
        records[repayments[-1]] = ("card_repayment", details)

    change = events.get("funding_account_change")
    records[funding] = (
        "funding_account_change",
        {"from": change["from"], "to": change["to"], "changed": True} if change else {"changed": False},
    )
    records[spend] = ("card_spend_series", {"monthly_card_spend_cents": baseline["monthly_card_spend_cents"]})

    answer = _Answer("credit_behavior", records)
    if topic in (None, "card_use"):
        trend = f" Utilization is {baseline['utilization_trend']}." if "utilization_trend" in baseline else ""
        answer.find(
            f"The card balance is {_usd(owed)}, {_pct(balance['used_share_bp'])} of the "
            f"{_usd(snapshot['credit_limit_cents'])} limit.{trend}",
            card,
        )
    if topic in (None, "repayments"):
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
    if change and topic in (None, "paying_account"):
        # Whether the new account is one the bank side can see is for the bank side to say.
        answer.find(
            f"During the window the account that pays the card changed from {change['from']} to {change['to']}.",
            funding,
        )
    elif topic == "paying_account":
        answer.find("The account that pays the card did not change during the window.", funding)
    if topic in (None, "card_use"):
        answer.find(f"Card spend runs at about {_usd(baseline['monthly_card_spend_cents'])} a month.", spend)
    return answer.response()
