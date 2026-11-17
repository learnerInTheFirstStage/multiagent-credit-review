"""Set two arms' runs of the golden set side by side, case by case (spec 8.4).

    python eval/compare.py eval/results/<run A>/runs.jsonl eval/results/<run B>/runs.jsonl > compare.md

Each file is one arm's run on a fresh stack, as eval/run.py writes it. A case is set side by side only when
every run of it, in both files, was graded with its question path. A case with a run missing or broken is
named and left out. Each case's class comes from eval/golden-set.md: R when the fixed list of questions reaches
its answer, D when the answer needs a question about one record an earlier answer listed.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from grade import at_first_draft, right_every_run  # noqa: E402

# The checks set side by side. Hand-over follows from the recommendation.
CHECKS = ("recommendation", "cause", "cites")
STAGES = (("first", "On the first checked draft"), ("final", "On the final report"))


def classes(document: Path) -> dict[str, str]:
    """Each case's class, in the order of the dataset's document."""
    rows = (line.split(" | ") for line in document.read_text().splitlines() if line.startswith("| c_"))
    return {row[0].removeprefix("| "): row[3] for row in rows}


def paired(a: list[dict], b: list[dict], classes: dict[str, str], files: tuple[str, str]) -> str:
    """The paired table of two arms' run records, read from `files`, as a document."""
    arms = {"A": a, "B": b}
    reps = {label: max((run["rep"] for run in runs), default=0) for label, runs in arms.items()}
    names = {label: next((run["arm"] for run in runs if "arm" in run), "arm not recorded") for label, runs in arms.items()}

    def runs_of(label: str, case_id: str) -> list[dict]:
        return sorted((run for run in arms[label] if run["case"] == case_id), key=lambda run: run["rep"])

    def complete(label: str, case_id: str) -> bool:
        runs = runs_of(label, case_id)
        # An arm with no runs at all has reps 0: no case of it is complete.
        return bool(runs) and len(runs) == reps[label] and all("path_grades" in run for run in runs)

    def right(label: str, case_id: str, check: str, stage: str) -> bool | None:
        runs = runs_of(label, case_id)
        return right_every_run([at_first_draft(run) for run in runs] if stage == "first" else runs, check, reps[label])

    def mark(value: bool | None) -> str:
        return {True: "yes", False: "NO", None: "-"}[value]

    def by_run(label: str, case_id: str, say) -> str:
        return ", ".join(say(run["path_grades"]) for run in runs_of(label, case_id))

    seen = [case_id for case_id in classes if any(run["case"] == case_id for run in a + b)]
    cases = [case_id for case_id in seen if complete("A", case_id) and complete("B", case_id)]
    left_out = [case_id for case_id in seen if case_id not in cases]
    out = ["# Two arms set side by side", ""]
    out += [f"- {label}: {names[label]}, {reps[label]} runs of each case, from {file}" for label, file in zip(arms, files)]
    out += [f"- {len(cases)} cases in both. Left out, a run missing or not graded with its path: {', '.join(left_out) or 'none'}."]
    if unknown := sorted({run["case"] for run in a + b} - classes.keys()):
        out += [f"- Left out, not in the dataset's document, so of no class: {', '.join(unknown)}."]

    out += [
        "",
        "## Case by case",
        "",
        "Right in every run, as recommendation / cause / cites. A dash: the case has no such check.",
        "",
        "| Case | Class | A, first draft | A, final report | B, first draft | B, final report |",
        "|---|---|---|---|---|---|",
    ]
    for case_id in cases:
        cells = [" / ".join(mark(right(label, case_id, check, stage)) for check in CHECKS) for label in arms for stage, _ in STAGES]
        out.append(f"| {case_id} | {classes[case_id]} | " + " | ".join(cells) + " |")

    # The first draft comes first: all arms share one check, whose refusals say what is missing, so their final
    # reports converge whatever the model did.
    for stage, title in STAGES:
        out += ["", f"## {title}", "", "| Class | Check | Both | Only A | Only B | Neither |", "|---|---|---|---|---|---|"]
        for kind in dict.fromkeys(classes[case_id] for case_id in cases):
            for check in CHECKS:
                cells = {"both": [], "A": [], "B": [], "neither": []}
                for case_id in (case_id for case_id in cases if classes[case_id] == kind):
                    in_a, in_b = right("A", case_id, check, stage), right("B", case_id, check, stage)
                    if in_a is None or in_b is None:
                        continue  # the case has no such check
                    cells["both" if in_a and in_b else "A" if in_a else "B" if in_b else "neither"].append(case_id)
                named = {key: f"{len(ids)}: {', '.join(ids)}" if ids else "0" for key, ids in cells.items()}
                out.append(
                    f"| {kind} | {check} | {len(cells['both'])} | {named['A']} | {named['B']} | {len(cells['neither'])} |"
                )

    out += [
        "",
        "## Questions asked",
        "",
        "Questions sent, run by run. The fixed plan asks 8, the agent at most 6 of its own.",
        "",
        "| Case | Class | Questions, A | Questions, B | Refused calls, A | Refused calls, B |",
        "|---|---|---|---|---|---|",
    ]
    for case_id in cases:
        asked = [
            by_run(label, case_id, lambda g: f"{g['questions']} at the round limit" if g["out_of_rounds"] else str(g["questions"]))
            for label in arms
        ]  # fmt: skip
        refused = [by_run(label, case_id, lambda g: str(g["refused"])) for label in arms]
        out.append(f"| {case_id} | {classes[case_id]} | {' | '.join(asked)} | {' | '.join(refused)} |")

    out += ["", "| Arm | Runs | Questions per run | Refused calls | Ended at the round limit | Gave up |", "|---|---|---|---|---|---|"]
    for label in arms:
        grades = [run["path_grades"] for case_id in cases for run in runs_of(label, case_id)]
        questions = sum(g["questions"] for g in grades) / len(grades) if grades else 0
        out.append(
            f"| {label}: {names[label]} | {len(grades)} | {questions:.1f} | {sum(g['refused'] for g in grades)} "
            f"| {sum(g['out_of_rounds'] for g in grades)} | {sum(g['gave_up_codes'] is not None for g in grades)} |"
        )

    out += ["", "## The agent on class D", ""]
    agents = [label for label in arms if names[label] == "agent"]
    class_d = [case_id for case_id in cases if classes[case_id] == "D"]
    if agents and class_d:
        out += [
            "Whether a question returned the records the case must reach before a code gap named it, run by run. A run "
            "nudged there has shown what the rules arm shows.",
            "",
            "| Case | " + " | ".join(f"{label}: {names[label]}" for label in agents) + " |",
            "|---|" + "---|" * len(agents),
        ]

        def reached(g: dict) -> str:
            if g["reached"] is None:
                return "-"
            return "not reached" if not g["reached"] else "after a nudge" if g["nudged"] else "unprompted"

        for case_id in class_d:
            out.append(f"| {case_id} | " + " | ".join(by_run(label, case_id, reached) for label in agents) + " |")
    else:
        out.append("No agent arm, or no class D case, in these runs.")
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("a", type=Path, help="runs.jsonl of the first arm")
    parser.add_argument("b", type=Path, help="runs.jsonl of the second arm")
    parser.add_argument("--document", type=Path, default=ROOT / "eval" / "golden-set.md", help="where the classes are")
    args = parser.parse_args()
    a, b = ([json.loads(line) for line in path.read_text().splitlines() if line.strip()] for path in (args.a, args.b))
    print(paired(a, b, classes(args.document), (str(args.a), str(args.b))), end="")


if __name__ == "__main__":
    main()
