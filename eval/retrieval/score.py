"""Score the past-case search with RAGAS: did it return the precedents situations.yaml labels, and only those?

Two RAGAS metrics, both by id, so no model judges and a run costs nothing:
  precision  IDBasedContextPrecision: of the reviews search showed, the share that are real precedents.
             Low means the investigator, and the analyst reading its path, see misleading cases.
  recall     IDBasedContextRecall: of the real precedents, the share search showed. Low means a useful
             precedent was missed.
Each at k = 1, 2, 3 (the service's TOP_K) and 5, on the first k results.

    eval/retrieval/.venv/bin/python eval/retrieval/score.py eval/retrieval/results/retrieved.json
    eval/retrieval/.venv/bin/python eval/retrieval/score.py RETRIEVED.json --runs eval/results/<run id>/runs.jsonl

RETRIEVED.json is retrieve.py's output: fixed queries, every embedder. --runs adds the searches the
agent really made in golden-set runs (eval/run.py keeps each one's query and returned ids in the case's
path), scored against the labels of the case's situation. Writes results/retrieval.md and a JSON file.

--judge adds RAGAS's LLM-judged ContextRelevance on every query and review pair in the top 3, as the
investigator reads the review (masked). Two judge prompts per pair, averaged by RAGAS. The judge is
JUDGE_MODEL (default claude-opus-5-5), not the investigator's LLM_MODEL. It cross-checks the labels:
where the judge and situations.yaml disagree, one of them is wrong, and the report lists each case.
Spends money; --judge-limit N judges only the first N pairs, to price a run. The key is LLM_API_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")  # before ragas is imported: no usage telemetry

import yaml  # noqa: E402
from ragas import SingleTurnSample  # noqa: E402

with warnings.catch_warnings():
    # Deprecated in 0.4 with no replacement in ragas.metrics.collections; requirements.txt pins the version.
    warnings.simplefilter("ignore", DeprecationWarning)
    from ragas.metrics import IDBasedContextPrecision, IDBasedContextRecall  # noqa: E402

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
KS = (1, 2, 3, 5)
TOP_K = 3  # services/orchestrator/app/retrieval.py
PRECISION, RECALL = IDBasedContextPrecision(), IDBasedContextRecall()


async def scores(returned: list[str], relevant: list[str]) -> dict:
    """RAGAS precision and recall at each k. None where nothing was returned (RAGAS gives NaN)."""
    out = {}
    for k in KS:
        if not returned:  # RAGAS gives NaN on an empty list: a search that found nothing has no precision
            out[k] = {"precision": None, "recall": 0.0}
            continue
        sample = SingleTurnSample(retrieved_context_ids=returned[:k], reference_context_ids=relevant)
        out[k] = {"precision": await PRECISION.single_turn_ascore(sample),
                  "recall": await RECALL.single_turn_ascore(sample)}
    return out


JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-opus-5-5")
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
JUDGE_CONCURRENCY = 8
# A judge score at or above this counts as "relevant" when set against the labels. 0.5 is what two
# "partially relevant" ratings make: the same topic (payroll, a transfer). It takes one "relevant" to pass.
RELEVANT_AT = 0.75
CACHE = RESULTS / "judge-cache.json"  # verdicts by model, query and review: a rerun pays only for new pairs


def render(shown: dict) -> str:
    """One review as the investigator reads it: masked text, cause and recommendation, no ids."""
    return (f"An earlier credit review dated {shown['as_of']}. What was found and noted: {shown['text']} "
            f"Likely cause: {shown['likely_cause']}. Recommendation: {shown['recommendation']}.")


def question(query: str) -> str:
    return f"Which earlier credit reviews dealt with a case like this one? {query}"


async def judge(pairs: dict[tuple[str, str], str], limit: int | None) -> tuple[dict, dict]:
    """RAGAS ContextRelevance for each (query, review id) pair. Returns scores (None on a judge error) and usage."""
    from anthropic import AsyncAnthropic
    from ragas.llms import llm_factory
    from ragas.metrics.collections import ContextRelevance

    key = os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("--judge needs LLM_API_KEY or ANTHROPIC_API_KEY (set -a; source .env; set +a)")
    client = AsyncAnthropic(api_key=key)
    usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    create = client.messages.create

    async def counted(*args, **kwargs):  # wrapped before RAGAS takes the client, so every call is counted
        response = await create(*args, **kwargs)
        usage["calls"] += 1
        usage["input_tokens"] += response.usage.input_tokens
        usage["output_tokens"] += response.usage.output_tokens
        return response

    client.messages.create = counted
    metric = ContextRelevance(llm=llm_factory(JUDGE_MODEL, provider="anthropic", client=client, max_tokens=2048))
    gate = asyncio.Semaphore(JUDGE_CONCURRENCY)
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    cached = {pair: cache[key] for pair in pairs if (key := json.dumps([JUDGE_MODEL, *pair])) in cache}
    todo = [(pair, text) for pair, text in pairs.items() if pair not in cached]
    todo = todo[:limit] if limit else todo

    async def one(pair, text):
        async with gate:
            try:
                value = (await metric.ascore(user_input=question(pair[0]), retrieved_contexts=[text])).value
            except Exception:  # noqa: BLE001  reported as a judge error, never as a score
                return pair, None
            return pair, None if value != value else round(value, 2)  # NaN: both judges failed

    scored = dict(await asyncio.gather(*(one(p, t) for p, t in todo)))
    RESULTS.mkdir(exist_ok=True)
    cache.update({json.dumps([JUDGE_MODEL, *pair]): value for pair, value in scored.items() if value is not None})
    CACHE.write_text(json.dumps(cache, indent=0))
    price_in, price_out = PRICES.get(JUDGE_MODEL, (0.0, 0.0))
    usage["cost"] = round((usage["input_tokens"] * price_in + usage["output_tokens"] * price_out) / 1e6, 2)
    usage["from_cache"] = len(cached)
    return {**cached, **scored}, usage


def avg(rows: list[dict], k: int, metric: str) -> str:
    values = [r["scores"][k][metric] for r in rows if r["scores"][k][metric] is not None]
    return f"{mean(values):.2f}" if values else "-"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("retrieved", type=Path, help="retrieve.py's output")
    parser.add_argument("--runs", type=Path, action="append", default=[], help="a golden run's runs.jsonl (repeatable)")
    parser.add_argument("--situations", type=Path, default=HERE / "situations.yaml")
    parser.add_argument("--judge", action="store_true", help="add RAGAS's LLM-judged ContextRelevance (spends money)")
    parser.add_argument("--judge-limit", type=int, help="judge only the first N query and review pairs")
    args = parser.parse_args()
    situations = {s["id"]: s for s in yaml.safe_load(args.situations.read_text())["situations"]}
    situation_of = {case: s for s in situations.values() for case in s["cases"]}
    retrieved = json.loads(args.retrieved.read_text())

    rows = []
    for run in retrieved["runs"]:
        for r in run["results"]:
            relevant = situations[r["situation"]]["relevant"]
            rows.append({"source": run["embedder"], **r, "relevant": relevant,
                         "scores": await scores(r["returned"], relevant)})
    unavailable = [run["embedder"] for run in retrieved["runs"] if not run["available"]]

    agent, unlabelled = [], []
    for path in args.runs:
        for line in path.read_text().splitlines():
            record = json.loads(line)
            searches = [s for s in ((record.get("path") or {}).get("searches") or []) if "returned" in s]
            for search in searches:
                if record["case"] not in situation_of:
                    unlabelled.append({"case": record["case"], **search})
                    continue
                s = situation_of[record["case"]]
                agent.append({"source": f"agent ({path.parent.name})", "case": record["case"], "situation": s["id"],
                              "style": "agent", "query": search["query"], "returned": search["returned"],
                              "relevant": s["relevant"], "scores": await scores(search["returned"], s["relevant"])})

    usage = None
    if args.judge:
        # Each review's masked text, as search shows it, from every result retrieve.py kept.
        texts = {i: render(s) for run in retrieved["runs"] for r in run["results"]
                 for i, s in zip(r["returned"], r.get("shown") or [])}
        pairs = {(r["query"], i): texts[i] for r in rows + agent for i in r["returned"][:TOP_K] if i in texts}
        verdicts, usage = await judge(pairs, args.judge_limit)
        for r in rows + agent:
            r["judged"] = {i: verdicts[(r["query"], i)] for i in r["returned"][:TOP_K] if (r["query"], i) in verdicts}

    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    (RESULTS / f"{stamp}-scores.json").write_text(json.dumps({"retrieved": str(args.retrieved), "rows": rows,
                                                              "agent": agent, "unlabelled": unlabelled,
                                                              "judge": {"model": JUDGE_MODEL, "usage": usage}},
                                                             indent=1))
    text = summary(rows, agent, unlabelled, unavailable, retrieved)
    if usage is not None:
        text += judge_summary(rows + agent, usage)
    (RESULTS / "retrieval.md").write_text(text)
    print(text.split("\n## Per query")[0])
    if usage is not None:
        print(text[text.index("\n## LLM judge"):].split("\n### Where")[0])
    return 0


def judge_summary(rows: list[dict], usage: dict) -> str:
    """RAGAS's judged relevance next to the labels: per embedder and style, then where they disagree."""
    lines = ["", "## LLM judge: RAGAS ContextRelevance", "",
             f"Judge {JUDGE_MODEL}: {usage['calls']} new calls ({usage['input_tokens']} tokens in, "
             f"{usage['output_tokens']} out, about ${usage['cost']:.2f}), {usage['from_cache']} pairs from the cache. "
             f"A score is the mean of RAGAS's two judge prompts, each 0 (not relevant), 1 (partly) or 2 (relevant), "
             f"scaled to 0 to 1. At {RELEVANT_AT} or more it counts as relevant below.", "",
             "| Source | Pairs judged | Mean relevance of the top 3 | Judge errors |", "|---|---|---|---|"]
    groups: dict[str, list] = {}
    for r in rows:
        name = r["source"] if r["style"] == "agent" else f"{r['source']} ({r['style']})"
        groups.setdefault(name, []).extend(r.get("judged", {}).values())
    for name, values in groups.items():
        ok = [v for v in values if v is not None]
        lines.append(f"| {name} | {len(values)} | {mean(ok):.2f} | {len(values) - len(ok)} |" if ok else
                     f"| {name} | {len(values)} | - | {len(values)} |")

    seen, agree = {}, {(True, True): 0, (True, False): 0, (False, True): 0, (False, False): 0}
    for r in rows:
        for i, value in r.get("judged", {}).items():
            if value is not None and (r["query"], i) not in seen:
                labelled, judged = i in r["relevant"], value >= RELEVANT_AT
                seen[(r["query"], i)] = (r["situation"], labelled, value)
                agree[(labelled, judged)] += 1
    total = sum(agree.values()) or 1
    levels = (0.0, 0.25, 0.5, 0.75, 1.0)
    spread = {(labelled, v): 0 for labelled in (True, False) for v in levels}
    for _, labelled, value in seen.values():
        spread[(labelled, value)] = spread.get((labelled, value), 0) + 1
    lines += ["", "### Labels against the judge", "",
              "Each distinct query and review pair once. The judge's scores:", "",
              "| | " + " | ".join(str(v) for v in levels) + " |", "|---|" + "---|" * len(levels),
              "| Labelled a precedent | " + " | ".join(str(spread[(True, v)]) for v in levels) + " |",
              "| Not labelled | " + " | ".join(str(spread[(False, v)]) for v in levels) + " |", "",
              f"At {RELEVANT_AT} or more as relevant:", "",
              "| | Judge: relevant | Judge: not relevant |", "|---|---|---|",
              f"| Labelled a precedent | {agree[(True, True)]} | {agree[(True, False)]} |",
              f"| Not labelled | {agree[(False, True)]} | {agree[(False, False)]} |", "",
              f"They agree on {agree[(True, True)] + agree[(False, False)]} of {sum(agree.values())} pairs "
              f"({(agree[(True, True)] + agree[(False, False)]) / total:.0%})."]
    lines += ["", "### Where the judge and the labels disagree", "",
              "| Situation | Review | Labelled | Judge | Query |", "|---|---|---|---|---|"]
    for (query, i), (situation, labelled, value) in sorted(seen.items(), key=lambda x: (x[1][0], x[0][1])):
        if labelled != (value >= RELEVANT_AT):
            lines.append(f"| {situation} | {i} | {'yes' if labelled else 'no'} | {value} | {query} |")
    return "\n".join(lines) + "\n"


def summary(rows, agent, unlabelled, unavailable, retrieved) -> str:
    lines = ["# Past-case search, scored with RAGAS", "",
             f"Data clock {retrieved['as_of']}. Seeded reviews: {retrieved['seeded']}. "
             f"The service shows the top {TOP_K}. Precision is over the reviews returned, recall over the labelled precedents.",
             ""]
    if unavailable:
        lines += [f"Not available in this image, so not scored: {', '.join(unavailable)}.", ""]
    head = "| Embedder | Queries | " + " | ".join(f"P@{k} | R@{k}" for k in KS) + " |"
    lines += ["## By embedder", "", head, "|" + "---|" * (head.count("|") - 1)]
    # The ceiling: most situations have one precedent, so even a perfect search shows noise after rank 1.
    ideal = [{"scores": {k: {"precision": min(k, len(r["relevant"])) / k, "recall": min(k, len(r["relevant"])) /
                              len(r["relevant"])} for k in KS}} for r in rows if r["source"] == rows[0]["source"]] if rows else []
    if ideal:
        cells = " | ".join(f"{avg(ideal, k, 'precision')} | {avg(ideal, k, 'recall')}" for k in KS)
        lines.append(f"| best possible | {len(ideal)} | {cells} |")
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        for style in (r["style"], "all"):
            groups.setdefault((r["source"], style), []).append(r)
    for r in agent:
        groups.setdefault((r["source"], "agent"), []).append(r)
    order = {"close": 0, "paraphrase": 1, "generated": 2, "all": 3, "agent": 4}
    for (source, style), group in sorted(groups.items(), key=lambda g: (g[0][1] == "agent", g[0][0], order[g[0][1]])):
        cells = " | ".join(f"{avg(group, k, 'precision')} | {avg(group, k, 'recall')}" for k in KS)
        lines.append(f"| {source}{'' if style == 'agent' else f' ({style})'} | {len(group)} | {cells} |")

    lines += ["", "## Per query", "",
              f"At k = {TOP_K}, what the service shows. A labelled precedent is in **bold**.", "",
              "| Source | Situation | Style | P@3 | R@3 | Returned (rank order) |", "|---|---|---|---|---|---|"]
    for r in rows + agent:
        shown = ", ".join(f"**{i}**" if i in r["relevant"] else i for i in r["returned"][:TOP_K]) or "nothing"
        p, rc = r["scores"][TOP_K]["precision"], r["scores"][TOP_K]["recall"]
        situation = r["situation"] + (f" ({r['case']})" if "case" in r else "")
        lines.append(f"| {r['source']} | {situation} | {r['style']} | "
                     f"{'-' if p is None else f'{p:.2f}'} | {rc:.2f} | {shown} |")
    if unlabelled:
        lines += ["", "## Agent searches in cases with no labelled precedent", "",
                  "Anything returned here is background with no real precedent behind it.", ""]
        lines += [f"- {u['case']}: \"{u['query']}\" returned {', '.join(u['returned']) or 'nothing'}" for u in unlabelled]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
