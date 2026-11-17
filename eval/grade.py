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
LINKS = {"twin_of": "twin of", "same_as": "same as", "milder_than": "milder than"}
# How the summary of a report starts when two drafts failed the service's own check. The service
# then hands the case to a person with no cause and no evidence: no answer was reached.
GAVE_UP = "The report could not be completed."


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
    if link == "twin_of":  # one fact apart: the agent has to get both right
        return mine["grades"]["recommendation"] and other["grades"]["recommendation"]
    got, base = mine["report"]["recommendation"], other["report"]["recommendation"]
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
    return "\n".join(out) + "\n"
