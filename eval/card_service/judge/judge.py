"""Judge the Card Service agent's wording with DeepEval: what the code grader cannot read.

checks.py holds every finding to its records by code: ids, figures, counts, and a few plain phrasings
("will be paid", "normal"). A paraphrase gets past it: "expected to settle within days", "in keeping
with how it has always used the card", "a one-off hiccup". An analyst reads these findings before
approving a pause or a limit cut, so a finding that softens or overstates its records is a wrong
answer even when every number in it is right. This judge reads the words.

Two metrics per answer, judged by a model other than the one that wrote the findings:
  faithful_to_records  DeepEval's Faithfulness: no statement in the findings contradicts the cited
                       records. A statement they neither back nor contradict ("the bank returned it" for
                       an ACH return) is not counted: counted, it failed right answers on one run in two.
  states_not_judges    G-Eval: no recommendation, no prediction or reassurance, no wording stronger or
                       softer than the records, no cause the records do not show. What is unsupported
                       but matters to the analyst is caught here.

It runs on answers already made (no agent, no Card), in its own environment:

    uv venv eval/card_service/judge/.venv && uv pip install -p eval/card_service/judge/.venv \
        -r eval/card_service/judge/requirements.txt
    set -a; source .env; set +a
    eval/card_service/judge/.venv/bin/python eval/card_service/judge/judge.py              # newest model run
    eval/card_service/judge/.venv/bin/python eval/card_service/judge/judge.py RESULTS.json # a given run
    eval/card_service/judge/.venv/bin/python eval/card_service/judge/judge.py --fixtures   # calibration

--fixtures judges hand-written answers whose right verdict is known (fixtures.yaml), the ones code
lets through among them. Exit code 1 if the judge gets one wrong. A model run exits 0: the judge
reports next to the code grades, it does not replace them.

Spends money: about 4 to 8 judge calls per answer. The key is LLM_API_KEY (or ANTHROPIC_API_KEY).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# Before deepeval is imported: no usage telemetry leaves this machine, and its cache stays out of the repo.
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("DEEPEVAL_RESULTS_FOLDER", "")
os.environ.setdefault("ERROR_REPORTING", "NO")

import yaml  # noqa: E402
from deepeval.metrics import FaithfulnessMetric, GEval  # noqa: E402
from deepeval.models import AnthropicModel  # noqa: E402
from deepeval.test_case import LLMTestCase, SingleTurnParams  # noqa: E402

HERE = Path(__file__).resolve().parent
SET = HERE.parent
RESULTS = SET / "results"
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-opus-5-5")
CONCURRENCY = 4

STATES_NOT_JUDGES = [
    "The actual output is a list of findings about a business customer's credit card, written for a credit "
    "analyst. The retrieval context holds the Card Service records the findings cite. Judge only the wording; "
    "assume every figure, date and count is already checked.",
    "Penalize any finding that recommends, suggests or argues for an action: changing the limit, pausing the "
    "card, contacting the customer, watching or reviewing the account.",
    "Penalize any prediction or reassurance the records do not state: that a returned repayment will be, is "
    "expected to be or is likely to be paid; that a rise is temporary; that nothing is wrong.",
    "Penalize wording stronger or softer than the records: calling a rising or spiking utilization normal, in "
    "keeping or in line with history; calling a failed repayment minor, a hiccup or a one-off; calling spend "
    "alarming or out of control when the record says it is not unusual.",
    "Penalize any cause the records do not show: why a balance rose, what the spend was for, why the account "
    "lacked funds beyond the return reason in the record.",
    "Do not penalize restating record fields, figures or dates, comparing with the customer's usual figure "
    "when a record gives it, or saying what was not checked.",
    "Score high only if no finding does any of the penalized things.",
]


def _money(value: int) -> str:
    return f"${value / 100:,.2f}"


def _plain(value):
    """A record's data as an analyst reads it: *_cents as dollars and *_bp as percentages."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key.endswith("_cents") and isinstance(item, int):
                out[key.removesuffix("_cents")] = _money(item)
            elif key.endswith("_bp") and isinstance(item, int):
                out[key.removesuffix("_bp")] = f"{item / 100:g}%"
            else:
                out[key] = _plain(item)
        return out
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def render(record: dict) -> str:
    """One record, labelled as Card's: its fields (utilization_trend, unusual, paid_since) are Card's own ratings."""
    return f"Card Service record {record['id']} ({record['kind']}): {json.dumps(_plain(record['data']))}"


def test_case(name: str, question: str, findings: list[dict], records: dict[str, dict]) -> LLMTestCase:
    """One answer: its findings as the output, the records they cite as the context."""
    cited = list(dict.fromkeys(i for f in findings for i in f["evidence_ids"]))
    return LLMTestCase(
        name=name,
        input=question,
        actual_output="\n".join(f"- {f['claim']}" for f in findings),
        retrieval_context=[render(records[i]) for i in cited if i in records],
    )


def metrics(model: AnthropicModel) -> list:
    """Fresh instances per answer: a DeepEval metric keeps the state of its last measure."""
    return [
        FaithfulnessMetric(model=model, threshold=1.0, async_mode=True),
        GEval(name="states_not_judges", model=model, threshold=0.7, evaluation_steps=STATES_NOT_JUDGES,
              evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT,
                                 SingleTurnParams.RETRIEVAL_CONTEXT]),
    ]


NAMES = {"Faithfulness": "faithful_to_records", "states_not_judges [GEval]": "states_not_judges"}


async def judge(cases: list[tuple[LLMTestCase, dict]], model: AnthropicModel) -> list[dict]:
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(case: LLMTestCase, extra: dict) -> dict:
        async with gate:
            scores, cost = {}, 0.0
            for metric in metrics(model):
                try:
                    await metric.a_measure(case, _show_indicator=False)
                    scores[NAMES.get(metric.__name__, metric.__name__)] = {
                        "score": round(metric.score, 2), "passed": metric.is_successful(), "reason": metric.reason}
                except Exception as error:  # a judge call that fails is reported, not passed
                    scores[NAMES.get(metric.__name__, metric.__name__)] = {
                        "score": None, "passed": False, "reason": f"judge error: {error}"[:300]}
                cost += metric.evaluation_cost or 0.0
            return {"id": case.name, **extra, "findings": case.actual_output, "scores": scores, "cost": round(cost, 4)}

    return list(await asyncio.gather(*(one(c, e) for c, e in cases)))


def from_run(path: Path) -> list[tuple[LLMTestCase, dict]]:
    """The answers of a run.py results file that have findings, with the code grader's verdict."""
    questions = {c["id"]: c["question"] for c in yaml.safe_load((SET / "cases.yaml").read_text())["cases"]}
    out = []
    for r in json.loads(path.read_text()):
        if r["response"]["findings"]:
            records = {e["id"]: e for e in r["response"]["evidence"]}
            out.append((test_case(r["id"], questions[r["id"]], r["response"]["findings"], records),
                        {"code_passed": r["passed"]}))
    return out


def from_fixtures() -> list[tuple[LLMTestCase, dict]]:
    """Hand-written answers over Card's recorded records, each with the verdict it must get."""
    records = {e["id"]: e for v in json.loads((SET / "recorded.json").read_text()).values()
               for e in (v["body"] or {}).get("evidence", [])}
    out = []
    for f in yaml.safe_load((HERE / "fixtures.yaml").read_text())["fixtures"]:
        findings = [{"claim": c["claim"], "evidence_ids": c["cites"]} for c in f["findings"]]
        out.append((test_case(f["id"], f["question"], findings, records), {"want": f["want"], "about": f["about"]}))
    return out


def newest_model_run() -> Path:
    runs = [p for p in RESULTS.glob("*.json") if "no_model" not in p.name and "judge" not in p.name
            and p.name[:8].isdigit()]
    if not runs:
        sys.exit(f"no model run in {RESULTS}; run eval/card_service/run.py with LLM_MODEL set first")
    return max(runs, key=lambda p: p.name)


def summary(rows: list[dict], title: str, fixtures: bool) -> str:
    total = sum(r["cost"] for r in rows)
    lines = [f"# {title}", "", f"Judge: {JUDGE_MODEL}. {len(rows)} answers, ${total:.2f}.", ""]
    head = "| Answer | Want | Faithful | States, not judges | Judge right |" if fixtures else \
        "| Case | Code grader | Faithful | States, not judges |"
    lines += [head, "|" + "---|" * (head.count("|") - 1)]
    for r in rows:
        f, s = r["scores"]["faithful_to_records"], r["scores"]["states_not_judges"]
        cell = lambda m: f"{'pass' if m['passed'] else '**fail**'} {m['score']}"  # noqa: E731
        if fixtures:
            lines.append(f"| {r['id']} | {r['want']} | {cell(f)} | {cell(s)} | {'yes' if r['right'] else '**no**'} |")
        else:
            lines.append(f"| {r['id']} | {'pass' if r['code_passed'] else 'fail'} | {cell(f)} | {cell(s)} |")
    lines += ["", "## Reasons", ""]
    for r in rows:
        lines.append(f"**{r['id']}**")
        for name, m in r["scores"].items():
            lines.append(f"- {name} ({m['score']}): {m['reason']}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results", nargs="?", type=Path, help="a run.py results file (default: the newest model run)")
    parser.add_argument("--fixtures", action="store_true", help="judge the calibration answers in fixtures.yaml")
    parser.add_argument("--only", action="append", default=[], help="judge only this answer id (repeatable)")
    args = parser.parse_args()
    key = os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("set LLM_API_KEY or ANTHROPIC_API_KEY (set -a; source .env; set +a)")
    model = AnthropicModel(model=JUDGE_MODEL, api_key=key)

    source = "fixtures" if args.fixtures else (args.results or newest_model_run())
    cases = from_fixtures() if args.fixtures else from_run(source)
    if args.only:
        cases = [c for c in cases if c[0].name in args.only]
    rows = asyncio.run(judge(cases, model))
    if args.fixtures:
        for r in rows:
            passed = all(m["passed"] for m in r["scores"].values())
            r["right"] = passed == (r["want"] == "pass")

    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    (RESULTS / f"{stamp}-judge-{'fixtures' if args.fixtures else Path(source).stem}.json").write_text(
        json.dumps(rows, indent=1))
    title = "Card Service judge, calibration answers" if args.fixtures else f"Card Service judge, {Path(source).name}"
    text = summary(rows, title, args.fixtures)
    (RESULTS / ("judge-fixtures.md" if args.fixtures else "judge.md")).write_text(text)
    print(text.split("\n## ")[0])
    return 0 if not args.fixtures or all(r["right"] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
