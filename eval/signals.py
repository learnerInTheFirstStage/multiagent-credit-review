"""The review signal for a test customer, built from data/shared_customers.yaml.

Nobody owns the signal generator yet. Until someone does, the evaluation builds
its signals here, by the rules in API doc sections 3 and 10. Three things the
signal needs are not in the data file and are filled in:

- cash_30d_avg_cents: taken to be the cash of a week ago.
- repayment due dates: the 5th of the last three months.
- repayment amounts: the monthly card spend, or the amount of the failed or late one.
"""

from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
# The team's twelve customers, then the ones the evaluation adds.
FILES = (ROOT / "data" / "shared_customers.yaml", ROOT / "eval" / "extra_customers.yaml")
ENGINE_SHARE = 0.20  # the engine would set the limit at 20% of linked cash
LARGE_CUT = 1 / 3  # a cut of a third or more goes to a person


def customers() -> tuple[date, dict[str, dict]]:
    files = [yaml.safe_load(path.read_text()) for path in FILES]
    today = date.fromisoformat(files[0]["clock"]["signal_date"])
    return today, {customer["id"]: customer for data in files for customer in data["customers"]}


def _months_back(day: date, months: int) -> date:
    month = day.month - months
    return date(day.year + (month - 1) // 12, (month - 1) % 12 + 1, 5)


def signal_for(customer: dict, today: date) -> dict:
    bank, card = customer["snapshot"]["bank"], customer["snapshot"]["card"]
    limit = card["credit_limit_cents"]
    engine_limit = round(bank["cash_now_cents"] * ENGINE_SHARE)
    statuses = card["last_3_repayments"]
    trouble = {event["kind"]: event for event in customer["signal_window"]["card_events"]}

    reasons = []
    if limit and 1 - engine_limit / limit >= LARGE_CUT:
        reasons.append("large_cut")
    if "failed" in statuses:
        reasons.append("failed_repayment")
    if any(account["days_stale"] or not account["active"] for account in bank["linked_accounts"]):
        reasons.append("incomplete_data")

    repayments = []
    for position, status in enumerate(statuses):
        amount = customer["baselines"]["card"]["monthly_card_spend_cents"]
        if position == len(statuses) - 1 and f"{status}_repayment" in trouble:
            amount = trouble[f"{status}_repayment"]["amount_cents"]
        due = _months_back(today, len(statuses) - 1 - position)
        repayments.append({"due": due.isoformat(), "amount_cents": amount, "status": status})

    return {
        "signal_id": f"sig_{customer['id']}",
        "customer_id": customer["id"],
        "as_of": today.isoformat(),
        "current_limit_cents": limit,
        "engine_limit_cents": engine_limit,
        "review_reasons": reasons,
        "snapshot": {
            "bank": {
                "cash_now_cents": bank["cash_now_cents"],
                "cash_7d_ago_cents": bank["cash_week_ago_cents"],
                "cash_30d_avg_cents": bank["cash_week_ago_cents"],
                "accounts": [
                    {
                        "id": account["id"],
                        "last_synced": date.fromordinal(today.toordinal() - account["days_stale"]).isoformat(),
                        # How a person knows the account, only when the data names it.
                        **{key: account[key] for key in ("name", "mask") if key in account},
                    }
                    for account in bank["linked_accounts"]
                ],
            },
            "card": {"balance_cents": round(limit * card["utilization_pct"]), "repayments": repayments},
        },
    }
