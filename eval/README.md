# eval

The golden dataset run: every test customer through the whole system, scored on cause, recommendation, evidence and correct handoff, next to what the engine would do alone. KAN-42 and KAN-56.

## Files

- `golden_set.yaml`: the cases and their accepted answers. `golden-set.md` is generated from it by `describe.py`; read that before a run.
- `extra_customers.yaml`: the evaluation's own customers, added to `data/shared_customers.yaml`.
- `history.yaml`: the one seeded review every stack has (`seed_c_007_2025`). The image copies it.
- `history_eval.yaml`: the evaluation's other fourteen seeded reviews. Only the evaluation overlay names it.
- `signals.py`: the review signal of each case. `run.py`: the runner. `grade.py`: the grading and the summary. `compare.py`: two arms side by side.

## Running an arm

Three arms run the same cases through the same graph, report writer and check. They differ in who chooses the questions (spec 8.4):

- `agent`: the model chooses, at most 6, with the code gap list as nudges. It can search past cases.
- `fixed`: a written list of 8 questions, every topic that needs no record id. No follow-up.
- `fixed_rules`: the same list, then what the code gap list names, at most 4 more.

One arm per fresh stack, on a fresh volume. `INVESTIGATION_FLOW` sets the arm. Empty means `agent`.

```
INVESTIGATION_FLOW=fixed_rules docker compose -p goldenset -f docker-compose.yml -f eval/docker-compose.eval.yml up -d --build postgres card orchestrator
python eval/run.py --arm fixed_rules --container "docker compose -p goldenset exec -T orchestrator"
docker compose -p goldenset down -v
```

The runner stops before the first signal when:

- the stack runs another arm than `--arm`, or an `INVESTIGATION_FLOW` that names no arm (the service runs such a value as the agent, but a misspelt arm is not counted as one);
- a case is open for one of the customers;
- the database is not clean: `python -m app.seed_history /eval/history.yaml /eval/history_eval.yaml --expect-clean` refuses a review that is not a seed dated before the data clock, or a seed with other content;
- the service does not list every seeded case.

Results go to `eval/results/<run id>/`: `runs.jsonl` as each run ends, `summary.md` at the end. Every record carries `arm`. A graded run also carries the case's question path (`GET /cases/{id}/path`) and its path grade, or `path_error` when the path page could not be read; a run that ended in an error carries neither. A run spends money on the model the stack is set to, about 80 runs for the whole set. The paid run is the owner's to start.

`about` at the top of the summary holds the arm, whether search was on and with which embedder, how many reviews were seeded, every value of `services/orchestrator/app/policy.py` as one JSON line, and the first twelve hex digits of the SHA-256 of `app/graph/prompts.py`. Both are read inside the container.

Once a held-out run exists, a later run with another policy line or another prompt hash is a dev run, and is reported as one.

## Comparing two arms

```
python eval/compare.py eval/results/<run A>/runs.jsonl eval/results/<run B>/runs.jsonl > compare.md
```

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
