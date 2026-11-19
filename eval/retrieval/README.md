# Past-case search, scored with RAGAS

The investigator's `SearchPastCases` tool (`services/orchestrator/app/retrieval.py`) is the system's one retrieval step. It shows the model the 3 closed reviews (`TOP_K`) nearest its query, each with the earlier recommendation and what a person decided. A wrong precedent can steer a case. If search shows an unrelated "keep the limit" review, the investigator, and then the analyst reading its path, are anchored on a decision about a different situation. The eval plants these traps on purpose: `history_eval.yaml` says `seed_p1` to `seed_p5` mirror one case of a pair, "so copying the nearest one is wrong half the time".

Nothing measured what search returns: `grade.py` counts searches and nothing more. This folder does, with RAGAS:

| Metric | RAGAS | Answers | Cost |
|---|---|---|---|
| precision@k | `IDBasedContextPrecision` on the first k results | Of what search showed, how much was a real precedent? Low means misleading cases are shown. | $0 |
| recall@k | `IDBasedContextRecall` on the first k results | Of the real precedents, how many did it show? Low means a useful one was missed. | $0 |
| judged relevance | `ContextRelevance`, LLM-judged (`--judge`) | Would a model call each shown review relevant to the query? A cross-check on the hand labels. | about $4.50 for 315 pairs |

- **The ID-based metrics are the grade.** They are scored at k = 1, 2, 3 (what the service shows) and 5. No model judges, so the golden eval's rule against model judges holds.
- **The LLM judge sits next to them, not in place of them.** The judge is `JUDGE_MODEL` (default `claude-opus-5-5`), not the investigator's `LLM_MODEL` (`claude-sonnet-5-5`). It uses the same `LLM_API_KEY`, and nothing goes into `.env`.

It changes nothing in the Orchestrator. `retrieve.py` calls `app.retrieval.search` as it is, with its date, outcome, check and embedder filters, inside the Orchestrator's own image.

## What a precedent is: situations.yaml

- **Situations:** twelve kinds of case, covering 21 golden cases. Each lists the seeded reviews that are real precedents and why.
- **The rule:** a precedent has the same cause and the same card-repayment situation, whatever was decided then. Under it:
  - `seed_p3` (failed repayment, paid three days later) is the precedent for c_111;
  - `seed_p4` (failed repayment, never paid) is the precedent for c_110;
  - neither is a precedent for c_001, whose card is paid on time.
- **Two queries per situation,** in the tool's own form ("what was found so far, in one or two sentences"):
  - `close`: worded near the precedent's own claim;
  - `paraphrase`: the same facts in other words.
- **Why paraphrases matter:** the investigator never sees the seeds, so its queries are paraphrases.
- **No query names a cause or a recommendation.**
- **The pool is the same for every case:** every seeded review dated before the data clock, of any customer. So cases in one situation share their queries.

Six golden cases are left out because no seeded review is a precedent for them:

- c_009: a planned vendor payment.
- c_011 and c_013: no clear cause.
- c_012: the feed and the transactions disagree.
- c_103: three months of shrinking income with a card in good standing. `seed_p5` has a late repayment, and `seed_c_314` covers one weak week.
- c_106: income stopped, with the card paid from a feeder account.

For these, anything search shows is background with no precedent behind it. `score.py --runs` lists what the agent got for them.

## More queries: generated.yaml

Two hand-written queries per situation are few, and one person wrote them all. `generate.py` asks the judge model for 4 more per situation, 48 in all, for $0.10:
- **Source:** the queries are written from the stories of the situation's cases (`data/shared_customers.yaml`, `eval/extra_customers.yaml`), never from the seeded reviews, so they can't copy a precedent's wording.
- **Labels:** each query keeps its situation's labels.
- **Filter:** a query that names a cause label or a customer id is dropped. None were.
- **Kept in git:** `generated.yaml` is reviewed like `situations.yaml`, and a run never regenerates it.

The queries keep each case's own details, such as c_201's $3K vendor payment and c_203's memo addressed to the reviewer. An investigator would mention those too.

## Running it

```bash
# 1. The eval's Orchestrator image (with the fastembed model) and an empty database, as their own project
docker compose -p ragas -f docker-compose.yml -f eval/docker-compose.eval.yml build orchestrator
docker compose -p ragas -f docker-compose.yml -f eval/docker-compose.eval.yml up -d --wait postgres

# 2. The real search, both embedders, every query. No model, no Bank, no Card.
mkdir -p eval/retrieval/results
docker compose -p ragas -f docker-compose.yml -f eval/docker-compose.eval.yml run --rm --no-deps -T \
    -e PYTHONPATH=/app -v "$PWD/eval/retrieval:/eval/retrieval:ro" orchestrator \
    python /eval/retrieval/retrieve.py > eval/retrieval/results/retrieved.json

# 3. Score with RAGAS, in its own environment so the Orchestrator's dependencies do not change
uv venv eval/retrieval/.venv && uv pip install -p eval/retrieval/.venv -r eval/retrieval/requirements.txt
eval/retrieval/.venv/bin/python eval/retrieval/score.py eval/retrieval/results/retrieved.json   # $0
#    --runs eval/results/<run id>/runs.jsonl   also score the agent's own searches in a golden run
set -a; source .env; set +a                                                                     # LLM_API_KEY
eval/retrieval/.venv/bin/python eval/retrieval/score.py eval/retrieval/results/retrieved.json --judge
#    --judge-limit 10   price a run first (10 pairs cost $0.13)

# Only to remake generated.yaml (it is kept in git): about $0.10
eval/retrieval/.venv/bin/python eval/retrieval/generate.py

docker compose -p ragas down -v   # when done
```

- **Run `generate.py` before `retrieve.py`:** `retrieve.py` reads `generated.yaml` when it is there.

### Repeating the end-to-end test

This spends money on the system's model: about $0.40 a case on `claude-sonnet-5-5`, which LangFuse records when its keys are in `.env`. Use one fresh stack per arm. Move the ports if the default stack is up: a compose file that sets `ports: !override` on bank, card and orchestrator (here 8022, 8023 and 8024).

```bash
F="-f docker-compose.yml -f eval/docker-compose.eval.yml -f ports.override.yml"
docker compose -p ragas $F down -v && docker compose -p ragas $F up -d --build --wait postgres bank card orchestrator
set -a; source .env; set +a
services/orchestrator/.venv/bin/python eval/run.py --arm agent --api http://127.0.0.1:8024 \
    --container "docker compose -p ragas exec -T orchestrator" --reps 1 \
    --only c_107 c_110 c_111 c_002 c_102 --out eval/retrieval/results/golden
eval/retrieval/.venv/bin/python eval/retrieval/score.py eval/retrieval/results/retrieved.json \
    --runs eval/retrieval/results/golden/<run id>/runs.jsonl --judge
```

**Arm B (search encouraged)** is the same, on a fresh stack, with one more compose file. It mounts a copy of `services/orchestrator/app/graph/prompts.py` over the image's `/app/app/graph/prompts.py`. In the copy, `SEARCH_TOOL` ends with: "Once the specialists have answered, search past cases once with what you found, before you say you have enough." Keep the copy outside the repo: it's an experiment, not a change to the service. The run's `summary.md` records the prompts' hash, so the two arms can't be mixed up.
- **Judge verdicts are cached** in `results/judge-cache.json`, keyed by model, query and review. A rerun pays only for pairs it hasn't judged.

- **An empty database is required.** `retrieve.py` seeds the 15 reviews with `expect_clean` and stops if the database holds anything else.
- **Output:** `results/retrieval.md` and a JSON file per run, all git-ignored. RAGAS telemetry is switched off.
- **Agent searches:** `--runs` reads the agent's searches from a golden run's `runs.jsonl`. Each case's path keeps every search's query and returned ids. Searches that were refused are skipped.

## First results (2026-11-19)

Both embedders, 24 queries each, on the 15 seeded reviews:

| Embedder | Queries | P@1 | R@1 | P@3 | R@3 | R@5 |
|---|---|---|---|---|---|---|
| best possible | 24 | 1.00 | 0.81 | 0.47 | 0.98 | 1.00 |
| bge-small, close | 12 | 0.83 | 0.65 | 0.47 | 0.98 | 1.00 |
| bge-small, paraphrase | 12 | **0.50** | 0.40 | 0.33 | **0.65** | 0.75 |
| hash:384, close | 12 | 0.75 | 0.56 | 0.47 | 0.98 | 1.00 |
| hash:384, paraphrase | 12 | **0.17** | 0.10 | 0.22 | **0.48** | 0.62 |

**How to read P@3.** Most situations have one precedent, so even a perfect search shows two non-precedents in its top 3. That's the 0.47 ceiling. Read precision against the best-possible row.

What the scores show:

1. **The embedder matters only on paraphrase, and the agent's queries are paraphrases.**
   - On close wording both embedders find nearly every precedent in the top 3 (R@3 0.98).
   - On paraphrase, the first result is a real precedent half the time with bge-small, against 1 in 6 with `hash:384`. `hash:384` hashes words, so it only finds a review that shares them.
2. **The main stack does not run the embedder the eval measures.**
   - The Orchestrator image sets `EMBEDDINGS_MODEL=hash:384` (`services/orchestrator/Dockerfile`). The eval overlay switches to bge-small.
   - So golden-set results with search on describe a search the demo stack does not have.
3. **The c_107 trap works on both embedders.**
   - For "same stocking payment as last year, but income shrinking and a late repayment", both put the stocking reviews (all "keep the limit") above `seed_p5`.
   - On the paraphrase, neither shows `seed_p5` at all.
4. **Healthy customers are shown failed-repayment precedents.**
   - The payroll-and-tax paraphrase for c_001 (card paid on time) returns `seed_p4` and `seed_p3` first on both embedders.
   - `seed_p4` ended in a card pause.
5. **Three results is about right.**
   - Over all 24 queries with bge-small, recall goes from 0.71 at 2 results to 0.81 at 3 and 0.88 at 5.
   - Precision falls as results are added, and most of the gap is the single-precedent ceiling.
   - Cutting to 2 would lose precedents. Going to 5 would mostly add noise.

### With the 48 generated queries

| Embedder | Queries | P@1 | R@1 | P@3 | R@3 | R@5 |
|---|---|---|---|---|---|---|
| bge-small, generated | 48 | 0.58 | 0.47 | 0.38 | **0.84** | 0.91 |
| hash:384, generated | 48 | 0.48 | 0.38 | 0.28 | **0.66** | 0.75 |

The generated queries confirm the gap on a set four times larger and written by another author. The gap is smaller than on my paraphrases, and it's the same way round.

### The LLM judge against the labels

`claude-opus-5-5` judged every query and review pair in the top 3: 315 pairs, 630 calls, $4.48. Its scores:

| | 0 | 0.25 | 0.5 | 0.75 | 1 |
|---|---|---|---|---|---|
| Labelled a precedent | 0 | 0 | 13 | 7 | 71 |
| Not labelled | 59 | 28 | 119 | 7 | 11 |

1. **It never rejects a labelled precedent.** None scores below 0.5, and 78 of 91 score 0.75 or more.
2. **A score of 0.5 means "same topic".** That's what two "partly relevant" ratings make, such as "payroll and tax" in both texts. Counting it as relevant would make 119 more reviews precedents. So 0.75 is the line, and at 0.75 the judge and the labels agree on 284 of 315 pairs (90%).
3. **The 18 strong disagreements are the twin traps the labels are built for:**
   - `seed_p1` (transfer to the customer's own account) for the new-payee case (4);
   - `seed_p4` (repayment never paid) for the paid-three-days-later case (3);
   - a stocking review for c_107 (2);
   - payroll-and-tax reviews for the weak-income week.

   RAGAS's generic prompt asks whether the context holds information relevant to the question, so it rates topic overlap, and the twins overlap by design. That makes it the wrong ground truth for "which precedent fits". The labels stay the grade, and the judge confirms they are never missing an obvious precedent.

### End to end with the real model (2026-11-19)

The whole path ran on the eval stack: signal, investigation, both specialists on live Bank and Card, search, report, the service's check, then grading (`eval/run.py`) and RAGAS on the agent's own searches (`score.py --runs`).
- **Settings:** `claude-sonnet-5-5`, bge-small, LangFuse on for cost.
- **Cases:** the 5 where search matters most: c_107 (the stocking trap), the paid/unpaid twins c_110 and c_111, and the own-account/new-payee twins c_002 and c_102. One run each, in two arms, each on a fresh stack.

| Arm | Searches | Right (recommendation, cause, handover, citations) | Cost (LangFuse) |
|---|---|---|---|
| A: as is | **0 in 5 cases** (and 0 in 3 earlier runs) | 5 of 5 | $1.89 |
| B: search encouraged, an experiment | 1 in every case | 5 of 5 | $1.95 |

**Arm B is an experiment, not a proposal.** It adds one sentence to the investigator's search-tool text: "Once the specialists have answered, search past cases once with what you found, before you say you have enough." The changed `prompts.py` was mounted over the image's copy for that stack only. It isn't in this folder or in git; the run's summary records its hash (`cc3267ba4721`, against `81972768a026`).

What the agent's own searches got (RAGAS, ID-based):

| Source | Queries | P@1 | R@1 | P@3 | R@3 | Judged relevance of the top 3 |
|---|---|---|---|---|---|---|
| agent, arm B | 5 | **0.80** | 0.80 | 0.33 | **1.00** | 0.62 |
| bge-small, generated | 48 | 0.58 | 0.47 | 0.38 | 0.84 | 0.65 |

1. **The investigator doesn't search on its own.** It made 0 searches in 8 real-model runs. The prompt describes the tool but never says when to use it, and the customer's own earlier reviews already reach it through code. So, as the system stands, the RAG step is almost never exercised.
2. **When it does search, retrieval works.** The agent's queries carry the specific findings ("transfer to the customer's own account", "R01, still unpaid"), and bge-small put the real precedent first in 4 of 5 cases, with all 5 in the top 3. P@3 of 0.33 is the best possible here, since each case has one precedent.
3. **The twins still show up, and they didn't mislead.** Every top 3 included the look-alike twin (`seed_p2` for c_002, `seed_p4` for c_111, `seed_p3` for c_110), and c_107's first result was `seed_p2`, a new-payee review. All 5 answers stayed right. No report cited a search result; the only past review any report mentions is the customer's own history, which code supplies. The rule that precedents are background held end to end.
4. **Search added little to the cost:** $1.95 against $1.89, which is within run-to-run noise.
5. **Search didn't change any answer.** With 5 cases and one run each, this shows search is safe here, not that it helps. Every case was already right without it.

## Recommendations (for the Orchestrator's owner to decide)

1. **Run bge-small on the main stack**, or run the eval on `hash:384`, so the demo and the eval measure the same search. bge-small needs `WITH_EMBEDDINGS=1` at build time, which downloads the model from huggingface.co.
2. **Keep `TOP_K` at 3.**
3. **Consider ranking on more than the text.** Findings 3 and 4 show the nearest text is often not the right precedent when only the card side differs. The seeds' `review_reasons` can't tell them apart (`seed_p3` and `seed_p4` list only `large_cut`), so this would need a field that records whether the repayment failed.

4. **Decide what search is for.** The investigator made no search in 8 real-model runs, so as it stands the RAG step is almost never exercised. Either:
   - say in the prompt when a past case helps. Arm B shows one sentence is enough, and that it then retrieves well and doesn't mislead; or
   - present search as optional background in the demo, not as part of how a case is decided.

## Limits

- **Small set:** 15 reviews and 72 queries (24 by hand, 48 generated). The numbers show direction, not precise rates.
- **Hand-written labels:** a different labeller could draw some lines differently. Each `why` in `situations.yaml` says where the line is. The LLM judge agrees with them on 90% of pairs, and the rest are the twin pairs.
- **Agent searches:** 5, all from the experiment arm, one run per case. They show direction only.
