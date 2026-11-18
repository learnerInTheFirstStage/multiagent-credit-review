"""Grading for the Card Service agent's test set: the checks every answer must pass, and each case's own.

Imported by run.py and by services/orchestrator/tests/test_card_service_set.py, both with the
Orchestrator's code importable (it uses the graph's own acceptance check and the agent's figure
check, so the set grades by the same rules the service runs on).
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.card_service.guardrails import _CALLS_IT_NORMAL, recommends_an_action, unsupported_figures
from app.graph import build
from contracts import Evidence, InvestigateResponse

GENERAL = ("valid", "cites_returned", "figures_from_records", "counts_from_records", "wording_from_records",
           "no_action")
# A count, not the tail of an amount or a date: "$50,000 failed", "due 2026-11-05 failed", "due 11/05
# failed" and "due Nov 5 failed" hold no count. "1 of 3 failed" is 1. A match with a month is skipped.
_MONTH = (r"(?P<month>\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
          r"|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?\s+)?")
_COUNT = re.compile(_MONTH + r"(?<![\d,.$/-])\b(?P<n>\d+)(?:\s+of\s+(?:the\s+)?(?:last\s+)?\d+)?\s+(?:were\s+|was\s+)?"
                    r"(?P<status>paid on time|paid|late|failed)\b", re.I)
# A repayment Card says is still owed (paid_since: no), told as paid after all, or about to be.
_SAYS_PAID = re.compile(r"\b(?:re)?paid\s+(?:since|later|after|afterwards|back)\b"
                        r"|\b(?:re)?paid\s+(?:\w+\s+){1,2}later\b"
                        r"|\bwill\s+(?:\w+\s+)?be\s+(?:re)?paid\b"
                        r"|\b(?:probably|likely)\s+(?:be\s+|been\s+|got\s+|gets\s+)?(?:re)?paid\b", re.I)
_NEGATED = re.compile(r"\b(?:not|never|no)\b|n't\b", re.I)
# "its usual 47%", "the customer's usual share", "higher than usual", "a steady climb", "consistent with
# the rising trend": these compare with the past or describe the rise; they do not call the level normal.
_COMPARED_WITH = re.compile(r"(?:\b(?:its|their|the|a|than|above|below|over|under)|\w(?:'s|s'))\s+$", re.I)
_DESCRIBES_A_FIGURE_OR_RISE = re.compile(
    r"^\s+(?:[\d$]|(?:with\s+(?:the\s+|a\s+|its\s+)?)?(?:ris|climb|increas|grow|upward|spik))", re.I)


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
        if m.group("month"):
            continue
        n, word = int(m.group("n")), m.group("status").lower()
        status = "paid" if word.startswith("paid") else word
        if summaries:
            if all(s.get(status) != n for s in summaries):
                return f"'{m.group(0)}' but the cited summary says {summaries[0].get(status)}"
        elif sum(r.get("status") == status for r in repayments) != n:
            return f"'{m.group(0)}' but the cited repayments hold {sum(r.get('status') == status for r in repayments)}"
    return None


def _said(pattern: re.Pattern, claim: str) -> str | None:
    """The first match not negated in its own clause ("has not been paid since", "hasn't been paid since")."""
    for m in pattern.finditer(claim):
        clause = re.split(r"[.,;:!?]|\bbut\b", claim[: m.start()], flags=re.I)[-1]
        if _NEGATED.search(clause) or (m.group(0).lower() == "usual" and _COMPARED_WITH.search(clause)):
            continue
        if pattern is _CALLS_IT_NORMAL and _DESCRIBES_A_FIGURE_OR_RISE.search(claim[m.end():]):
            continue
        return m.group(0)
    return None


def _wording_holds(claim: str, cited: list[dict]) -> str | None:
    """The claim does not contradict what its cited records say in words. None when it holds."""
    for r in cited:
        if r["kind"] == "card_repayment" and r["data"].get("paid_since") == "no":
            if said := _said(_SAYS_PAID, claim):
                return f"'{said}' but {r['id']} says paid_since: no"
        if r["kind"] == "card_balance" and r["data"].get("utilization_trend") in ("rising", "spiking"):
            if said := _said(_CALLS_IT_NORMAL, claim):
                return f"'{said}' but {r['id']} says utilization is {r['data']['utilization_trend']}"
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
    figures, counts, wording, actions = [], [], [], []
    for f in response["findings"]:
        cited = [evidence[i] for i in f["evidence_ids"] if i in evidence]
        if bad := unsupported_figures(f["claim"], [Evidence.model_validate(c) for c in cited]):
            figures.append(f"{bad} in '{f['claim'][:80]}'")
        if wrong := _counts_hold(f["claim"], cited):
            counts.append(wrong)
        if wrong := _wording_holds(f["claim"], cited):
            wording.append(wrong)
        if recommends_an_action(f["claim"]):
            actions.append(f"'{f['claim'][:80]}'")
    if response.get("suggested_next") and recommends_an_action(response["suggested_next"]):
        actions.append(f"suggested_next '{response['suggested_next'][:80]}'")
    out["figures_from_records"] = "; ".join(figures) or None
    out["counts_from_records"] = "; ".join(counts) or None
    out["wording_from_records"] = "; ".join(wording) or None
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


def grade(case: dict, response: dict, returned: dict[str, dict], problems: list[str] = ()) -> dict[str, Any]:
    """`problems`: what went wrong with the runner's calls to Card (a call with no recording, a reply not recorded)."""
    checks = {**general(response, returned), **expected(case, response), "card_calls": "; ".join(problems) or None}
    return {"id": case["id"], "passed": all(v is None for v in checks.values()), "checks": checks}
