# eval

The golden dataset run: every test customer through the Orchestrator, scored on cause, recommendation, evidence and correct handoff, next to what the engine would do alone. KAN-42 and KAN-56. Auth is not started: the runner calls the Orchestrator directly with dev tokens. Every case is closed by a rejection, so no card action is ever sent.

Who answers the graph's questions is `SPECIALISTS` on the stack, and `--specialists` on the runner:

- `real` (the default, `SPECIALISTS` empty): the Cash Flow agent, which asks Bank, and the Credit Behavior agent, which asks Card. A run tests the specialists too, and spends model calls on them. Bank and Card must be up, and seed the evaluation's customers as well as the shared ones (the overlay sets that).
- `stand_in` (`SPECIALISTS=stand_in`): the stand-ins answer from the data files, with no service and no model (`services/orchestrator/app/agents/mock.py`). A run tests the graph, the check and the policy. Every run before KAN-36 was of this kind.

The two kinds do not compare: the runner stops when the stack runs other specialists than `--specialists`, and `summary.md` and every record say which ran.

## Files

- `golden_set.yaml`: the cases and their accepted answers. `golden-set.md` is generated from it by `describe.py` (run it with the Orchestrator's Python environment, since it imports the app); read that before a run.
- `extra_customers.yaml`: the evaluation's own customers. The overlay adds them to the stand-ins' data through `SHARED_CUSTOMERS_FILE`, and to what Bank and Card seed through `SHARED_CUSTOMERS_YAML`; `data/shared_customers.yaml` does not change.
- `history.yaml`: the one seeded review every stack has (`seed_c_007_2025`). The image copies it.
- `history_eval.yaml`: the evaluation's other fourteen seeded reviews. Only the evaluation overlay names it.
- `card_service/`: the Card Service agent's own test set (KAN-29, the Credit Behavior specialist), run straight against the agent and Card. See its README.
- `signals.py`: the review signal of each case. `run.py`: the runner. `grade.py`: the grading and the summary. `compare.py`: two arms side by side.

## Running an arm

Three arms run the same cases through the same graph, report writer and check. They differ in who chooses the questions (spec 8.4):

- `agent`: the model chooses, at most 6, with the code gap list as nudges. It can search past cases.
- `fixed`: a written list of 8 questions, every topic that needs no record id. No follow-up.
- `fixed_rules`: the same list, then what the code gap list names, at most 4 more.

One arm per fresh stack, on a fresh volume. `INVESTIGATION_FLOW` sets the arm. Empty means `agent`.

With the real specialists (the default):

```
INVESTIGATION_FLOW=fixed_rules docker compose -p goldenset -f docker-compose.yml -f eval/docker-compose.eval.yml up -d --build postgres bank card orchestrator
python eval/run.py --arm fixed_rules --container "docker compose -p goldenset exec -T orchestrator"
docker compose -p goldenset down -v
```

The Orchestrator starts only once Bank and Card answer `/health`, which they do once they have seeded. If either fails to start, `up` says so; read its log (`docker compose -p goldenset logs bank`) before a run, since otherwise every question to it would come back `bank_unreachable` or the like.

With the stand-ins:

```
SPECIALISTS=stand_in INVESTIGATION_FLOW=fixed_rules docker compose -p goldenset -f docker-compose.yml -f eval/docker-compose.eval.yml up -d --build postgres bank card orchestrator
python eval/run.py --arm fixed_rules --specialists stand_in --container "docker compose -p goldenset exec -T orchestrator"
docker compose -p goldenset down -v
```

Bank and Card still start, since the Orchestrator waits for them, but nothing asks them.

The runner stops before the first signal when:

- the stack runs another arm than `--arm`, or an `INVESTIGATION_FLOW` that names no arm (the service runs such a value as the agent, but a misspelt arm is not counted as one);
- the stack runs other specialists than `--specialists`, or a `SPECIALISTS` that is neither empty, `real` nor `stand_in` (the service runs such a value as the real agents, so a misspelt `stand_in` is refused rather than run as them);
- a case is open for one of the customers;
- the database is not clean: `python -m app.seed_history /eval/history.yaml /eval/history_eval.yaml --expect-clean` refuses a review that is not a seed dated before the data clock, or a seed with other content;
- the service does not list every seeded case.

Results go to `eval/results/<run id>/`: `runs.jsonl` as each run ends, `requests.json` with the five request checks (t_01 to t_05), `usage.json` when usage was read from LangFuse, `summary.md` at the end. Every record carries `arm` and `specialists`. A graded run also carries the case's question path (`GET /cases/{id}/path`) and its path grade, or `path_error` when the path page could not be read; a run that ended in an error carries neither. A graded run carries `wall_seconds` too, or `wall_error` when the case list could not be read. A run spends money on the model the stack is set to, about 80 runs for the whole set. The paid run is the owner's to start.

A run whose case waits for Bank or Card (`waiting_for_data`), or failed because one of them did not answer, is not graded: its result would measure the stack, not the agent. The runner reads this from the case page's status and `outage`, never from the path page, so a path page that cannot be read hides nothing. The record carries `outage`, the service's own sentence such as "Bank Service unavailable (http_503) since 2026-11-18T10:00Z, retried 0 times, next retry at 2026-11-18T10:01Z", and an `error` that says so. A waiting case stays open, so that customer's later runs are not sent. The runner still writes `summary.md`, then exits non-zero naming the first such run: check the stack and run again on a fresh one.

`must_cite` and `must_reach` name a specialist's record by what it is, `{kind, where}`, not by its id: the stand-ins give the failed repayment `rep_c_010_3`, Card gives it another id. The grader matches a spec against the `records` of the path page (every record of the case, raw data, free text taken out) and counts a cited or returned id whose record matches. A bare id is only for a record the Orchestrator makes (`hist_`, `sig_`, `xs_`). When the path page could not be read, a spec is not graded: `cites` is empty for that run. `golden-set.md` shows each spec with the record it names on the stand-ins.

`about` at the top of the summary holds the arm, the specialists, whether search was on and with which embedder, how many reviews were seeded, every value of `services/orchestrator/app/policy.py` as one JSON line, and the first twelve hex digits of the SHA-256 of `app/graph/prompts.py`. Both are read inside the container.

Once a held-out run exists, a later run with another policy line or another prompt hash is a dev run, and is reported as one.

## Counted run by run

The last section of `summary.md` counts runs, not cases: each run is one decision an analyst sees. Every share is printed beside its counts, and a share of nothing prints `n/a`. Runs that ended in an error and runs whose report gave up (no answer) are listed on their own line. An error is left out of every share. A give-up is a miss in the recall lines, wrong for the guardrail cases and counted in the first-draft line, and is left out of the other shares. The 95% interval (Wilson) takes the runs to be independent. Runs of one case are not, so with three runs per case it is narrower than it should be.

- Limit cuts. A cut is `reduce_limit` or `pause_card`. A case accepts a cut when one of its accepted answers is a cut, and requires one when all of them are. Precision is the runs that cut in a case that accepts a cut, of all runs that cut: when it is low, good customers are cut. Recall is the runs that cut in a case that requires a cut, of all runs in such cases: when it is low, risky customers are missed.
- Hand-overs. The same with `needs_human`, a report code handed over for its low confidence included. Low precision sends a person cases the agent should settle. Low recall settles, without a person, cases that need one.
- Too harsh or too lenient. Each run whose recommendation the case does not accept is one of: an action stricter than every accepted action (a cut the case does not justify), milder than every accepted action (risk left in place), an action where only a person was accepted, a hand-over where an action was required, or other (between accepted actions, or an accepted set that holds a hand-over and an action). The cases are listed under each.
- Cause confusion. A table of the expected cause (the case's accepted causes joined with "or") against the cause given, then each cell off the diagonal with its cases. A right action on a wrong cause is luck, and the cause is what the analyst checks before agreeing.
- Confidence. Per level, the runs and how many had the recommendation right. A wrong run at high confidence is named: it is the one most likely to be approved without a second look.
- Cost and time. Model calls, input and output tokens, cost and wall time per run: the median, the most in one run, and the total. A run that gave up or ended in an error counts here, since it was paid for.
- Guardrails. The cases whose data carries text written to steer the reviewer: a `memo` in the bank feed, which today is c_009 and c_203 (a `note` is a plain description and is not one). Whether every run of them had the recommendation right, and whether a report summary repeats six words of the memo in a row, the rule of `quotes()` in `services/orchestrator/app/agents/cash_flow/guardrails.py`. Then the share of runs whose first draft the service's own check refused, from the path.

### Usage and wall time

With `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` (and `LANGFUSE_BASE_URL`) set where the runner runs, and the `langfuse` package installed there, the runner reads each case's generations from LangFuse 30 seconds after the last run, into `usage.json`. A case is one LangFuse session, named by the service's case id. Without the keys or the package, or when LangFuse fails, the section says why usage was not recorded, and the runs grade the same. After two failed reads in a row the runner asks for no more.

LangFuse records every model call twice: a generation from the LangChain callback (`ChatAnthropic`), and the Anthropic SDK's generation (`anthropic.messages.create`) inside it. The runner counts only a generation whose parent is not a generation, told apart by `type` and `parentObservationId`, not by name. Its cost counts the thinking tokens, which are billed as output; the SDK's copy leaves reasoning out. For the same reason, LangFuse's own total for a trace shows about double. Input and output tokens add back the parts LangFuse keeps apart, such as `input_cache_read` and `output_reasoning`.

Wall time runs from the case's `created_at` to its `updated_at`, read from the case list (`GET /cases?status=needs_approval`) before the decision moves `updated_at`. When the list cannot be read, the runner's own clock is used, to within its 2-second poll. The summary says how many runs were timed each way.

### Why no model judges

The grader is code only. The accepted answers are labels (an action, a cause, records the report must cite), so matching them needs no judgment, and a model judge would add its own errors and cost to every run. The figures and citations in a report are already checked by code, the service's report check, before the report reaches a person.

## Comparing two arms

```
python eval/compare.py eval/results/<run A>/runs.jsonl eval/results/<run B>/runs.jsonl > compare.md
```

Compare only runs of the same specialists: `compare.py` does not check it. A record with no `specialists` is from before KAN-36, and ran the stand-ins.

Per case: its class from `golden-set.md` (R: the fixed list reaches the answer; D: the answer needs a question about one record an earlier answer listed), and for each arm whether it was right in every run on the first checked draft and on the final report. Then, per class and check, the cases both arms got right, only A, only B, and neither, first for the first draft and then for the final report. Then the questions each arm asked, refused calls, runs that ended at the round limit and give-ups. And for the agent on class D, whether it reached the records the case must reach before or after a code gap named them. A case with a run missing in either file is left out and named.

Quote the first-draft table for what the model does. The final report is the model and the check together. A first draft that was not a report counts as wrong on every check the case has, in `summary.md` and here.

As `golden_set.yaml` stands, the class D split by nudge tells something for c_009 and c_011 only. c_307's `must_reach` does not hold its payee's history, so a c_307 run shows as reached unprompted whether or not it asked for that history.

## The overlay

`docker-compose.eval.yml` builds the image with the fastembed model, mounts the evaluation's customers and both seed files read-only, and sets `HISTORY_SEED_FILE=/eval/history.yaml:/eval/history_eval.yaml`, so the service seeds all fifteen reviews at start.

Check that it merges with the main file before a paid run. This starts nothing:

```
docker compose -f docker-compose.yml -f eval/docker-compose.eval.yml config
```

If `build.args` does not merge there, pass `--build-arg WITH_EMBEDDINGS=1` on the build. On a machine that cannot reach huggingface.co at build time, build with `WITH_EMBEDDINGS=0` and run with `EMBEDDINGS_MODEL=hash:384`, the built-in embedder.
