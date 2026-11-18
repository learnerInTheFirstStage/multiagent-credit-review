"""What the records of a case meet: the needs of the guidelines, the actions they permit, and what is still open.

Code never picks the guideline or the action. These functions say what records permit and which questions
nobody asked yet. The report check refuses with them (2.A) and the gap pass nudges with them (2.D.3).
A guideline without `needs` is left out of everything here: with the test fixture's guidelines nothing is checked.
"""

from collections.abc import Iterable
from typing import get_args

from app import policy
from app.graph.causes import KIND_TOPIC, MUST_ADDRESS, NEEDS_RECORD, OWN_KINDS, TOPICS
from app.graph.report import Recommendation

_ACTING = tuple(recommendation for recommendation in get_args(Recommendation) if recommendation != "needs_human")
_CATALOGUE = [(tool, topic) for tool, topics in TOPICS.items() for topic in topics]


def _number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _passes(test, value) -> bool:
    if not isinstance(test, dict):
        return value == test
    # A list value passes an "in" test when it holds one of the values.
    if "in" in test and not any(item in test["in"] for item in (value if isinstance(value, list) else [value])):
        return False
    # A bound on a value that is missing or not a number is not met, and never raises.
    if "gte" in test and not (_number(value) and value >= test["gte"]):
        return False
    return "lte" not in test or (_number(value) and value <= test["lte"])


def _matching(need: dict, records: Iterable[dict]) -> list[dict]:
    where = need.get("where") or {}
    return [
        record
        for record in records
        if record["kind"] == need["kind"]
        and all(field in record["data"] and _passes(test, record["data"][field]) for field, test in where.items())
    ]


def _acts(guideline: dict) -> bool:
    return any(recommendation != "needs_human" for recommendation in guideline["allows"])


def _whole(entry: dict, records: list[dict]) -> bool:
    """Whether a question asked its whole topic: no account named, and no dates or the review window's own.

    Code cannot tell which topics a specialist reads the dates on, so any other dates narrow the question.
    payee_history's record_id is how that topic is asked, not a narrowing.
    """
    if entry.get("account_id"):
        return False
    dates = (entry.get("start"), entry.get("end"))
    if dates == (None, None):
        return True
    window = next((record["data"] for record in records if record["kind"] == "cash_change"), {})
    return dates == (window.get("window_start"), window.get("window_end"))


def _sent(asked: list[dict], topic: tuple[str, str], records: list[dict]) -> list[dict]:
    """The questions on a topic that went out whole. A refused one never did, and a narrowed one settles nothing."""
    return [
        entry
        for entry in asked
        if (entry.get("tool"), entry.get("topic")) == topic
        and entry.get("outcome") in ("answered", "no_answer")
        and _whole(entry, records)
    ]


def _narrowed(records: list[dict], asked: list[dict]) -> set[str]:
    """The ids that only narrowed questions returned: part of a topic, which cannot show that a need fails."""
    whole, narrowed = set(), set()
    for entry in asked:
        (whole if _whole(entry, records) else narrowed).update(entry.get("returned", []))
    return narrowed - whole


def _candidates(guideline: dict, records: list[dict]) -> list[dict]:
    """The payments a payee's history could be asked about: those that pass the guideline's own transaction need."""
    found = {
        record["id"]: record
        for need in guideline["needs"]
        if need["kind"] == "bank_transaction"
        for record in _matching(need, records)
    }
    return list(found.values())


def _question(need: dict, guideline: dict, records: list[dict], asked: list[dict]) -> dict | None:
    """The question that would close an open need. None when none can: none_fits, or no payment to ask about."""
    topic = KIND_TOPIC.get(need["kind"])
    if topic is None:
        return None
    ask = {"tool": topic[0], "args": {"topic": topic[1]}}
    if topic in NEEDS_RECORD:
        asked_about = {entry.get("record_id") for entry in _sent(asked, topic, records)}
        left = [record for record in _candidates(guideline, records) if record["id"] not in asked_about]
        if not left:
            return None
        # The largest payment not asked about yet. A payment out is negative.
        largest = max(left, key=lambda record: abs(a) if _number(a := record["data"].get("amount_cents")) else 0)
        ask["args"]["record_id"] = largest["id"]
    return ask


def _open_questions(guidelines: list[dict], records: list[dict], asked: list[dict]) -> list[dict]:
    """For each guideline that is not dead, each open need as the question that would close it. In catalogue order."""
    questions = []
    for guideline in guidelines:
        if not guideline.get("needs"):
            continue
        states = [state(need, guideline, records, asked) for need in guideline["needs"]]
        if "failed" in states:
            continue
        for need, need_state in zip(guideline["needs"], states):
            if need_state != "open":
                continue
            question = _question(need, guideline, records, asked)
            if question and question not in questions:
                questions.append(question)
    return sorted(questions, key=lambda question: _CATALOGUE.index((question["tool"], question["args"]["topic"])))


def _named(ask: dict) -> str:
    args = ask["args"]
    return f"{ask['tool']}, topic {args['topic']}" + (f", record_id {args['record_id']}" if "record_id" in args else "")


def meets(need: dict, records: Iterable[dict]) -> bool:
    """Whether the records hold the fact a need asks for.

    The records of the need's kind whose data passes every test of `where`. A missing field fails. With
    `share_at_least_bp` their shares of the fall must add up to it. A record counts once, however often it is passed.
    """
    matching = {record["id"]: record for record in _matching(need, records)}
    if not matching:
        return False
    line = need.get("share_at_least_bp")
    shares = (record["data"].get("share_of_fall_bp") for record in matching.values())
    return line is None or sum(share for share in shares if _number(share)) >= line


def state(need: dict, guideline: dict, records: list[dict], asked: list[dict]) -> str:
    """Met, failed (no question can still meet it) or open, given the ledger and what was asked (2.D.3).

    A guideline with a failed need is dead. none_fits is no record: it is never met here and no question closes it.
    What a narrowed question returned can meet a need but never fail one.
    """
    kind, where = need["kind"], need.get("where") or {}
    if KIND_TOPIC.get(kind) in NEEDS_RECORD:
        # A payee's history settles the need only for a payment the guideline is about. Bank keeps one for every
        # payee, and a running cost's history says nothing about the vendor payment G-02 asks after.
        about = {record["id"] for record in _candidates(guideline, records)}
        records = [record for record in records if record["kind"] != kind or record["data"].get("about") in about]
    if meets(need, records):
        return "met"
    narrowed = _narrowed(records, asked)
    if kind in OWN_KINDS or any(record["kind"] == kind and record["id"] not in narrowed for record in records):
        return "failed"
    topic = KIND_TOPIC.get(kind)
    if topic in NEEDS_RECORD:
        # Asked about every payment it could be about, and there was one at least.
        asked_about = {entry.get("record_id") for entry in _sent(asked, topic, records)}
        candidates = _candidates(guideline, records)
        if candidates and all(record["id"] in asked_about for record in candidates):
            return "failed"
    elif topic is not None and _sent(asked, topic, records):
        return "failed"
    # The signal already rules it out.
    for record in records:
        data = record["data"]
        if (
            kind == "card_repayment"
            and record["kind"] == "repayment_statuses"
            and "status" in where
            and isinstance(data.get("statuses"), list)
            and not any(_passes(where["status"], status) for status in data["statuses"])
        ):
            return "failed"
        if kind == "linked_account" and record["kind"] == "account_sync" and data.get("any_behind") is False:
            return "failed"
    return "open"


def unmet(
    guideline: dict, cited_records: list[dict], *, guidelines: list[dict], known: list[dict], asked: list[dict]
) -> list[dict]:
    """The needs of a guideline the cited records do not meet.

    none_fits is judged on every record of the case (`known`) and on what was asked, and counts as met
    while policy.RULES["none_fits_is_checked"] is off.
    """
    found = []
    for need in guideline.get("needs", []):
        if need["kind"] == "none_fits":
            if policy.RULES["none_fits_is_checked"] and (
                permitted(guidelines, known) or unsettled(guidelines, known, asked)
            ):
                found.append(need)
        elif not meets(need, cited_records):
            found.append(need)
    return found


def triggers(guidelines: list[dict], records: Iterable[dict]) -> list[dict]:
    """The must-address entries that apply and are met, each a copy with the ids of the records that meet it.

    An entry applies when some guideline has a need of its covered_by kind, and none does while
    policy.RULES["must_address"] is off.
    """
    if not policy.RULES["must_address"]:
        return []
    records = list(records)
    declared = {need["kind"] for guideline in guidelines for need in guideline.get("needs", [])}
    found = []
    for entry in MUST_ADDRESS:
        ids = list(dict.fromkeys(record["id"] for record in _matching(entry["trigger"], records)))
        if entry["covered_by"] in declared and ids:
            found.append({**entry, "record_ids": ids})
    return found


def permitted(guidelines: list[dict], records: Iterable[dict]) -> dict[str, list[str]]:
    """Each acting recommendation the records could carry, with the guidelines that could carry it.

    Permitted when some guideline that allows it has every need met, and every must-address entry met
    by the records is covered by a need of one of those guidelines.
    """
    records = list(records)
    met = [g for g in guidelines if g.get("needs") and all(meets(need, records) for need in g["needs"])]
    covers = [entry["covered_by"] for entry in triggers(guidelines, records)]
    found = {}
    for recommendation in _ACTING:
        carrying = [g for g in met if recommendation in g["allows"]]
        held = {need["kind"] for g in carrying for need in g["needs"]}
        if carrying and all(kind in held for kind in covers):
            found[recommendation] = [g["id"] for g in carrying]
    return found


def unsettled(guidelines: list[dict], records: list[dict], asked: list[dict]) -> list[dict]:
    """The questions that would close the open needs of the guidelines that allow an action and are not dead."""
    return _open_questions([g for g in guidelines if _acts(g)], records, asked)


def hand_over(guidelines: list[dict], records: list[dict], asked: list[dict]) -> list[str]:
    """The guidelines that allow only needs_human and have every need met, none_fits included."""
    return [
        g["id"]
        for g in guidelines
        if g.get("needs")
        and not _acts(g)
        and not unmet(g, records, guidelines=guidelines, known=records, asked=asked)
    ]


def code_gaps(guidelines: list[dict], records: list[dict], asked: list[dict]) -> list[dict]:
    """The questions nobody asked that could still decide the case, one entry per question (2.D.3).

    No text names a guideline: the investigator is told what was not asked, never which guideline it would complete.
    """
    gaps = []

    def add(rule: str, text: str, ask: dict) -> None:
        # Two rules that name the same question give one gap, under the first.
        if all(gap["ask"] != ask for gap in gaps):
            gaps.append({"rule": rule, "text": f"{text} Not yet asked: {_named(ask)}.", "ask": ask})

    # G1: something a report that acts may not pass over, nobody asked the topic that would show it, and no record
    # of that kind came back, even from a narrowed question.
    for entry in triggers(guidelines, records):
        tool, topic = KIND_TOPIC[entry["covered_by"]]
        if not any(record["kind"] == entry["covered_by"] for record in records) and not _sent(
            asked, (tool, topic), records
        ):
            add("G1", f"{entry['says']} ({', '.join(entry['record_ids'])}).", {"tool": tool, "args": {"topic": topic}})

    declared = [g for g in guidelines if g.get("needs")]
    states = [(g, [state(need, g, records, asked) for need in g["needs"]]) for g in declared]

    # G2: nothing is permitted, and a live guideline has a need met by what a specialist returned and one still open.
    # A need met by the case's own records does not count: they are there before anything is asked.
    if not permitted(guidelines, records):
        for g, need_states in states:
            if "failed" in need_states or not any(
                s == "met" and need["kind"] in KIND_TOPIC for need, s in zip(g["needs"], need_states)
            ):
                continue
            for need, s in zip(g["needs"], need_states):
                if s == "open" and (ask := _question(need, g, records, asked)):
                    # Not "half": a size word is a figure under 6.1, and a gap text holds none.
                    add("G2", "A guideline is partly supported.", ask)

    # G3: nothing found yet and no acting guideline is fully met. What could still decide it, one topic per gap,
    # three at most: first for the guidelines that allow an action, then for those that hand the case over.
    if not gaps and not any(_acts(g) and all(s == "met" for s in need_states) for g, need_states in states):
        hand_overs = [g for g in declared if not _acts(g)]
        topics = []
        for ask in [*unsettled(guidelines, records, asked), *_open_questions(hand_overs, records, asked)]:
            topic = (ask["tool"], ask["args"]["topic"])
            if topic not in topics and len(topics) < 3:
                topics.append(topic)
                add("G3", "No guideline's needs are met yet.", ask)
    return gaps
