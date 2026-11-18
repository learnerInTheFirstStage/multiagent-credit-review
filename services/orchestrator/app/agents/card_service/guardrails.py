"""Code around the model, not inside it. Every rule here holds whatever the model writes.

1. What the model reads: a transaction memo is wrapped in <memo>...</memo>, angle brackets
   escaped; the prompt says what is inside is data. Amounts in cents and shares in basis points
   are shown as dollars and whole percents (app.graph.facts.usd and pct), the form the figure
   check reads, so the model copies a figure instead of converting it. The records in the
   response keep Card's own units.
2. The evidence pool: every record a tool returned in this run. The response's evidence is built
   from it, so the model cannot invent a record.
3. Draft checks, per finding: the claim is not empty; every cited id is in the pool; every figure
   in the claim (amount, percent, number, multiple; read as app.graph.figures reads it) is shown
   by the cited records; the claim recommends no limit action, in any form; while any record says
   too_short_to_tell, a claim does not call things normal unless it says the history is too short
   or rests on a record that made its own comparison; a claim writes no raw cents or basis points.
4. Every could_not_determine line, the ones code wrote too: no action, no amount or percent no record
   shows (side_text_problem). A reason code writes for a dropped finding counts the ids no tool returned,
   never quotes one, and states no figure.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from app.graph import figures
from app.graph.facts import pct, usd
from contracts import Evidence, Finding

TOO_SHORT = "too_short_to_tell"

# ---------------------------------------------------------------------------
# 1. memo marking
# ---------------------------------------------------------------------------


def for_the_model(result: Any) -> Any:
    """A deep copy of a tool result as the model reads it: every `memo` marked as data, every
    *_cents amount in dollars and every *_bp share in whole percent ("amount": "$50,000")."""
    return _mark(copy.deepcopy(result))


def _shown(key: str, value: Any) -> tuple[str, Any]:
    # type() and not isinstance(): True is an int, and must not read as $0.01.
    if type(value) is int and key.endswith("_cents"):
        return key.removesuffix("_cents"), usd(value)
    if type(value) is int and key.endswith("_bp"):
        return key.removesuffix("_bp"), pct(value)
    return key, value


def _mark(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key == "memo" and isinstance(item, str):
                out[key] = _wrap(item)
                continue
            name, shown = _shown(key, item)
            if name != key and (name in value or name in out):
                name, shown = key, item  # two keys must not collapse into one: keep Card's
            out[name] = _mark(shown)
        return out
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

# "suggests" and a gerund read as advice (_suggests_doing_it): every limit, cap, card, account or credit named
# within 40 characters after the gerund, in the same sentence, is taken for its object. Group 1 is a rise or a fall,
# which can also be a reading; a pause, a freeze, a suspension, a closure or a block is only ever an action.
_SUGGESTS_DOING = re.compile(r"\bsuggest\w*\s+(?:(reduc|lower|cutt|decreas|increas|rais|tighten|keep)"
                             r"|paus|freez|suspend|clos|block)ing\b", re.I)
_THING = re.compile(r"\b(limits?|credit\s+lines?|caps?|cards?|accounts?|credit)\b", re.I)
_ALWAYS_THE_OBJECT = re.compile(r"limit|credit\s+line|cap", re.I)
# After a rise or a fall, a card, an account or credit (never a limit or a cap) is not the object when it only
# qualifies what rises or falls: a noun after it ("increasing card usage") or a preposition before it
# ("increasing reliance on the card"). Never before an action noun: "reducing risk with a card freeze".
_QUALIFIES = re.compile(r"\s+(usage|use|spend\w*|activit\w+|balances?|utili[sz]ation|payments?|repayments?"
                        r"|transactions?|statements?|cycles?|history|holders?|data|series|trends?)\b", re.I)
_AFTER_A_PREPOSITION = re.compile(r"\b(on|of|in|to|for|at|from|with|over|across|against|toward|towards|since)\s+"
                                  r"((the|its|their|his|her|this|that|a|an|each|every)\s+)?$", re.I)
_ACTION_NOUN = re.compile(r"\s+(reduction|cut|decrease|increase|freeze|suspension|closure|block|pause|change"
                          r"|adjustment)s?\b", re.I)
# The same pattern is in cash_flow/guardrails.py; tests/_action_phrasings.py holds the cases both must agree on.
_CAUSATIVE = (r"\b(should|must|ought\s+to|needs?\s+to|has\s+to|have\s+to)\s+(have|get)\s+(its|their|the|his|her)\s+"
              r"([\w'-]+\s+){0,2}?((credit\s+|card\s+)?limits?|cards?|credit\s+lines?)\s+(be\s+)?"
              r"(reduc|lower|cut|decreas|increas|rais|paus|freez|frozen|suspend|clos|block|tighten)\w*")
_SUGGESTS_IT_BE = (r"\bsuggest\w*\s+(that\s+)?(the\s+|its\s+|their\s+|this\s+)?([\w'-]+\s+)?"
                   r"((credit\s+|card\s+)?limits?|cards?|credit\s+lines?|accounts?)\s+(be|is\s+to|should\s+be)\s+"
                   r"(reduc|lower|cut|decreas|increas|rais|paus|freez|frozen|suspend|clos|block|tighten)\w*")
_RECOMMENDATION = [
    # "suggests" (the data suggests ...) is a reading, not advice.
    re.compile(r"\b(recommend\w*|advis\w+|suggest(?!s\b)\w*|should|propose\w*|consider)\b[^.]{0,60}\b(reduc\w*|lower\w*|"
               r"cut\w*|rais\w*|increas\w*|paus\w*|freez\w*|suspend\w*|clos\w*|block\w*|keep\w*)\b[^.]{0,40}"
               r"\b(limits?|cards?|accounts?)\b", re.I),
    re.compile(r"\b(reduce|lower|cut|decrease|increase|raise|keep)\s+(the\s+|their\s+|its\s+)?(credit\s+|card\s+)?limit\b", re.I),
    # Not "close to the card limit".
    re.compile(r"\b(pause|freeze|suspend|close(?!\s+to\b)|block)\s+(the\s+|their\s+|its\s+|all\s+)?([\w'-]+\s+){0,2}"
               r"(cards?|accounts?)\b", re.I),
    re.compile(r"\b(keep_limit|reduce_limit|pause_card|request_documents|needs_human)\b", re.I),
    re.compile(r"\brequest (the )?documents\b", re.I),
    # "must have" as an order, not an inference: "the customer must have their limit reduced".
    re.compile(_CAUSATIVE, re.I),
    # The subjunctive: "spend suggests that the card limit be reduced".
    re.compile(_SUGGESTS_IT_BE, re.I),
]
# A limit or card action in a noun, gerund or passive form ("a limit reduction", "lowering the limit",
# "the card ought to be frozen") is an action only beside a word that says it should happen: without
# one, "the card was paused on 2026-10-01" is a fact.
_ACTION_WORD = re.compile(r"\b(reduc\w*|lower\w*|cut\w*|decreas\w*|rais\w*|increas\w*|paus\w*|freez\w*|frozen|"
                          r"suspen\w*|clos\w*|block\w*|tighten\w*)\b", re.I)
_ACTION_ON = re.compile(r"\b(limits?|cards?|credit lines?)\b", re.I)
# Not the "must" of an inference: "it must have been paid after the limit was lowered".
_SHOULD = re.compile(r"\b(warrant\w*|prudent|advis\w*|should|ought|must(?!\s+have\b)|needs? to|recommend\w*|"
                     r"justified|appropriate|necessary|called for|makes sense|sensible|wise)\b", re.I)
_SENTENCE = re.compile(r"[^.;!?]+")
# "4852661 cents" or "3500 bp": a raw unit in a claim, which the figure check reads as a bare number.
_RAW_UNITS = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?\s*(?:cents?|bps?|basis points?)\b", re.I)
_CALLS_IT_NORMAL = re.compile(r"\b(normal|usual|steady|stable|typical|in line|consistent|unremarkable|as expected)\b"
                              r"|\bout of the ordinary\b|\bnothing (unusual|abnormal|of note)\b", re.I)
# What is usual for this customer, which a too_short_to_tell record says cannot be known yet. Words such
# as "consistent" or "as expected" can describe repayments alone, so only these count pool-wide.
_CALLS_IT_USUAL = re.compile(r"\b(normal|usual|typical|unremarkable)\b|\bout of the ordinary\b"
                             r"|\bnothing (unusual|abnormal|of note)\b", re.I)
# A claim that says there is too little history is the right answer, not a claim of normal.
_SAYS_TOO_SHORT = re.compile(r"\btoo (short|few|little)\b|\b(cannot|can't|can not|could not|unable to) (yet )?"
                             r"(tell|say|judge|determine)\b|\bnot enough (history|data)\b", re.I)


def _suggests_doing_it(text: str) -> bool:
    """ "Spend suggests lowering the card limit", "it suggests pausing the card". Not "suggests that ...",
    "suggests the customer is increasingly relying on the card", "suggests increasing card usage" or
    "suggests decreasing activity on the account"."""
    for said in _SUGGESTS_DOING.finditer(text):
        rest = text[said.end():].split(".")[0]
        for thing in _THING.finditer(rest):
            if thing.start() > 40:
                break
            if said.group(1) is None or _ALWAYS_THE_OBJECT.match(thing.group(1)):
                return True
            after = rest[thing.end():]
            if _ACTION_NOUN.match(after) or not (
                    _QUALIFIES.match(after) or _AFTER_A_PREPOSITION.search(rest[:thing.start()])):
                return True
    return False


def recommends_an_action(text: str) -> bool:
    # "Reduce_the_credit_limit" is words joined by underscores, which \b would read as one word.
    text = text.replace("_", " ")
    if any(p.search(text) for p in _RECOMMENDATION) or _suggests_doing_it(text):
        return True
    return any(_ACTION_WORD.search(s) and _ACTION_ON.search(s) and _SHOULD.search(s)
               for s in _SENTENCE.findall(text))


def unsupported_figures(claim: str, records: list[Evidence]) -> list[str]:
    """Figures in the claim that the cited records do not show, read as the case graph reads them
    (app.graph.figures): amounts however written ($, USD, US$, K, million), percents, numbers above
    twelve, and multiples, which no record backs. Dates are left out: the graph reads none out of a
    timestamp, so a correct "declined on 2026-11-06" would be refused."""
    allowed = figures.allowed_figures([r.model_dump(mode="json") for r in records])
    return [written for token, written in figures.figures(claim).items()
            if token not in allowed and token[0] not in "dy"]


def _compares(record: Evidence) -> bool:
    """The record holds its own comparison with the customer's history."""
    trend = record.data.get("utilization_trend") or record.data.get("history")
    return trend is not None and trend != TOO_SHORT


@dataclass
class Checked:
    accepted: list[Finding] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # told to the model in a repair round
    dropped: list[str] = field(default_factory=list)  # for could_not_determine if they still fail


def side_text_problem(text: str, pool: EvidencePool) -> str | None:
    """Why a could_not_determine line cannot go out, or None: it recommends an action, or states an amount
    or a percent no record of the run shows (a line names what is missing; "the last 3 cycles" is a span)."""
    if recommends_an_action(text):
        return "it recommended an action"
    allowed = figures.allowed_figures([r.model_dump(mode="json") for r in pool.records.values()])
    if [written for token, written in figures.figures(text, marked_only=True).items() if token not in allowed]:
        return "it stated a figure no record shows"
    return None


def check(findings: list[Finding], pool: EvidencePool) -> Checked:
    out = Checked()
    short = any(too_short(e) for e in pool.records.values())
    for n, finding in enumerate(findings, start=1):
        claim = " ".join(finding.claim.split())[:500]
        if not claim:
            out.problems.append(f"Finding {n}: the claim is empty.")
            out.dropped.append("A finding was left out because its claim was empty.")
            continue
        ids = list(dict.fromkeys(finding.evidence_ids))
        unknown = [i for i in ids if i not in pool]
        cited = pool.pick(ids)
        # `problems` go back to the model in a repair; `reasons` reach the Orchestrator, so they repeat
        # nothing the model wrote: no claim text, no figure the records lack, and no id a tool did not return
        # (they are counted; "Reduce_the_credit_limit_to_500_now" has the shape of an id).
        problems, reasons = [], []
        if unknown:
            # The model is told which (its own text, back to it); the Orchestrator only how many.
            problems.append(f"cites ids no tool returned: {[i[:64] for i in unknown]}")
            reasons.append(f"cites {len(unknown)} id{'s' if len(unknown) != 1 else ''} no tool returned")
        if not cited:
            problems.append("cites no record a tool returned")
            reasons.append(problems[-1])
        if recommends_an_action(claim):
            problems.append("recommends an action on the limit or the card; report facts only")
            reasons.append("recommends an action on the limit or the card")
        if cited and (bad := unsupported_figures(claim, cited)):
            problems.append(f"states {bad}, which the cited records {[e.id for e in cited]} do not hold; "
                            "copy figures from the cited records or cite the record that holds them")
            reasons.append(f"states figures the cited records {[e.id for e in cited]} do not hold")
        if raw := [m.group(0) for m in _RAW_UNITS.finditer(claim)]:
            problems.append(f"writes {raw} in raw units; copy amounts and percents as the tools show them "
                            "(\"amount\": \"$50,000\", \"used_share\": \"92%\")")
            reasons.append("writes an amount in raw cents or basis points")
        if cited and _CALLS_IT_NORMAL.search(claim) and not _SAYS_TOO_SHORT.search(claim):
            normal = None
            if any(too_short(e) for e in cited):
                normal = "calls something normal or steady from a record that says too_short_to_tell"
            elif short and _CALLS_IT_USUAL.search(claim) and not any(_compares(e) for e in cited):
                normal = ("calls something normal or usual while a record says too_short_to_tell, and "
                          "cites no record that compares with the customer's history")
            if normal:
                problems.append(normal)
                reasons.append(normal)
        if problems:
            out.problems.append(f"Finding {n} ('{claim[:90]}'): " + "; ".join(problems) + ".")
            out.dropped.append(f"A finding was left out because it {'; '.join(reasons)[:200]}.")
            continue
        out.accepted.append(Finding(claim=claim, confidence=finding.confidence, evidence_ids=[e.id for e in cited]))
    return out


def too_short(record: Evidence) -> bool:
    """The record says there is too little history to compare with."""
    return TOO_SHORT in (record.data.get("utilization_trend"), record.data.get("history"))
