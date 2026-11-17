"""Grading for the golden dataset. Code only: no model judges anything here.

A run is one case taken through the system once. A case is right only when
every one of its runs is right, because an analyst sees one run, not the best of three.
"""

from math import sqrt

# How hard an action is on the customer. needs_human is not an action, so it has no place.
STRICTNESS = {"keep_limit": 0, "request_documents": 1, "reduce_limit": 2, "pause_card": 3}
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


def grade(case: dict, report: dict) -> dict:
    """One run against the accepted answers. A check the case does not have is None."""
    if gave_up(report):
        # Wrong on everything, also for a case that should go to a person: it got there by failing.
        return {"recommendation": False, "cause": False, "hand_over": False, "cites": False if "must_cite" in case else None}
    return {
        "recommendation": report["recommendation"] in case["recommendations"],
        "cause": report["likely_cause"] in case["causes"],
        # Handing over a case the agent should settle is a failure too, and the other way round.
        "hand_over": (report["recommendation"] == "needs_human") == ("needs_human" in case["recommendations"]),
        "cites": set(case["must_cite"]) <= cited(report) if "must_cite" in case else None,
    }


def grade_path(case: dict, report: dict, path: dict) -> dict:
    """How a run reached its answer, from the case's path page (spec 8.5). Every key of the path is read with .get."""
    questions, drafts, gaps = path.get("questions", []), path.get("drafts", []), path.get("gaps", [])
    must_reach = case.get("must_reach", [cid for cid in case.get("must_cite", []) if not cid.startswith(OWN_RECORDS)])
    returned = {cid for question in questions for cid in question.get("returned", [])}
    reached = set(must_reach) <= returned if must_reach else None
    final, given_up = grade(case, report), gave_up(report)
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
        first_draft = grade(case, issued)

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

    nudged = []
    for cid in must_reach:
        index = next((i for i, question in enumerate(questions) if cid in question.get("returned", [])), None)
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


def summary(golden: dict, runs: list[dict], requests: list[dict], reps: int, about: dict) -> str:
    """The results as a document. `runs` holds one record per case and repetition."""
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
