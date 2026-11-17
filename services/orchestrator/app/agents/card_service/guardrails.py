"""Code around the model, not inside it. Every rule here holds whatever the model writes.

1. Memo marking: a transaction memo is wrapped in <memo>...</memo>, angle brackets escaped,
   before a tool result reaches the model. The prompt says what is inside is data.
2. The evidence pool: every record a tool returned in this run. The response's evidence is built
   from it, so the model cannot invent a record.
3. Draft checks, per finding: every cited id is in the pool; every dollar amount and percentage
   in the claim appears in the cited records; the claim recommends no limit action; a claim
   resting on a record that says too_short_to_tell does not call it normal.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from contracts import Evidence, Finding

TOO_SHORT = "too_short_to_tell"

# ---------------------------------------------------------------------------
# 1. memo marking
# ---------------------------------------------------------------------------


def for_the_model(result: Any) -> Any:
    """A deep copy of a tool result with every `memo` value marked as data."""
    return _mark(copy.deepcopy(result))


def _mark(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (_wrap(v) if k == "memo" and isinstance(v, str) else _mark(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mark(v) for v in value]
    return value


def _wrap(memo: str) -> str:
    # Escaped first, so a memo cannot close the tag early and smuggle text out of it.
    return "<memo>" + memo.replace("<", "&lt;").replace(">", "&gt;") + "</memo>"


# ---------------------------------------------------------------------------
# 2. the evidence pool
# ---------------------------------------------------------------------------


class EvidencePool:
    """Every record the tools returned during this run, first one kept."""

    def __init__(self) -> None:
        self.records: dict[str, Evidence] = {}

    def add(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        for item in result.get("evidence") or []:
            if not isinstance(item, dict) or not item.get("id") or item["id"] in self.records:
                continue
            try:
                self.records[item["id"]] = Evidence.model_validate(item)
            except ValueError:
                continue  # a malformed record is simply not citable

    def __contains__(self, evidence_id: str) -> bool:
        return evidence_id in self.records

    def of_kind(self, kind: str) -> list[Evidence]:
        return [e for e in self.records.values() if e.kind == kind]

    def pick(self, ids: list[str]) -> list[Evidence]:
        return [self.records[i] for i in dict.fromkeys(ids) if i in self.records]


# ---------------------------------------------------------------------------
# 3. draft checks
# ---------------------------------------------------------------------------

_RECOMMENDATION = [
    re.compile(r"\b(recommend\w*|advis\w+|suggest\w*|should|propose\w*|consider)\b[^.]{0,60}\b(reduc\w*|lower\w*|cut\w*|"
               r"rais\w*|increas\w*|paus\w*|freez\w*|suspend\w*|clos\w*|block\w*|keep\w*)\b[^.]{0,40}\b(limits?|cards?|accounts?)\b", re.I),
    re.compile(r"\b(reduce|lower|cut|decrease|increase|raise|keep)\s+(the\s+|their\s+|its\s+)?(credit\s+|card\s+)?limit\b", re.I),
    re.compile(r"\b(pause|freeze|suspend|close|block)\s+(the\s+|their\s+|its\s+|all\s+)?([\w'-]+\s+){0,2}(cards?|accounts?)\b", re.I),
    re.compile(r"\b(keep_limit|reduce_limit|pause_card|request_documents|needs_human)\b", re.I),
    re.compile(r"\brequest (the )?documents\b", re.I),
]
_MONEY = re.compile(r"(?<![\w.])-?\$\s?(\d[\d,]*(?:\.\d+)?)\s*(k|m|thousand|million)?\b", re.I)
_PERCENT = re.compile(r"([+-]?\d+(?:\.\d+)?)\s?%")
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}
_CALLS_IT_NORMAL = re.compile(r"\b(normal|usual|steady|stable|typical|in line|consistent)\b", re.I)


def recommends_an_action(text: str) -> bool:
    return any(p.search(text) for p in _RECOMMENDATION)


def _numbers(value: Any, key: str, dollars: list[float], percents: list[float]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _numbers(v, k, dollars, percents)
    elif isinstance(value, list):
        for v in value:
            _numbers(v, key, dollars, percents)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if key.endswith("_cents"):
            dollars.append(value / 100)
        elif key.endswith("_bp"):
            percents.append(value / 100)


def unsupported_figures(claim: str, records: list[Evidence]) -> list[str]:
    """Dollar amounts and percentages in the claim that none of the cited records hold."""
    dollars: list[float] = []
    percents: list[float] = []
    for record in records:
        _numbers(record.data, "", dollars, percents)
    bad = []
    for m in _MONEY.finditer(claim):
        value = float(m.group(1).replace(",", "")) * _SCALE.get((m.group(2) or "").lower(), 1)
        rounded = bool(m.group(2))  # "$50K" is a rounded figure; "$50,000.00" is exact
        if not any(abs(abs(value) - abs(d)) <= (abs(d) * 0.05 if rounded else max(1.0, abs(d) * 0.005)) for d in dollars):
            bad.append(m.group(0).strip())
    for m in _PERCENT.finditer(claim):
        value = abs(float(m.group(1)))
        if not any(abs(value - abs(p)) <= 0.6 for p in percents):
            bad.append(m.group(0).strip())
    return bad


@dataclass
class Checked:
    accepted: list[Finding] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # told to the model in a repair round
    dropped: list[str] = field(default_factory=list)  # for could_not_determine if they still fail


def check(findings: list[Finding], pool: EvidencePool) -> Checked:
    out = Checked()
    for n, finding in enumerate(findings, start=1):
        claim = " ".join(finding.claim.split())[:500]
        ids = list(dict.fromkeys(finding.evidence_ids))
        unknown = [i for i in ids if i not in pool]
        cited = pool.pick(ids)
        problems = []
        if unknown:
            problems.append(f"cites ids no tool returned: {unknown}")
        if not cited:
            problems.append("cites no record a tool returned")
        if recommends_an_action(claim):
            problems.append("recommends an action on the limit or the card; report facts only")
        if cited and (bad := unsupported_figures(claim, cited)):
            problems.append(f"states {bad}, which the cited records {[e.id for e in cited]} do not hold; "
                            "copy figures from the cited records or cite the record that holds them")
        if cited and _CALLS_IT_NORMAL.search(claim) and any(too_short(e) for e in cited):
            problems.append("calls something normal or steady from a record that says too_short_to_tell")
        if problems:
            out.problems.append(f"Finding {n} ('{claim[:90]}'): " + "; ".join(problems) + ".")
            out.dropped.append(f"A finding was left out because it {'; '.join(problems)[:200]}.")
            continue
        out.accepted.append(Finding(claim=claim, confidence=finding.confidence, evidence_ids=[e.id for e in cited]))
    return out


def too_short(record: Evidence) -> bool:
    """The record says there is too little history to compare with."""
    return TOO_SHORT in (record.data.get("utilization_trend"), record.data.get("history"))
