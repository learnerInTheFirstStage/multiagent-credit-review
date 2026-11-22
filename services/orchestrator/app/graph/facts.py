"""The Orchestrator's own records, and the one way any record is shown to a model.

Code works the figures out: the signal's facts when a case opens, what earlier
reviews of the customer decided, and whether the money went to the account that
now pays the card. A model never sees cents or basis points under a key that
follows 1.4: `shown` turns them into dollars and whole percent, and it marks or
withholds text a customer or a person wrote. The figure rule builds its allow-list from the same output, so what
a model reads and what it may write cannot drift (figures.py).

Pure functions: no database, no model.
"""

import re
from collections.abc import Iterable
from datetime import date

from app import policy

# Keys whose value is text a customer or a person wrote. A key that ends in _text is too (1.4).
_TEXT_KEYS = ("memo", "note")
# Any other string that holds whitespace, $ or % is treated as such text, whatever its key.
_TEXT = re.compile(r"[\s$%]")
_WRITTEN_BY = "the customer or a person"
_WITHHELD = {"withheld": "text written by the customer or a person"}
_AVERAGE_DAYS = 30  # the signal's cash_30d_avg_cents
_SHOWN_REVIEWS = 3  # earlier reviews that get a record of their own
_OWN_IDS = ("sig_", "hist_", "xs_")  # the prefixes of the records only the Orchestrator makes (1.4)
_ACCOUNT_NAME = re.compile(r"[A-Za-z][A-Za-z .&'-]{0,63}")
_MASK = re.compile(r"[A-Za-z0-9]{2,4}")


def _record(id: str, kind: str, data: dict) -> dict:
    return {"id": id, "kind": kind, "data": data}


def bp(part: int, whole: int) -> int | None:
    """part as basis points of whole, rounded half up. None when there is no whole to divide by."""
    if whole <= 0:
        return None
    return (10000 * part + whole // 2) // whole


def usd(cents: int) -> str:
    """Whole dollars, with cents only when they are not zero: "$230,000", "-$230,000", "$12.50"."""
    dollars, rest = divmod(abs(cents), 100)
    return f"{'-' if cents < 0 else ''}${dollars:,}" + (f".{rest:02d}" if rest else "")


def pct(bp: int) -> str:
    """Whole percent, half up: "44%", "425%", "0%". A share above nothing but under half a percent is "under 1%"."""
    if bp < 0:
        return "-" + pct(-bp)
    if 0 < bp < 50:
        return "under 1%"
    return f"{(bp + 50) // 100}%"


def written_date(day: str) -> str:
    """A YYYY-MM-DD day as a customer reads it: "Dec 5, 2026"."""
    when = date.fromisoformat(day)
    return f"{when:%b} {when.day}, {when.year}"


def account_label(account: dict) -> str | None:
    """How a person knows a linked account, "Chase Business Checking ••4821": a linked_account record's data or a
    signal's account. None when it has no name code may write.

    Code writes the name into sentences that reach a model unmarked (a specialist's hard facts), and a name can be
    one the customer gave the account. So only a name that reads as a bank's name for an account is used: letters,
    spaces and .&'- only, and at most five words, so that it is never six words of customer text in a row
    (cash_flow.guardrails.QUOTE_WORDS). The mask is used when it is 2 to 4 letters or digits, as a bank feed gives it.
    """
    name, mask = account.get("name"), account.get("mask")
    if not isinstance(name, str) or not _ACCOUNT_NAME.fullmatch(name) or len(re.findall(r"[A-Za-z']+", name)) > 5:
        return None
    return f"{name} ••{mask}" if isinstance(mask, str) and _MASK.fullmatch(mask) else name


def _text(value, untrusted: bool) -> dict:
    if untrusted:
        return {"untrusted_text": str(value), "written_by": _WRITTEN_BY}
    return dict(_WITHHELD)


def _shown_value(value, untrusted: bool):
    if isinstance(value, dict):
        return _shown_data(value, untrusted)
    if isinstance(value, list):
        return [_shown_value(item, untrusted) for item in value]
    if isinstance(value, str) and _TEXT.search(value):
        return _text(value, untrusted)
    return value


def _shown_entry(key: str, value, untrusted: bool) -> tuple[str, object]:
    if key in _TEXT_KEYS or key.endswith("_text"):
        return key, _text(value, untrusted)
    # type() and not isinstance(): True is an int, and must not read as $0.01.
    if type(value) is int and "_cents" in key:
        return key.replace("_cents", ""), usd(value)
    if type(value) is int and key.endswith("_bp"):
        return key.removesuffix("_bp"), pct(value)
    return key, _shown_value(value, untrusted)


def _shown_data(data: dict, untrusted: bool) -> dict:
    shown = {}
    for key, value in data.items():
        name, value = _shown_entry(key, value, untrusted)
        # Two keys must not collapse into one. A record that breaks the convention keeps the key it has.
        if name != key and (name in data or name in shown):
            name = key
        shown[name] = value
    return shown


def shown(record: dict, *, untrusted: bool = False) -> dict:
    """A record as a model reads it (1.4).

    untrusted=True only where the investigator reads a tool result: the text is
    there, marked. Everywhere else it is withheld.
    """
    return {"id": record["id"], "kind": record["kind"], "data": _shown_data(record["data"], untrusted)}


def shown_answer(answer: dict, *, untrusted: bool = False) -> dict:
    """A specialist's answer as a model reads it: its findings, what it could not determine, its records shown."""
    response = answer.get("response")
    if response is None:
        return {"error": answer.get("error")}
    return {
        "findings": response["findings"],
        "could_not_determine": response["could_not_determine"],
        "evidence": [shown(item, untrusted=untrusted) for item in response["evidence"]],
    }


def signal_facts(signal: dict, window: dict) -> list[dict]:
    """The five sig_ records: what the signal says, with the figures a report needs worked out."""
    bank, card = signal["snapshot"]["bank"], signal["snapshot"]["card"]
    as_of = date.fromisoformat(signal["as_of"])

    now, before = bank["cash_now_cents"], bank["cash_7d_ago_cents"]
    fall = max(0, before - now)
    cash = {
        "as_of": signal["as_of"],
        "window_start": window["start"],
        "window_end": window["end"],
        "window_days": (date.fromisoformat(window["end"]) - date.fromisoformat(window["start"])).days,
        "average_days": _AVERAGE_DAYS,
        "cash_now_cents": now,
        "cash_7d_ago_cents": before,
        "cash_30d_avg_cents": bank["cash_30d_avg_cents"],
        "change_cents": now - before,
        "fall_cents": fall,
    }
    # No share of a fall that did not happen, and none of a balance that was not above zero.
    if fall and (share := bp(fall, before)) is not None:
        cash["fall_share_bp"] = share
    # False while the policy has no number: G-13 cannot be cited until the owner gives one.
    cash["below_minimum"] = policy.MIN_CASH_CENTS is not None and now < policy.MIN_CASH_CENTS

    current, engine = signal["current_limit_cents"], signal["engine_limit_cents"]
    cut = current - engine
    limit = {"current_limit_cents": current, "engine_limit_cents": engine, "cut_cents": cut}
    if cut > 0 and (share := bp(cut, current)) is not None:
        limit["cut_share_bp"] = share
    limit |= {
        "is_cut": engine < current,
        "very_large": policy.LARGE_LIMIT_CENTS is not None and current >= policy.LARGE_LIMIT_CENTS,
        "review_reasons": list(signal["review_reasons"]),
    }

    balance = card["balance_cents"]
    position = {"balance_cents": balance}
    if (share := bp(balance, current)) is not None:
        position["used_share_bp"] = share
    position["over_engine_limit_cents"] = max(0, balance - engine)

    repayments = [
        {"due": repayment["due"], "amount_cents": repayment["amount_cents"], "status": repayment["status"]}
        for repayment in card["repayments"]
    ]
    statuses = [repayment["status"] for repayment in repayments]
    paid = {
        "repayments": repayments,
        "statuses": statuses,
        # With none on record, "every repayment paid" is not met.
        "all_paid": bool(statuses) and all(status == "paid" for status in statuses),
        "any_late": "late" in statuses,
        "any_failed": "failed" in statuses,
    }
    if repayments:
        paid["latest_status"] = sorted(repayments, key=lambda repayment: repayment["due"])[-1]["status"]

    accounts = [
        {
            "id": account["id"],
            # How a person knows the account, when the signal says (account_label).
            **{key: account[key] for key in ("name", "mask") if account.get(key) is not None},
            "last_synced": account["last_synced"],
            "days_behind": (as_of - date.fromisoformat(account["last_synced"])).days,
        }
        for account in bank["accounts"]
    ]
    synced = {"accounts": accounts, "any_behind": any(account["days_behind"] > 0 for account in accounts)}

    return [
        _record("sig_cash", "cash_change", cash),
        _record("sig_limit", "limit_change", limit),
        _record("sig_card", "card_position", position),
        _record("sig_repayments", "repayment_statuses", paid),
        _record("sig_accounts", "account_sync", synced),
    ]


def _same_season(days_before: int) -> bool:
    """A whole number of years ago, one or more, give or take policy.SEASON_DAYS."""
    years = round(days_before / 365.25)
    return years >= 1 and abs(days_before - round(years * 365.25)) <= policy.SEASON_DAYS


def _decided(review: dict) -> str | None:
    # What a person decided on a hand-over, and otherwise what the report recommended.
    return review.get("human_decision") or review["recommendation"]


def history_facts(reviews: list[dict], signal: dict) -> list[dict]:
    """The hist_ records (7.3): what earlier reviews of this customer decided. Typed, citable, no free text.

    reviews are history.earlier_reviews rows, every one of them (limit=None): the summary counts all it is
    given, and only the three newest get a record of their own. One dated on or after this signal's day is
    not an earlier review and is left out.
    """
    customer_id = signal["customer_id"]
    today = date.fromisoformat(signal["as_of"])
    earlier = []
    for review in reviews:
        days_before = (today - date.fromisoformat(review["as_of"])).days
        if days_before > 0:
            earlier.append((days_before, review))
    earlier.sort(key=lambda pair: pair[0])  # newest first

    in_window = [review for days_before, review in earlier if days_before <= policy.REPEAT_WINDOW_DAYS]
    # A request that reached the documents step counts however the case closed after it. Before that step existed
    # (and in the seeds) an approved request closed the case as done.
    requests = [
        review for review in in_window
        if review.get("requested_documents") or (review["approved"] and _decided(review) == "request_documents")
    ]
    summary = {
        "earlier_reviews": len(earlier),
        "approved_reviews": sum(1 for _, review in earlier if review["approved"]),
        "window_days": policy.REPEAT_WINDOW_DAYS,
        "reviews_in_window": len(in_window),
        "document_requests_in_window": len(requests),
        "document_requests_for_income_in_window": sum(
            1 for review in requests if review["likely_cause"] in policy.REPEAT_CAUSES
        ),
    }
    if earlier:
        summary["last_review_as_of"] = earlier[0][1]["as_of"]
    records = [_record(f"hist_{customer_id}_summary", "review_history", summary)]

    for days_before, review in earlier[:_SHOWN_REVIEWS]:
        then_before, then_after = review["cash_7d_ago_cents"], review["cash_now_cents"]
        data = {
            "as_of": review["as_of"],
            "days_before": days_before,
            "outcome": review["outcome"],
            "approved": bool(review["approved"]),
        }
        if review.get("human_decision"):
            data["human_decision"] = review["human_decision"]
        data |= {
            "likely_cause": review["likely_cause"],
            "recommendation": review["recommendation"],
            "guideline_ids": list(review["guideline_ids"]),
            "review_reasons": list(review["review_reasons"]),
            "cash_before_cents": then_before,
            "cash_after_cents": then_after,
            "fall_cents": max(0, then_before - then_after),
        }
        if data["fall_cents"] and (share := bp(data["fall_cents"], then_before)) is not None:
            data["fall_share_bp"] = share
        data["same_season"] = _same_season(days_before)
        records.append(_record(f"hist_{review['case_id']}", "earlier_review", data))

    # Cash counts as back only against a review a person approved, from the same time of an earlier year.
    season = next(
        ((days_before, review) for days_before, review in earlier if review["approved"] and _same_season(days_before)),
        None,
    )
    if season is not None:
        days_before, review = season
        now_before = signal["snapshot"]["bank"]["cash_7d_ago_cents"]
        recovery = {
            "earlier_case": review["case_id"],
            "earlier_as_of": review["as_of"],
            "days_before": days_before,
            "cash_before_then_cents": review["cash_7d_ago_cents"],
            "cash_after_then_cents": review["cash_now_cents"],
            "cash_before_now_cents": now_before,
        }
        if (share := bp(now_before, review["cash_7d_ago_cents"])) is not None:
            recovery["now_vs_then_bp"] = share
        records.append(_record(f"hist_{customer_id}_recovery", "cash_recovery", recovery))
    return records


def account_match(records: Iterable[dict]) -> dict | None:
    """xs_account_match: how many listed payments went to the account that now pays the card.

    Made once the card side says that account changed and the bank side has listed
    the window's payments. Neither side can say it alone: each sees one end.
    """
    records = list(records)
    change = next(
        (r for r in records if r["kind"] == "funding_account_change" and r["data"].get("changed") is True), None
    )
    listed = any(r["kind"] in ("payment_summary", "bank_transaction") for r in records)
    if change is None or not listed or not (account := change["data"].get("to")):
        return None
    sent = [r for r in records if r["kind"] == "bank_transaction" and r["data"].get("to_account") == account]
    amounts = [r["data"].get("amount_cents") for r in sent]
    match = {"account": account, "listed_transfers": len(sent)}
    # A transfer with no amount in whole cents leaves the total unknown. A sum that skipped it would be wrong.
    if all(type(amount) is int for amount in amounts):
        match["total_cents"] = sum(abs(amount) for amount in amounts)
    match["transaction_ids"] = [r["id"] for r in sent]
    return _record("xs_account_match", "account_match", match)


def ledger(state: dict) -> dict[str, dict]:
    """Every record a case holds, by id: its own facts, every record an answered question
    returned, and the cross-side match when it can be made. Nothing from a question that failed.

    A specialist's record under an id only the Orchestrator gives (sig_, hist_, xs_) is left
    out, whether or not code made that id in this case: an answer can neither replace nor
    forge code's own facts.
    """
    records = {}
    for record in state.get("facts", []):
        records.setdefault(record["id"], record)
    for answer in state.get("answers", []):
        if (response := answer.get("response")) is not None:
            for record in response["evidence"]:
                if not record["id"].startswith(_OWN_IDS):
                    records.setdefault(record["id"], record)
    if (match := account_match(records.values())) is not None:
        records[match["id"]] = match
    return records
