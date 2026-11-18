"""Figures in a text, and whether the records it cites show them (6.1).

`figures` reads every amount, share, date, count over twelve, multiple and size in
a text and gives each a canonical token, so "$230,000", "$230K" and "$0.23M" are one
figure. A figure is backed when a record shows the same token. The allow-list is
made from `facts.shown`, the form every model reads, never from keys and never from
text a customer or a person wrote.

Tokens: m: money, n: any other number, p: percent, d: date, y: year, w: a multiple
or a size in words. A w: token is never backed: the records show ratios as percent.
"""

import re
from collections.abc import Callable, Iterable
from decimal import Decimal

from app.graph import facts

# Step 1. Ids hold digits that are not figures. Set in emphasis ("_c_010_") they are ids still.
_IDS = re.compile(
    r"(?<![A-Za-z\d])"
    r"(?:(?:txn|acct|bal|agg|rep|ctx|crd|sig|hist|xs|case|seed)_[A-Za-z\d_]+|c_\d{3}|G-\d{2}[a-z]?)"
    r"(?![A-Za-z\d])"
)

# Not inside a word, and not the decimals of a number. A full stop after a letter ends a sentence: "fell.90,000".
_FREE = r"(?<!\w)(?<!\d\.)"
_NUM = r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
_YEAR = r"(?:19\d{2}|20\d{2}|2100)"
# Not the start of an amount, a share or a longer number.
_NOT_AN_AMOUNT = (
    r"(?![\w%]|[.,]\d|\s*%|\s*(?i:percent|per\s+cent|pct|dollars?|usd|hundred|thousand|million|billion)\b)"
)
# In any case, except that "may" is the verb: only "May" and "MAY" are the month.
_MONTH = (
    r"(?P<month>(?i:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|June?|July?|Aug(?:ust)?"
    r"|Sept?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)|May|MAY)\b\.?"
)
_DAY = r"(?P<day>3[01]|[12]\d|0?[1-9])(?:st|nd|rd|th)?"
_WRITTEN_YEAR = rf"(?:,?\s+(?P<year>{_YEAR}){_NOT_AN_AMOUNT})?"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Step 2. Dates.
_ISO_DATE = re.compile(r"(?<![\w-])(\d{4})-(\d{2})-(\d{2})(?![\w-])")
_MONTH_DAY = re.compile(rf"\b{_MONTH}\s+{_DAY}{_NOT_AN_AMOUNT}{_WRITTEN_YEAR}")
_DAY_MONTH = re.compile(rf"{_FREE}(?<![,$]){_DAY}\s+(?:of\s+)?{_MONTH}{_WRITTEN_YEAR}")
_BARE_YEAR = re.compile(rf"{_FREE}(?<![$,]){_YEAR}{_NOT_AN_AMOUNT}")

# Step 3. Multiples: a ratio the model worked out.
_TWELVE = "zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
_WORDS = (
    rf"{_TWELVE}|thirteen|fourteen|fifteen|sixteen"
    "|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand"
)
_COUNT = rf"(?:\d+(?:\.\d+)?|{_WORDS})"
_COMPARED = (
    "the|as|its|their|that|what|more|less|larger|smaller|higher|lower|bigger|greater|normal|usual|typical|average"
)
_MULTIPLE = re.compile(
    rf"{_FREE}{_COUNT}-?(?:x|fold)\b"
    rf"|{_FREE}\d+(?:\.\d+)?\s?×"
    # "x2" written before the number. Not the x of a word or a hex number ("box2", "0x2F"). "×2" is _TIMES_FIRST.
    r"|(?<!\w)x\d+(?:\.\d+)?"
    rf"|\bfactor\s+of\s+{_COUNT}\b"
    r"|\b(?:doubl|tripl|quadrupl)(?:e|es|ed|ing)\b"
    rf"|{_FREE}{_COUNT}\s+times\s+(?:{_COMPARED})\b"
    rf"|\b(?:twice|thrice)\s+(?:{_COMPARED})\b",
    re.IGNORECASE,
)

# Step 4. Percents.
_PERCENT = re.compile(rf"{_FREE}{_NUM}\s*(?:%|(?i:percent|per\s+cent|pct)\b)")

# Step 5. Numbers: money when written with $ or dollars, any other number otherwise.
_SCALE = r"(?:(?P<letter>[kKmMB]|bn)\b|\s*(?P<word>(?i:hundred|thousand|million|billion))\b)"
_SCALES = {
    "k": 10**3, "m": 10**6, "b": 10**9, "bn": 10**9,
    "hundred": 100, "thousand": 10**3, "million": 10**6, "billion": 10**9,
}
# Read before the dates, so "$ 2000" is never a year. A number with a percent after it is left to _PERCENT.
_DOLLARS = re.compile(
    rf"\$\s*-?{_NUM}(?![\d.,]*\s*(?:%|(?i:percent|per\s+cent|pct)\b)){_SCALE}?(?:\s*(?i:dollars?|usd)\b)?"
)
# A multiple written with × before its number, read before the dates: "×2 May 2026", "× 2025".
_TIMES_FIRST = re.compile(r"×\s*\d+(?:\.\d+)?")
_DOLLAR_WORDS = re.compile(rf"{_FREE}{_NUM}{_SCALE}?\s*(?i:dollars?|usd)\b")
_ORDINAL = re.compile(rf"{_FREE}(?P<number>\d+)(?:st|nd|rd|th)\b")
_NUMBER = re.compile(rf"{_FREE}{_NUM}{_SCALE}?(?!\w)")

# Step 6. Sizes in words.
_FRACTIONS = "third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth"
_SIZE = re.compile(
    # At most eight words in front: a figure written out needs no more, and a longer run is retried from every word.
    rf"(?:\b(?:{_WORDS})[\s-]+(?:and\s+)?){{0,8}}\b(?P<size>hundred|thousand|million|billion|percent|pct|halved)s?\b"
    r"|(?<!\bfirst[\s-])(?<!\bsecond[\s-])(?<!\blatter[\s-])\b(?P<half>half|halves)\b"
    r"|(?:\b(?:a|an|one|two|three)|#)[\s-]+(?P<quarter>quarters?)\b(?!\s+(?:ago|earlier|later)\b)"
    # "two-thirds" but not "twenty-third", an ordinal.
    rf"|\b(?:{_TWELVE})-(?P<hyphened>(?:{_FRACTIONS})s?)\b"
    rf"|\b(?P<plural>(?:{_FRACTIONS})s)\b"
    rf"|\b(?:a|an|one)\s+(?P<single>{_FRACTIONS})\s+of\b",
    re.IGNORECASE,
)

# A number directly followed by one of these is a span of time, which a question or a gap may name.
# Not a word that only starts with one: "90,000 year-end" is an amount.
_SPAN = re.compile(r"[\s-]*(?:day|week|month|year)s?(?![\w-])", re.IGNORECASE)

# One figure: where it starts, the token, the figure as written, and whether a span of time follows it.
_Found = tuple[int, str, str, bool]


def _canon(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _amount(match: re.Match) -> Decimal:
    value = Decimal(match["number"].replace(",", ""))
    scale = match.groupdict().get("letter") or match.groupdict().get("word")
    return value * _SCALES[scale.lower()] if scale else value


def _take(text: str, pattern: re.Pattern, read: Callable[[re.Match], str | None], found: list, blank=" ") -> str:
    """Note what `read` makes of each match and blank the match, so that no later step reads it again.

    Blanking keeps the length, so every offset is an offset in the text as written.
    """

    def note(match: re.Match) -> str:
        if (token := read(match)) is not None:
            found.append((match.start(), match.end(), token, bool(_SPAN.match(text, match.end()))))
        return blank * len(match.group())

    return pattern.sub(note, text)


def _named_date(match: re.Match) -> str:
    day = f"{_MONTHS.index(match['month'][:3].title()) + 1:02d}-{int(match['day']):02d}"
    return f"d:{match['year']}-{day}" if match["year"] else f"d:{day}"


def _number(match: re.Match, *, marked_only: bool) -> str | None:
    value = _amount(match)
    scaled = bool(match.groupdict().get("letter") or match.groupdict().get("word"))
    if marked_only and not scaled:
        return None
    # Whole numbers up to twelve are counts, and pass unchecked (step 7).
    if not scaled and value == value.to_integral_value() and 0 <= value <= 12:
        return None
    return f"n:{_canon(value)}"


def _size(match: re.Match) -> str:
    return f"w:{next(word for word in match.groupdict().values() if word).lower()}"


def _scan(text: str, *, marked_only: bool = False) -> list[_Found]:
    written, found = text, []
    text = _take(text, _IDS, lambda match: None, found)
    # With the ids gone, an underscore is markdown: "_90,000_" is 90,000.
    text = text.replace("_", " ")
    # Before the dates and years: the number after a $ or a × is never one. A number taken here, as by
    # every money and number step, leaves a # behind, so that step 6 can tell "2 quarters" from "the quarter".
    text = _take(text, _DOLLARS, lambda match: f"m:{_canon(_amount(match))}", found, blank="#")
    if not marked_only:
        text = _take(text, _TIMES_FIRST, lambda match: "w:times", found)
        text = _take(text, _ISO_DATE, lambda match: f"d:{match.group()}", found)
        text = _take(text, _MONTH_DAY, _named_date, found)
        text = _take(text, _DAY_MONTH, _named_date, found)
        text = _take(text, _BARE_YEAR, lambda match: f"y:{match.group()}", found)
        text = _take(text, _MULTIPLE, lambda match: "w:times", found)
    text = _take(text, _PERCENT, lambda match: f"p:{_canon(_amount(match))}", found)
    # An ordinal is read as its number. It leaves no # behind: "the 3rd quarter" is a period, not a size.
    text = _take(text, _ORDINAL, lambda match: _number(match, marked_only=marked_only), found)
    text = _take(text, _DOLLAR_WORDS, lambda match: f"m:{_canon(_amount(match))}", found, blank="#")
    text = _take(text, _NUMBER, lambda match: _number(match, marked_only=marked_only), found, blank="#")
    if not marked_only:
        # Step 7: the words zero to twelve and the ordinals first to twelfth that this leaves are not figures.
        _take(text, _SIZE, _size, found)
    return [(start, token, written[start:end], span) for start, end, token, span in sorted(found)]


def figures(text: str, *, marked_only: bool = False) -> dict[str, str]:
    """Canonical token -> the figure as first written, in the order written.

    marked_only=True reads only percents, and amounts written with $, dollars, K, M,
    thousand or million: for a next step, where "within 14 days" is a deadline.
    """
    found: dict[str, str] = {}
    for _, token, written, _ in _scan(text, marked_only=marked_only):
        found.setdefault(token, written)
    return found


def _allow(value, allowed: set[str]) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _allow(item, allowed)
    elif isinstance(value, list):
        for item in value:
            _allow(item, allowed)
    elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
        tokens = {token for _, token, _, _ in _scan(str(value))}
        if not isinstance(value, str):
            # A bare number is a number, and whatever the tokenizer reads in it as shown: 1950 is a year too.
            tokens.add(f"n:{_canon(Decimal(str(value)))}")
        for token in tokens:
            # A multiple or a size in words is never backed, not even by a record that holds the word ("doubled").
            if token.startswith("w:"):
                continue
            allowed.add(token)
            if token.startswith("m:"):
                # A report may write "230,000" for a shown "$230,000". Not the other way round.
                allowed.add(f"n:{token[2:]}")
            elif token.startswith("d:") and len(token) == len("d:2026-11-05"):
                allowed |= {f"d:{token[7:]}", f"y:{token[2:6]}"}


def allowed_figures(records: Iterable[dict]) -> set[str]:
    """The tokens the records show: from the values of `facts.shown`, never from keys or withheld text."""
    allowed: set[str] = set()
    for record in records:
        _allow(facts.shown(record)["data"], allowed)
    return allowed


def not_backed(text: str, allowed: set[str], *, loose: bool = False) -> list[str]:
    """The figures of a text that are not in `allowed`, as first written.

    loose=True lets dates through, and a number directly followed by days, weeks,
    months or years: for a question and for a gap a model wrote, where "the last 90
    days" names a span and states nothing about the records.
    """
    refused: dict[str, str] = {}
    for _, token, written, span in _scan(text):
        if token in allowed or (loose and (token[0] in "dy" or (token[0] == "n" and span))):
            continue
        refused.setdefault(token, written)
    return list(refused.values())


# The parts of a report whose text states figures, and the field that holds the text.
_ITEMS = (("evidence", "claim"), ("ruled_out", "reason"), ("other_findings", "claim"), ("review_reasons", "finding"))


def made_up(report: dict, ledger: dict[str, dict]) -> list[tuple[str, str, str | None]]:
    """Every figure of a report that the records it cites for it do not show.

    Each is (part, the figure as written, the id of a record of this case that shows
    it but is not cited there, or None). Parts, in this order: "summary", then
    "evidence 1", "ruled_out 1", "other_findings 1", "review_reasons 1" and so on
    (numbered from 1), then "next_action".

    An item is checked against the records its own evidence_ids name, the summary
    against every record the report cites, and next_action against the same for
    money and percent only. An item that cites nothing may state no figure. An id
    that is not in the ledger backs nothing: the check reports it on its own.
    """
    cited = [record_id for part, _ in _ITEMS for item in report[part] for record_id in item["evidence_ids"]]
    checks = [("summary", report["summary"], cited, False)]
    for part, field in _ITEMS:
        checks += [(f"{part} {n}", item[field], item["evidence_ids"], False) for n, item in enumerate(report[part], 1)]
    checks.append(("next_action", report.get("next_action"), cited, True))

    shows: dict[str, set[str]] = {}

    def shown_by(record_id: str) -> set[str]:
        if record_id not in shows:
            shows[record_id] = allowed_figures([ledger[record_id]])
        return shows[record_id]

    refused = []
    for part, text, ids, marked_only in checks:
        if not text:
            continue
        allowed = set().union(*(shown_by(record_id) for record_id in ids if record_id in ledger))
        for token, written in figures(text, marked_only=marked_only).items():
            if token in allowed:
                continue
            elsewhere = next((other for other in ledger if other not in ids and token in shown_by(other)), None)
            refused.append((part, written, elsewhere))
    return refused
