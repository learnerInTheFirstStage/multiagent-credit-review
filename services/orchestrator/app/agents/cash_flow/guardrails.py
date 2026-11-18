"""Code around the model, not inside it. Every rule here holds whatever the model writes.

1. What the model reads: a tool result with every amount in dollars and every share in percent, as the
   case graph shows records (app.graph.facts), so the model copies figures and never works them out. Text
   a customer or a person could have written is marked as data: a `memo`, `note` or `*_text` value goes in
   <memo>...</memo>, any other text with a space, $, % or angle bracket in <text>...</text>, brackets escaped.
2. Draft checks, per finding: every cited id was returned by a tool in this run; every figure in the
   claim is one the cited records show (app.graph.figures, the rule the case graph's report check uses);
   the claim recommends no limit or card action; it does not repeat text a customer wrote.
3. The same action, figure and quoting checks for every could_not_determine line, the ones code wrote too,
   and for suggested_next. A reason code writes for a dropped finding counts the ids no tool returned,
   never quotes one, and states no figure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.graph import facts, figures
from contracts import Finding

_MEMO_KEYS = ("memo", "note")
_FREE_TEXT = re.compile(r"[\s$%<>]")
QUOTE_WORDS = 6  # this many words in a row from a customer's text is a quote of it


# ---------------------------------------------------------------------------
# 1. what the model reads
# ---------------------------------------------------------------------------


def for_the_model(value: Any) -> Any:
    """A copy of a tool result as the model may read it: figures formatted, free text marked as data."""
    if isinstance(value, dict):
        shown: dict[str, Any] = {}
        for key, item in value.items():
            name, item = _entry(str(key), item)
            if name != key and (name in value or name in shown):
                name = key  # two keys must not collapse into one
            shown[name] = item
        return shown
    if isinstance(value, list):
        return [for_the_model(item) for item in value]
    if isinstance(value, str) and _FREE_TEXT.search(value):
        return _wrap("text", value)
    return value


def _entry(key: str, value: Any) -> tuple[str, Any]:
    if _FREE_TEXT.search(key):
        key = _wrap("text", key)
    if isinstance(value, str) and (key in _MEMO_KEYS or key.endswith("_text")):
        return key, _wrap("memo", value)
    # type() and not isinstance(): True is an int, and must not read as $0.01.
    if type(value) is int and "_cents" in key:
        return key.replace("_cents", ""), facts.usd(value)
    if type(value) is int and key.endswith("_bp"):
        return key.removesuffix("_bp"), facts.pct(value)
    return key, for_the_model(value)


def _wrap(tag: str, text: str) -> str:
    # Escaped first, so the text cannot close the tag early and smuggle words out of it.
    return f"<{tag}>" + text.replace("<", "&lt;").replace(">", "&gt;") + f"</{tag}>"


def free_texts(records: list[dict]) -> list[str]:
    """Every piece of text in the records that a customer or a person could have written."""
    found: list[str] = []

    def walk(value: Any, key: str) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, str(k))
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str) and (key in _MEMO_KEYS or key.endswith("_text") or _FREE_TEXT.search(value)):
            found.append(value)

    for record in records:
        walk(record.get("data"), "")
    return found


# ---------------------------------------------------------------------------
# 2. and 3. checks
# ---------------------------------------------------------------------------

# Not "close to the card limit".
_ACT = (r"(?:keep|kept|maintain|hold|reduc|lower|cut|decreas|increas|rais|tighten|loosen|freez|froze|paus"
        r"|suspend|clos(?!e\s+to\b)|block|restrict|revok|cancel)\w*")
_LIMIT = r"(?:(?:credit\s+|card\s+)?limits?|credit\s+lines?)"
_RECOMMENDATION = [
    # "Lowering the credit limit is warranted", "reduce the customer's limit", "cut its credit line".
    re.compile(rf"\b{_ACT}\s+(?:[\w'’-]+\s+){{0,3}}?{_LIMIT}\b", re.I),
    # "The limit should be lowered", "the card ought to be paused".
    re.compile(rf"\b(?:{_LIMIT}|cards?)\b[^.]{{0,40}}\b(?:should|must|ought|needs?|has\s+to|have\s+to|would|could"
               rf"|can|may|is\s+to)\b(?:\s+\w+){{0,2}}?\s+(?:be\s+)?{_ACT}", re.I),
    # "A limit reduction is warranted", "a credit line cut".
    re.compile(rf"\b{_LIMIT}\s+(?:reduction|cut|decrease|increase|freeze|change|adjustment)s?\b", re.I),
    re.compile(r"\b(?:paus|freez|suspend|clos(?!e\s+to\b)|block|cancel)\w*\s+(?:[\w'’-]+\s+){0,3}?cards?\b", re.I),
    re.compile(r"\b(?:keep_limit|reduce_limit|pause_card|request_documents|needs_human)\b", re.I),
    re.compile(r"\brequest(?:s|ed|ing)?\s+(?:the\s+|further\s+|more\s+|supporting\s+)?(?:documents|documentation)\b",
               re.I),
    re.compile(r"\b(?:needs?\s+(?:a\s+)?human|human\s+review|escalat\w+\s+to)\b", re.I),
    # "must have" as an order, not an inference: "the customer must have their limit reduced". The same
    # pattern is in card_service/guardrails.py; tests/_action_phrasings.py holds the cases both must agree on.
    re.compile(r"\b(should|must|ought\s+to|needs?\s+to|has\s+to|have\s+to)\s+(have|get)\s+(its|their|the|his|her)\s+"
               r"([\w'-]+\s+){0,2}?((credit\s+|card\s+)?limits?|cards?|credit\s+lines?)\s+(be\s+)?"
               r"(reduc|lower|cut|decreas|increas|rais|paus|freez|frozen|suspend|clos|block|tighten)\w*", re.I),
    # The subjunctive: "spend suggests that the card limit be reduced". Also in card_service/guardrails.py.
    re.compile(r"\bsuggest\w*\s+(that\s+)?(the\s+|its\s+|their\s+|this\s+)?([\w'-]+\s+)?"
               r"((credit\s+|card\s+)?limits?|cards?|credit\s+lines?|accounts?)\s+(be|is\s+to|should\s+be)\s+"
               r"(reduc|lower|cut|decreas|increas|rais|paus|freez|frozen|suspend|clos|block|tighten)\w*", re.I),
]


def recommends_an_action(text: str) -> bool:
    # "Reduce_the_credit_limit" is words joined by underscores, which \b would read as one word.
    text = text.replace("_", " ")
    return any(pattern.search(text) for pattern in _RECOMMENDATION)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def quotes(text: str, texts: list[str]) -> bool:
    """Whether the text repeats QUOTE_WORDS words in a row of any of the texts, or carries a data tag."""
    if re.search(r"</?(?:memo|text)>", text, re.I):
        return True
    said = " " + " ".join(_words(text)) + " "
    for source in texts:
        words = _words(source)
        for start in range(0, max(0, len(words) - QUOTE_WORDS) + 1):
            run = words[start:start + QUOTE_WORDS]
            if len(run) == QUOTE_WORDS and f" {' '.join(run)} " in said:
                return True
    return False


def clean(text: str, limit: int = 500) -> str:
    return " ".join(text.split())[:limit]


@dataclass
class Checked:
    accepted: list[dict] = field(default_factory=list)  # findings, as JSON
    problems: list[str] = field(default_factory=list)  # told to the model in a repair round
    dropped: list[str] = field(default_factory=list)  # for could_not_determine if they still fail


def check(drafts: list[dict], records: dict[str, dict]) -> Checked:
    """Each drafted finding against the records this run fetched.

    `problems` go back to the model in a repair; `dropped` goes to the Orchestrator, so it repeats nothing the
    model wrote: no claim text, no figure the records do not show, and no id a tool did not return (they are
    counted; "Reduce_the_credit_limit_to_500_now" has the shape of an id).
    """
    out = Checked()
    texts = free_texts(list(records.values()))
    for n, draft in enumerate(drafts, start=1):
        claim = clean(draft["claim"])
        ids = list(dict.fromkeys(draft["evidence_ids"]))
        unknown = [i for i in ids if i not in records]
        cited = [records[i] for i in ids if i in records]
        problems, reasons = [], []
        if unknown:
            # The model is told which (its own text, back to it); the Orchestrator only how many.
            problems.append(f"cites ids no tool returned: {[i[:64] for i in unknown]}")
            reasons.append(f"cites {len(unknown)} id{'s' if len(unknown) != 1 else ''} no tool returned")
        if not cited:
            problems.append("cites no record a tool returned")
            reasons.append(problems[-1])
        if recommends_an_action(claim):
            problems.append("recommends a limit action (on the limit or the card); report facts only")
            reasons.append("recommends a limit action")
        if cited and (bad := figures.not_backed(claim, figures.allowed_figures(cited))):
            problems.append(f"states {bad}, which the cited records {[r['id'] for r in cited]} do not show; "
                            "copy figures as the records show them, or cite the record that shows them")
            reasons.append(f"states figures the cited records {[r['id'] for r in cited]} do not show")
        if quotes(claim, texts):
            problems.append("repeats text a customer wrote; say what the records show in your own words")
            reasons.append("repeats text a customer wrote")
        if problems:
            out.problems.append(f"Finding {n} ('{claim[:90]}'): " + "; ".join(problems) + ".")
            out.dropped.append(f"Finding {n} was left out because it {'; '.join(reasons)[:240]}.")
            continue
        out.accepted.append(Finding(claim=claim, confidence=draft["confidence"],
                                    evidence_ids=[r["id"] for r in cited]).model_dump(mode="json"))
    return out


def side_text_problem(text: str, records: dict[str, dict]) -> str | None:
    """Why a could_not_determine line or a suggested next step the model wrote cannot go out, or None.

    Checked against every record of the run, for money and percent only: such a line names what is missing,
    and "the last 90 days" is a span, not a figure.
    """
    if recommends_an_action(text):
        return "it recommended an action"
    if quotes(text, free_texts(list(records.values()))):
        return "it repeated text a customer wrote"
    allowed = figures.allowed_figures(records.values())
    if [written for token, written in figures.figures(text, marked_only=True).items() if token not in allowed]:
        return "it stated a figure no record shows"
    return None
