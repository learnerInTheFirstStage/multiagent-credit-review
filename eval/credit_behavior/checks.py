"""Grading for the Credit Behavior test set: the checks every answer must pass, and each case's own.

Imported by run.py and by services/orchestrator/tests/test_credit_behavior_set.py, both with the
Orchestrator's code importable (it uses the graph's own acceptance check and the agent's figure
check, so the set grades by the same rules the service runs on).
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.credit_behavior.guardrails import recommends_an_action, unsupported_figures
from app.graph import build
from contracts import Evidence, InvestigateResponse

GENERAL = ("valid", "cites_returned", "figures_from_records", "counts_from_records", "no_action")
# A count, not the tail of an amount: "$50,000 failed" holds no count.
_COUNT = re.compile(r"(?<![\d,.$])\b(\d+)\s+(?:were\s+|was\s+)?(paid on time|paid|late|failed)\b", re.I)


def matches(record: dict, spec: dict) -> bool:
    """A record is of the spec's kind and each of its `where` keys has the value given (or one of {in: [...]})."""
    if record.get("kind") != spec["kind"]:
        return False
    data = record.get("data", {})
    for key, want in (spec.get("where") or {}).items():
        have = data.get(key)
        if isinstance(want, dict) and "in" in want:
            if have not in want["in"]:
                return False
        elif have != want:
            return False
    return True


def _counts_hold(claim: str, cited: list[dict]) -> str | None:
    """Every "N paid / late / failed" in a claim is what the cited records count. None when it holds."""
    summaries = [r["data"] for r in cited if r["kind"] == "repayment_summary"]
    repayments = [r["data"] for r in cited if r["kind"] == "card_repayment"]
    for m in _COUNT.finditer(claim):
        n, word = int(m.group(1)), m.group(2).lower()
        status = "paid" if word.startswith("paid") else word
        if summaries:
            if all(s.get(status) != n for s in summaries):
                return f"'{m.group(0)}' but the cited summary says {summaries[0].get(status)}"
        elif sum(r.get("status") == status for r in repayments) != n:
            return f"'{m.group(0)}' but the cited repayments hold {sum(r.get('status') == status for r in repayments)}"
    return None


def general(response: dict, returned: dict[str, dict]) -> dict[str, str | None]:
    """The checks every answer must pass. Each maps to None (passed) or why it failed."""
    out: dict[str, str | None] = dict.fromkeys(GENERAL)
    try:
        parsed = InvestigateResponse.model_validate(response)
        build._usable(parsed, known={})
    except ValueError as error:
        out["valid"] = str(error)[:200]
        return out
    evidence = {e["id"]: e for e in response["evidence"]}
    problems = []
    for item in response["evidence"]:
        source = returned.get(item["id"])
        if source is None:
            problems.append(f"{item['id']} was not returned by any tool in this request")
        elif (source["kind"], source["data"]) != (item["kind"], item["data"]):
            problems.append(f"{item['id']} differs from what the tool returned")
    out["cites_returned"] = "; ".join(problems) or None
    figures, counts, actions = [], [], []
    for f in response["findings"]:
        cited = [evidence[i] for i in f["evidence_ids"] if i in evidence]
        if bad := unsupported_figures(f["claim"], [Evidence.model_validate(c) for c in cited]):
            figures.append(f"{bad} in '{f['claim'][:80]}'")
        if wrong := _counts_hold(f["claim"], cited):
            counts.append(wrong)
        if recommends_an_action(f["claim"]):
            actions.append(f"'{f['claim'][:80]}'")
    if response.get("suggested_next") and recommends_an_action(response["suggested_next"]):
        actions.append(f"suggested_next '{response['suggested_next'][:80]}'")
    out["figures_from_records"] = "; ".join(figures) or None
    out["counts_from_records"] = "; ".join(counts) or None
    out["no_action"] = "; ".join(actions) or None
    return out


def expected(case: dict, response: dict) -> dict[str, str | None]:
    """The case's own checks."""
    expect = case.get("expect", {})
    out: dict[str, str | None] = {}
    evidence = {e["id"]: e for e in response.get("evidence", [])}
    cited = [evidence[i] for f in response.get("findings", []) for i in f["evidence_ids"] if i in evidence]
    if expect.get("findings") == "some":
        out["has_findings"] = None if response.get("findings") else "no findings"
    if expect.get("findings") == "none":
        out["no_findings"] = None if not response.get("findings") else f"{len(response['findings'])} findings"
    for n, spec in enumerate(expect.get("records") or [], start=1):
        out[f"cites_{n}_{spec['kind']}"] = None if any(matches(r, spec) for r in cited) else f"nothing cited matches {spec}"
    for n, spec in enumerate(expect.get("forbid") or [], start=1):
        hit = [r["id"] for r in cited if matches(r, spec)]
        out[f"forbids_{n}_{spec['kind']}"] = f"cites {hit}" if hit else None
    unknown = " ".join(response.get("could_not_determine") or []).lower()
    for n, text in enumerate(expect.get("says") or [], start=1):
        out[f"says_{n}"] = None if text.lower() in unknown else f"could_not_determine does not say '{text}'"
    return out


def grade(case: dict, response: dict, returned: dict[str, dict]) -> dict[str, Any]:
    checks = {**general(response, returned), **expected(case, response)}
    return {"id": case["id"], "passed": all(v is None for v in checks.values()), "checks": checks}
