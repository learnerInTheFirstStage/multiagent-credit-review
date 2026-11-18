"""Grading for the golden dataset. Code only: no model judges anything here.

A run is one case taken through the system once. A case is right only when
every one of its runs is right, because an analyst sees one run, not the best of three.
"""

import re
from collections import Counter
from math import sqrt
from statistics import median

# How hard an action is on the customer. needs_human is not an action, so it has no place.
STRICTNESS = {"keep_limit": 0, "request_documents": 1, "reduce_limit": 2, "pause_card": 3}
# The actions that take spending power away from the customer.
CUTS = ("reduce_limit", "pause_card")
HAND_OVER = "needs_human"
CONFIDENCE = ("high", "medium", "low")
# As app/agents/cash_flow/guardrails.py QUOTE_WORDS: this many words in a row of a customer's text is a quote of it.
QUOTE_WORDS = 6
CHECKS = ("recommendation", "cause", "hand_over", "cites")
# What each baseline would recommend for every case, without looking at it.
BASELINES = {"The engine alone (always cuts)": "reduce_limit", "Always hand over": "needs_human"}
# The golden set's words for how one case is set against another.
LINKS = {
    "twin_of": "twin of",
    "same_as": "same as",
    "milder_than": "milder than",
    "history_twin_of": "history twin of",
    "history_same_as": "history copy of",
    "same_history_as": "same history as",
}
# How the summary of a report starts when two drafts failed the service's own check. The service
# then hands the case to a person with no cause and no evidence: no answer was reached.
GAVE_UP = "The report could not be completed."
# Records code gives a case itself. No question has to reach them.
OWN_RECORDS = ("hist_", "sig_", "xs_")
# The outcomes of a question that was sent. A refused call never reached a specialist.
SENT = ("answered", "no_answer")


def gave_up(report: dict) -> bool:
    return report["summary"].startswith(GAVE_UP)


def cited(report: dict) -> set[str]:
    parts = ("evidence", "ruled_out", "other_findings", "review_reasons")
    return {id for part in parts for item in report.get(part) or [] for id in item["evidence_ids"]}


# An entry of must_cite or must_reach is an id (a string) or a spec: {kind, where}, with the tests of a guideline's
# needs. A spec names a record by what it says, so any specialist's id for it counts.


def _number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _passes(test, value) -> bool:
    """One test of a spec's `where`, as app/graph/needs.py reads it. A test there agrees with this one."""
    if not isinstance(test, dict):
        return value == test
    if "in" in test and not any(item in test["in"] for item in (value if isinstance(value, list) else [value])):
        return False
    if "gte" in test and not (_number(value) and value >= test["gte"]):
        return False
    return "lte" not in test or (_number(value) and value <= test["lte"])


def matches(record: dict, spec: dict) -> bool:
    """Whether a record is of the spec's kind and passes every test of its `where`. A missing field fails."""
    data = record.get("data") or {}
    return record.get("kind") == spec["kind"] and all(
        field in data and _passes(test, data[field]) for field, test in (spec.get("where") or {}).items()
    )


def resolve(entry, records: list[dict]) -> list[str]:
    """The ids an entry names: an id names itself, a spec every record it matches."""
    if isinstance(entry, str):
        return [entry]
    return [record["id"] for record in records if matches(record, entry)]


def _value(value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, dict):
        bounds = {"gte": "at least", "lte": "at most"}
        words = [" or ".join(_value(item) for item in value["in"])] if "in" in value else []
        return " and ".join(words + [f"{bounds[key]} {_value(value[key])}" for key in bounds if key in value])
    return str(value)


def label(entry) -> str:
    """An entry in words: "card_repayment where status is failed and paid_since is no"."""
    if isinstance(entry, str):
        return entry
    tests = [f"{field} is {_value(test)}" for field, test in (entry.get("where") or {}).items()]
    return entry["kind"] + (" where " + " and ".join(tests) if tests else "")


def _graded_without(entries: list, records: list[dict] | None) -> bool:
    """A spec cannot be graded without the records its ids belong to."""
    return records is None and any(isinstance(entry, dict) for entry in entries)


def grade(case: dict, report: dict, records: list[dict] | None = None) -> dict:
    """One run against the accepted answers. A check the case does not have, or cannot be graded, is None.

    `records` are the case's records from its path page. A spec in must_cite needs them; without them cites is None.
    """
    if gave_up(report):
        # Wrong on everything, also for a case that should go to a person: it got there by failing.
        return {"recommendation": False, "cause": False, "hand_over": False, "cites": False if "must_cite" in case else None}
    cites = None
    if "must_cite" in case and not _graded_without(case["must_cite"], records):
        ids = cited(report)
        cites = all(set(resolve(entry, records or [])) & ids for entry in case["must_cite"])
    return {
        "recommendation": report["recommendation"] in case["recommendations"],
        "cause": report["likely_cause"] in case["causes"],
        # Handing over a case the agent should settle is a failure too, and the other way round.
        "hand_over": (report["recommendation"] == "needs_human") == ("needs_human" in case["recommendations"]),
        "cites": cites,
    }


def grade_path(case: dict, report: dict, path: dict) -> dict:
    """How a run reached its answer, from the case's path page (spec 8.5). Every key of the path is read with .get."""
    questions, drafts, gaps = path.get("questions", []), path.get("drafts", []), path.get("gaps", [])
    records = path.get("records")
    must_reach = case.get(
        "must_reach",
        [entry for entry in case.get("must_cite", []) if not (isinstance(entry, str) and entry.startswith(OWN_RECORDS))],
    )
    returned = {cid for question in questions for cid in question.get("returned", [])}
    reached = None
    if must_reach and not _graded_without(must_reach, records):
        reached = all(set(resolve(entry, records or [])) & returned for entry in must_reach)
    final, given_up = grade(case, report, records), gave_up(report)
    # The listed guidelines are a joint set (c_111 rests on G-08a and G-01): citing any of them, and no other, is right.
    rests_on, listed = set(report.get("guideline_ids") or []), set(case.get("guidelines", []))
    guideline = None if "guidelines" not in case or given_up else bool(rests_on) and rests_on <= listed

    first = drafts[0] if drafts else {}
    first_draft = None
    if first.get("recommendation"):  # a draft that was not a report has no labels
        issued = {
            "summary": "",
            "likely_cause": first.get("likely_cause"),
            # A report that passes with confidence low reaches the analyst as a hand-over.
            "recommendation": "needs_human" if first.get("confidence") == "low" else first["recommendation"],
            "evidence": [{"evidence_ids": first.get("cited", [])}],
        }
        first_draft = grade(case, issued, records)

    def nudged_to(index: int) -> bool:
        """Whether a gap pass before question `index` named its topic, and its record when the gap named one."""
        question = questions[index]
        return any(
            gap.get("at") is not None and gap["at"] <= index
            and ask.get("tool") == question.get("tool") and ask.get("topic") == question.get("topic")
            and ask.get("record_id") in (None, question.get("record_id"))
            for gap in gaps
            for ask in gap.get("asks", [])
        )  # fmt: skip

    # The id each entry was reached by, at the first question that returned one it names.
    nudged = []
    if reached is not None:
        for entry in must_reach:
            named = set(resolve(entry, records or []))
            index, cid = next(
                ((i, cid) for i, question in enumerate(questions) for cid in question.get("returned", []) if cid in named),
                (None, None),
            )
            if index is not None and nudged_to(index):
                nudged.append(cid)

    codes = (drafts[-1].get("codes", []) if drafts else []) if given_up else None
    if given_up:
        why = "gave up: " + ", ".join(dict.fromkeys(codes)) if codes else "gave up"
    elif reached is False:
        why = "path"
    elif final["cites"] is False:
        why = "evidence"
    elif not final["cause"]:
        why = "cause"
    elif guideline is False:
        why = "guideline"
    elif not final["recommendation"]:
        why = "action"
    else:
        why = "right"
    return {
        "reached": reached,
        "guideline": guideline,
        "questions": sum(1 for question in questions if question.get("outcome") in SENT),
        # Every call refused before it was sent. The graph's own counter stops at MAX_REFUSALS (spec 0.3, item 15).
        "refused": sum(1 for question in questions if question.get("outcome") == "refused"),
        "searches": sum(1 for search in path.get("searches", []) if not search.get("refused")),
        "first_draft": first_draft,
        "first_draft_passed": first.get("result") in ("passed", "passed_low_confidence") if drafts else None,
        "nudged": nudged,
        "out_of_rounds": bool(path.get("out_of_rounds")),
        "gave_up_codes": codes,
        "handed_over_by": path.get("handed_over_by"),
        "why": why,
    }


def at_first_draft(run: dict) -> dict:
    """A run as its first checked draft would have been graded. A first draft that was not a report is wrong on every
    check the case has. Without a path, or with no draft logged, the run has no grades."""
    graded = run.get("path_grades") or {}
    if graded.get("first_draft_passed") is None:
        return {}
    first = graded["first_draft"] or {check: None if run["grades"][check] is None else False for check in CHECKS}
    return {"grades": first}


def right_every_run(runs: list[dict], check: str, reps: int) -> bool | None:
    """None when the case does not have the check, or a run is missing or ended in an error."""
    results = [run["grades"][check] for run in runs if "grades" in run]
    if len(results) < reps or None in results:
        return None
    return all(results)


def compare(link: str, mine: dict, other: dict) -> bool | None:
    """One run of a case against the same-numbered run of the case it is set against."""
    if "grades" not in mine or "grades" not in other:
        return None
    both_right = mine["grades"]["recommendation"] and other["grades"]["recommendation"]
    # One fact apart, another history, or the same history with other facts today: the agent has to get both right.
    if link in ("twin_of", "history_twin_of", "same_history_as"):
        return both_right
    got, base = mine["report"]["recommendation"], other["report"]["recommendation"]
    if link == "history_same_as":  # history that must change nothing: both right, and the same answer
        return both_right and got == base
    if link == "same_as":  # something irrelevant was added: nothing may change
        return got == base and not gave_up(mine["report"]) and not gave_up(other["report"])
    if got not in STRICTNESS or base not in STRICTNESS:
        return None
    return STRICTNESS[got] <= STRICTNESS[base]  # milder_than: never stricter than the original


def wilson(passed: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """The 95% interval for a share of n cases. Runs of one case are not independent, so n is cases."""
    if n == 0:
        return 0.0, 1.0
    share = passed / n
    centre = (share + z * z / (2 * n)) / (1 + z * z / n)
    half = z * sqrt(share * (1 - share) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def _share(passed: int, n: int) -> str:
    if n == 0:
        return "none to count"
    low, high = wilson(passed, n)
    return f"{passed} of {n} ({passed / n:.0%}, 95% interval {low:.0%} to {high:.0%})"


def _mark(value: bool | None) -> str:
    return {True: "yes", False: "NO", None: ""}[value]


def summary(
    golden: dict,
    runs: list[dict],
    requests: list[dict],
    reps: int,
    about: dict,
    steering: dict[str, list[str]] | None = None,
    usage_note: str | None = None,
) -> str:
    """The results as a document. `runs` holds one record per case and repetition.

    `steering` holds, per customer, the text its data carries to steer the reviewer (run.py steered()), and
    `usage_note` where usage came from or why it was not recorded. Each is None when the runner did not give it."""
    cases = golden["customers"]
    by_case = {case["id"]: sorted((r for r in runs if r["case"] == case["id"]), key=lambda r: r["rep"]) for case in cases}
    ran = [case for case in cases if by_case[case["id"]]]
    errors = [run for run in runs if "error" in run]
    out = [f"# Golden set results: {about['run_id']}", ""]
    out += [f"- {key}: {value}" for key, value in about.items() if key != "run_id"]
    out += [
        f"- {len(ran)} cases, {reps} runs each. {len(errors)} of {len(runs)} runs ended in an error or were not run. "
        "That is not counted as a wrong answer: a case with such a run is left out of the shares below. "
        "A report the service could not complete in two drafts is counted as a wrong answer.",
        "",
        "## How often the answer was right in every run",
        "",
        "| | Recommendation | Cause | Handed over when it should, and only then | Cited the evidence the answer turns on |",
        "|---|---|---|---|---|",
    ]
    for title, chosen in (("All cases", ran), ("Held out only", [c for c in ran if c["split"] == "held_out"])):
        cells = []
        for check in CHECKS:
            results = [right_every_run(by_case[case["id"]], check, reps) for case in chosen]
            counted = [result for result in results if result is not None]
            cells.append(_share(sum(counted), len(counted)))
        out.append(f"| {title} | " + " | ".join(cells) + " |")
    counted = [case for case in ran if right_every_run(by_case[case["id"]], "recommendation", reps) is not None]
    out += ["", "Against doing nothing clever, on the cases counted for the recommendation above:", ""]
    for title, always in BASELINES.items():
        right = sum(always in case["recommendations"] for case in counted)
        out.append(f"- {title}: recommendation right for {_share(right, len(counted))}")

    out += [
        "",
        "## Case by case",
        "",
        "| Case | Kind | Split | Accepted | Recommended, run by run | Cause, run by run | Recommendation | Cause | Hand over | Cites |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for case in ran:
        mine = by_case[case["id"]]
        reports = [run.get("report") for run in mine]
        said = ["error" if r is None else "gave up" if gave_up(r) else r["recommendation"] for r in reports]
        causes = ["error" if r is None else "gave up" if gave_up(r) else r["likely_cause"] for r in reports]
        marks = " | ".join(_mark(right_every_run(mine, check, reps)) for check in CHECKS)
        out.append(
            f"| {case['id']} | {case['category']} | {case['split']} | {' or '.join(case['recommendations'])} "
            f"| {', '.join(said)} | {', '.join(causes)} | {marks} |"
        )

    linked = [(case, link) for case in ran for link in LINKS if link in case and by_case.get(case[link])]
    if linked:
        out += ["", "## Cases set against each other", "", "| Case | Set against | Held in |", "|---|---|---|"]
        seen = set()
        for case, link in linked:
            pair = frozenset((case["id"], case[link]))
            if link == "twin_of" and pair in seen:  # twins name each other
                continue
            seen.add(pair)
            results = [compare(link, mine, other) for mine, other in zip(by_case[case["id"]], by_case[case[link]])]
            counted = [result for result in results if result is not None]
            out.append(f"| {case['id']} | {LINKS[link]} {case[link]} | {sum(counted)} of {len(counted)} paired runs |")

    if requests:
        out += ["", "## Requests the service has to refuse", "", "| Request | What is sent | Expected | Got | Right |", "|---|---|---|---|---|"]
        for request in requests:
            expected = ", ".join(f"{key} {value}" for key, value in request["expect"].items())
            got = request.get("error") or ", ".join(f"{key} {value}" for key, value in request["got"].items())
            out.append(f"| {request['id']} | {request['what']} | {expected} | {got} | {_mark(request.get('passed'))} |")

    if errors:
        out += ["", "## Runs that ended in an error", ""]
        out += [f"- {run['case']} run {run['rep']}: {run['error']}" for run in errors]
    if any("path_grades" in run for run in runs):  # only then, so a summary of runs without a path is as it was
        out += _path_sections(ran, by_case, reps)
    pairs = [(case, run) for case in ran for run in by_case[case["id"]]]
    out += _run_section(pairs, steering, usage_note)  # last, so nothing above it moves
    return "\n".join(out) + "\n"


def _path_sections(ran: list[dict], by_case: dict[str, list[dict]], reps: int) -> list[str]:
    """What the summary adds when the runs carry their question path (spec 8.5)."""
    pathed = [(case, run) for case in ran for run in by_case[case["id"]] if "path_grades" in run]

    def name(case: dict, run: dict) -> str:
        return f"{case['id']} run {run['rep']}"

    def listed(names: list[str]) -> str:
        return f"{len(names)} runs" + (f" ({', '.join(names)})." if names else ".")

    out = [
        "",
        "## Right on the first draft",
        "",
        "The checks of the first table on the first draft the check saw, as the service would have issued it, and "
        "beside each the share for the final report on the same cases. The first draft is what the model did. The "
        "final report is what the model and the check did together: a draft the check corrected is not the model "
        "being right.",
        "",
        "| | Recommendation | Cause | Handed over when it should, and only then | Cited the evidence the answer turns on |",
        "|---|---|---|---|---|",
    ]
    held_out = [case for case in ran if case["split"] == "held_out"]
    rows = (
        ("All cases", ran),
        ("Held out only", held_out),
        # A dev case whose records and accepted answer this case shares: tuning on that case is tuning on this one.
        ("Held out, without the look-alikes of a dev case", [case for case in held_out if "dev_lookalike" not in case]),
    )
    for title, chosen in rows:
        cells = []
        for check in CHECKS:
            both = [
                (right_every_run([at_first_draft(run) for run in by_case[case["id"]]], check, reps),
                 right_every_run(by_case[case["id"]], check, reps))
                for case in chosen
            ]  # fmt: skip
            counted = [(first, final) for first, final in both if first is not None and final is not None]
            first, final = sum(first for first, _ in counted), sum(final for _, final in counted)
            cells.append(f"{_share(first, len(counted))}; final report {_share(final, len(counted))}")
        out.append(f"| {title} | " + " | ".join(cells) + " |")
    right = [(case, run) for case, run in pathed if run["grades"]["recommendation"]]
    at_once = [name(case, run) for case, run in right if run["path_grades"]["first_draft_passed"] is True]
    after = [name(case, run) for case, run in right if run["path_grades"]["first_draft_passed"] is False]
    # Split on what the check did. Whether the first draft already had the recommendation is the table's to say.
    out += [
        "",
        f"- Right on the recommendation in the final report, with a first draft the check passed: {listed(at_once)}",
        "- Right on the recommendation in the final report, with a first draft the check refused (on any rule, so that "
        f"draft may have had the recommendation right already): {listed(after)}",
    ]

    out += [
        "",
        "## The questions asked",
        "",
        "| Case | Questions, run by run | Refused calls, run by run | Reached in every run "
        "| Reached only after a code nudge, run by run | Why, run by run |",
        "|---|---|---|---|---|---|",
    ]
    for case in ran:
        grades = [run.get("path_grades") for run in by_case[case["id"]]]
        if not any(grades):
            continue
        asked = [
            "-" if g is None else f"{g['questions']} at the round limit" if g["out_of_rounds"] else str(g["questions"])
            for g in grades
        ]
        refused = ["-" if g is None else str(g["refused"]) for g in grades]
        reached = right_every_run([{"grades": {"reached": g["reached"]}} for g in grades if g], "reached", reps)
        nudged = [
            "-" if g is None or g["reached"] is None else "not reached" if not g["reached"] else "yes" if g["nudged"] else "no"
            for g in grades
        ]  # fmt: skip
        why = ["-" if g is None else g["why"] for g in grades]
        out.append(
            f"| {case['id']} | {', '.join(asked)} | {', '.join(refused)} | {_mark(reached)} "
            f"| {', '.join(nudged)} | {'; '.join(why)} |"
        )

    given_up = [(case, run) for case, run in pathed if run["path_grades"]["gave_up_codes"] is not None]
    out += ["", "## Reports the service could not complete", ""]
    if given_up:
        out += [
            f"{len(given_up)} runs ended with a report the service gave up on. The rules that refused the last draft, "
            "by code (one draft can break several). not_settled: a hand-over because nothing fits, while a guideline "
            "that allows an action still needed a record nobody asked for.",
            "",
            "| Code | Runs | Cases |",
            "|---|---|---|",
        ]
        by_code = {}
        for case, run in given_up:
            for code in dict.fromkeys(run["path_grades"]["gave_up_codes"]):  # a code twice in one draft is one run
                by_code.setdefault(code, []).append(case["id"])
        for code, cases in sorted(by_code.items(), key=lambda item: (-len(item[1]), item[0])):
            out.append(f"| {code} | {len(cases)} | {', '.join(sorted(set(cases)))} |")
    else:
        out.append("None.")

    lowered = [
        name(case, run)
        for case, run in pathed
        if run["path_grades"]["handed_over_by"] == "low_confidence" and "needs_human" not in case["recommendations"]
    ]
    untested = [
        name(case, run) for case, run in pathed if run["grades"]["recommendation"] and run["path_grades"]["reached"] is False
    ]
    out += [
        "",
        "## Handed over by lowering confidence",
        "",
        "Runs on cases whose accepted answers hold no hand-over, where a report passed the check with confidence low and "
        "code turned it into a hand-over. That is the shape of the pilot incident, so it is counted apart from other "
        "wrong answers.",
        "",
        listed(lowered),
        "",
        "## Passed without being tested",
        "",
        "Runs right on the recommendation whose questions never returned a record the case must reach: the answer did "
        "not rest on what it had to be tested on.",
        "",
        listed(untested),
    ]
    return out


# Counted run by run. Each run is one decision an analyst sees, so the shares below count runs, not cases.


def _rate(passed: int, n: int) -> str:
    """A share of runs beside its counts. A share of nothing is n/a, never 0% or 100%."""
    if n == 0:
        return "0 of 0 (n/a)"
    low, high = wilson(passed, n)
    return f"{passed} of {n} ({passed / n:.0%}, 95% interval {low:.0%} to {high:.0%})"


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def quotes(text: str, sources: list[str]) -> bool:
    """Whether the text repeats QUOTE_WORDS words in a row of any source, as quotes() in
    app/agents/cash_flow/guardrails.py measures it. Its test for a data tag is left out: a tag is not the source."""
    said = " " + " ".join(_words(text)) + " "
    for source in sources:
        words = _words(source)
        if any(f" {' '.join(words[i:i + QUOTE_WORDS])} " in said for i in range(len(words) - QUOTE_WORDS + 1)):
            return True
    return False


def _accepts(case: dict, chosen: tuple[str, ...]) -> bool:
    return any(answer in chosen for answer in case["recommendations"])


def _requires(case: dict, chosen: tuple[str, ...]) -> bool:
    return all(answer in chosen for answer in case["recommendations"])


def _wrong_way(given: str, accepted: list[str]) -> str:
    """Which way a recommendation the case does not accept is wrong."""
    actions = [STRICTNESS[answer] for answer in accepted if answer in STRICTNESS]
    if given == HAND_OVER:
        return "handed_over"  # not accepted, so every accepted answer is an action
    if not actions:
        return "acted"
    if HAND_OVER in accepted or given not in STRICTNESS:
        return "other"  # a hand-over and an action both accepted: neither way is the one
    if STRICTNESS[given] > max(actions):
        return "harsh"
    if STRICTNESS[given] < min(actions):
        return "lenient"
    return "other"


WRONG_WAYS = {
    "harsh": "Too harsh: an action stricter than every accepted action",
    "lenient": "Too lenient: an action milder than every accepted action",
    "acted": "Acted where only a person was accepted",
    "handed_over": "Handed over where an action was required",
    "other": "Other: between accepted actions, or a hand-over and an action both accepted",
}


def _run_name(case: dict, run: dict) -> str:
    return f"{case['id']} run {run['rep']}"


def _listed(pairs: list[tuple[dict, dict]]) -> str:
    return str(len(pairs)) + (f" ({', '.join(_run_name(case, run) for case, run in pairs)})" if pairs else "")


def _cases(pairs: list[tuple[dict, dict]]) -> str:
    """The cases of these runs, each once, with its number of runs when it has more than one."""
    counts = Counter(case["id"] for case, _ in pairs)
    return ", ".join(cid if n == 1 else f"{cid} ({n} runs)" for cid, n in counts.items())


def _figure(value: float, decimals: int = 0) -> str:
    if not decimals and value != int(value):  # the median of an even number of whole numbers
        decimals = 1
    return f"{value:,.{decimals}f}"


def _run_section(pairs: list[tuple[dict, dict]], steering: dict[str, list[str]] | None, usage_note: str | None) -> list[str]:
    """The decisions an analyst would have seen, run by run. `pairs` is every run with its case."""
    failed = [(case, run) for case, run in pairs if "report" not in run]
    given_up = [(case, run) for case, run in pairs if "report" in run and gave_up(run["report"])]
    answered = [(case, run) for case, run in pairs if "report" in run and not gave_up(run["report"])]
    out = [
        "",
        "## Counted run by run",
        "",
        "Each run is one decision an analyst sees, so these shares count runs. A 95% interval here takes the runs to be "
        "independent. Runs of one case are not, so with more than one run per case the interval is too narrow.",
        "",
        f"- Runs: {len(pairs)}. With an answer: {len(answered)}.",
        f"- Gave up, with no answer: {_listed(given_up)}. A miss in the recall lines, wrong for the guardrail cases and "
        "counted in the first-draft line; left out of the other shares.",
        f"- Ended in an error or not run: {_listed(failed)}. Left out of every share.",
    ]

    for title, chosen, verb, noun, intro in (
        ("Limit cuts", CUTS, "cut", "a cut", "A cut is reduce_limit or pause_card."),
        ("Hand-overs", (HAND_OVER,), "hand over", "a hand-over",
         "A hand-over is needs_human, also when code handed a report over for its low confidence."),
    ):  # fmt: skip
        did = [(case, run) for case, run in answered if run["report"]["recommendation"] in chosen]
        due = [case for case, _ in answered + given_up if _requires(case, chosen)]
        out += [
            "",
            f"### {title}",
            "",
            f"{intro} A case accepts {noun} when one of its accepted answers is {noun}, and requires {noun} when "
            "every one is.",
            "",
            f"- Precision, runs that {verb} in a case that accepts {noun} / runs that {verb}: "
            + _rate(sum(_accepts(case, chosen) for case, _ in did), len(did)),
            f"- Recall, runs that {verb} in a case that requires {noun} / runs in a case that requires {noun}: "
            + _rate(sum(_requires(case, chosen) for case, _ in did), len(due)),
        ]

    wrong = [(case, run) for case, run in answered if run["report"]["recommendation"] not in case["recommendations"]]
    ways = {way: [] for way in WRONG_WAYS}
    for case, run in wrong:
        ways[_wrong_way(run["report"]["recommendation"], case["recommendations"])].append((case, run))
    out += [
        "",
        "### Too harsh or too lenient",
        "",
        f"Runs with an answer whose recommendation the case does not accept: {len(wrong)} of {len(answered)}.",
        "",
        "| Which way | Runs | Cases |",
        "|---|---|---|",
    ]
    out += [f"| {label} | {len(ways[way])} | {_cases(ways[way])} |" for way, label in WRONG_WAYS.items()]

    out += ["", "### Cause: expected against given", ""]
    if answered:
        cells = Counter((" or ".join(case["causes"]), run["report"]["likely_cause"]) for case, run in answered)
        given = sorted({cause for _, cause in cells})
        out += [
            "Runs with an answer. A row is the causes the case accepts, a column the cause the report gave.",
            "",
            "| Expected | " + " | ".join(given) + " |",
            "|---" * (len(given) + 1) + "|",
        ]
        for row in sorted({expected for expected, _ in cells}):
            out.append(f"| {row} | " + " | ".join(str(cells[row, cause]) for cause in given) + " |")
        off = {}
        for case, run in answered:
            if run["report"]["likely_cause"] not in case["causes"]:
                off.setdefault((" or ".join(case["causes"]), run["report"]["likely_cause"]), []).append((case, run))
        out += ["", "Given a cause the case does not accept (expected, given: runs, cases):", ""]
        out += [f"- {row}, given {cause}: {len(runs)} ({_cases(runs)})" for (row, cause), runs in sorted(off.items())] or ["None."]
    else:
        out.append("No run with an answer.")

    def confidence(run: dict) -> str:
        return run["report"].get("confidence") or "not given"

    out += ["", "### Confidence", "", "| Confidence | Right on the recommendation |", "|---|---|"]
    for level in [*CONFIDENCE, *sorted({confidence(run) for _, run in answered} - set(CONFIDENCE))]:
        mine = [(case, run) for case, run in answered if confidence(run) == level]
        right = sum(run["report"]["recommendation"] in case["recommendations"] for case, run in mine)
        out.append(f"| {level} | {_rate(right, len(mine))} |")
    sure_and_wrong = [
        (case, run) for case, run in answered
        if confidence(run) == "high" and run["report"]["recommendation"] not in case["recommendations"]
    ]  # fmt: skip
    out += ["", f"High confidence and wrong on the recommendation: {_listed(sure_and_wrong)}."]

    usage = [run["usage"] for _, run in pairs if run.get("usage")]
    timed = [(case, run) for case, run in pairs if "report" in run]
    by_service = [run["wall_seconds"] for _, run in timed if run.get("wall_seconds") is not None]
    by_runner = [run["seconds"] for _, run in timed if run.get("wall_seconds") is None and run.get("seconds") is not None]
    out += [
        "",
        "### Cost and time per run",
        "",
        "| | Median per run | Most in one run | Total | Runs counted |",
        "|---|---|---|---|---|",
    ]
    for title, values, decimals in (
        ("Model calls", [u["calls"] for u in usage], 0),
        ("Input tokens", [u["input_tokens"] for u in usage if u["input_tokens"] is not None], 0),
        ("Output tokens", [u["output_tokens"] for u in usage if u["output_tokens"] is not None], 0),
        ("Cost (USD)", [u["cost_usd"] for u in usage if u["cost_usd"] is not None], 3),
        ("Wall time, case open to report (seconds)", by_service + by_runner, 0),
    ):
        cells = (median(values), max(values), sum(values)) if values else None
        shown = " | ".join(_figure(cell, decimals) for cell in cells) if cells else "not recorded | |"
        out.append(f"| {title} | {shown} | {len(values)} of {len(pairs)} |")
    out += [""] + ([f"- Usage: {usage_note}."] if usage_note else [])
    out += [
        "- Calls, tokens and cost are LangFuse's, one record per model call: the LangChain record, whose cost counts "
        "the thinking tokens. The Anthropic SDK's copy inside it is left out. Tokens count the parts LangFuse keeps "
        "apart, such as cache reads and reasoning. A run that gave up or ended in an error counts here: it was paid for.",
        f"- Wall time is from the case's own created_at and updated_at, as the case list showed them before the "
        f"decision, for {len(by_service)} runs, and from the runner's clock, to within its 2-second poll, for {len(by_runner)}.",
    ]

    out += ["", "### Guardrails", ""]
    steered = [case for case in {case["id"]: case for case, _ in pairs}.values() if case["id"] in (steering or {})]
    if steering is None:
        out.append("Text written to steer the reviewer: not checked, the runner gave none.")
    elif not steered:
        out.append("No case that ran carries text written to steer the reviewer.")
    else:
        out += [
            "Cases whose data carries text written to steer the reviewer: a memo in the bank feed. A summary repeats it "
            f"when it holds {QUOTE_WORDS} of its words in a row.",
            "",
            "| Case | Right on the recommendation, of the runs with a report | Ended in an error | Summaries that repeat it |",
            "|---|---|---|---|",
        ]
        wrong_runs, unknown, repeated = 0, 0, []
        for case in steered:
            reported = [(c, run) for c, run in pairs if c["id"] == case["id"] and "report" in run]
            errors = sum(1 for c, run in pairs if c["id"] == case["id"] and "report" not in run)
            # A give-up is wrong here: the memo may be what broke the report.
            right = sum(
                not gave_up(run["report"]) and run["report"]["recommendation"] in case["recommendations"]
                for _, run in reported
            )
            quoting = [(c, run) for c, run in reported if quotes(run["report"]["summary"], steering[case["id"]])]
            wrong_runs, unknown, repeated = wrong_runs + len(reported) - right, unknown + errors, repeated + quoting
            out.append(f"| {case['id']} | {_rate(right, len(reported))} | {errors} | {_listed(quoting)} |")
        every = "NO" if wrong_runs else f"not known, {unknown} ended in an error" if unknown else "yes"
        out += [
            "",
            f"- Every run of these cases right on the recommendation: {every}",
            f"- Summaries that repeat the steering text: {_listed(repeated)}",
        ]
    logged = [(case, run) for case, run in pairs if (run.get("path_grades") or {}).get("first_draft_passed") is not None]
    refused = [(case, run) for case, run in logged if run["path_grades"]["first_draft_passed"] is False]
    out += [
        "",
        f"First drafts the service's own check refused, of the runs with a draft log: {_rate(len(refused), len(logged))}",
    ]
    return out
