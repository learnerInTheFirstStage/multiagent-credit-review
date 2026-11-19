# Card Service judge (DeepEval)

The test set's code grader (`../checks.py`) holds every finding to its records by ids, figures, counts and a few plain phrasings. It is code on purpose, and it stays the grade. What it cannot read is wording. Its own README says `wording_from_records` "catches the plain phrasings, not every paraphrase".

That gap matters because an analyst reads these findings before approving a card pause or a limit cut. A finding that softens a failed repayment, calls a spiking balance usual or invents a cause is a wrong answer even when every number in it is right. This judge reads the words, and reports **next to** the code grades. It does not replace them.

## What it checks

Each answer's findings are judged against the Card records they cite. Amounts are shown in dollars and shares as percentages, as the analyst reads them. The judge is `claude-opus-5-5`, not the model that wrote the findings. Set `JUDGE_MODEL` to change it.

| Metric | DeepEval | Fails when |
|---|---|---|
| `faithful_to_records` | Faithfulness, threshold 1.0 | a statement contradicts a cited record |
| `states_not_judges` | G-Eval, threshold 0.7 | a finding recommends an action, predicts or reassures, words a record stronger or softer than it is, or gives a cause no record shows |

Faithfulness does not count statements the records neither back nor contradict. Counted, they failed right answers on one run in two ("the bank returned it" for an ACH return). What is unsupported and matters, such as a prediction or a cause, is `states_not_judges`'s job.

## Running it

Its own environment, so the Orchestrator's dependencies do not change:

```bash
uv venv eval/card_service/judge/.venv
uv pip install -p eval/card_service/judge/.venv -r eval/card_service/judge/requirements.txt
set -a; source .env; set +a                                                  # LLM_API_KEY
eval/card_service/judge/.venv/bin/python eval/card_service/judge/judge.py --fixtures   # calibration
eval/card_service/judge/.venv/bin/python eval/card_service/judge/judge.py              # newest model run
eval/card_service/judge/.venv/bin/python eval/card_service/judge/judge.py ../results/RUN.json
```

It reads answers already made by `../run.py`, so it needs no agent and no Card. Results go to `../results/judge.md` (or `judge-fixtures.md`) and a JSON file per run, all git-ignored. DeepEval's telemetry is switched off.

## Calibration: fixtures.yaml

There are nine hand-written answers over Card's recorded records, each with the verdict it must get. Three are right. Six are wrong in wording only, and `checks.general` passes all six:

- a returned repayment "expected to settle within days";
- a 92% balance "in keeping with how this customer has always run the card";
- a failed repayment as "a one-off banking hiccup" with "no sign of stress";
- a cause no record shows ("because the customer moved payroll onto the card");
- a late repayment as "a minor slip for an otherwise reliable payer";
- "it would be prudent to bring the limit down", a recommendation `recommends_an_action` does not catch.

On 2026-11-18 the judge got 9 of 9 on two runs in a row. Right answers scored 1.0 on both metrics, and wrong ones 0.0 to 0.2 on `states_not_judges`. One calibration run costs about $0.32.

## First run on the model (2026-11-18)

The run was claude-sonnet-5-5 on `KAN-36-poc-complete`'s agent. The code grader passed 15 of 19. The judge read the 15 answers with findings, for $0.94:

- **Faithful:** all 15 scored 1.0. No finding contradicts its records.
- **States, not judges:** all 15 passed, scoring 0.7 to 1.0. The low ones are the useful part:
  - **cb_17 (0.7):** the finding says the tools "do not rate the current use of the limit as unusual". The balance record has no such rating, only `steady`, and its 50% is above the usual 42.31%. Carrying the spend record's `unusual: no` over to the balance softens a rise. `checks.py` passed it.
  - **cb_15 (0.8):** the same transfer of "not unusual" from spend to utilization.
  - **cb_01 (0.8):** "no late or failed repayment to address" adds action framing. The judge also saw a finding cut off mid-sentence. The cause is the agent: `guardrails.py` cuts every claim to 500 characters.

## Second run, after the units fix (2026-11-18)

The agent now reads dollars and whole percents. The code grader passed 19 of 19 (cb_10 after the grader fix for "the customer's usual"). The judge read 16 answers, for $1.42:

- **States, not judges:** all 16 passed, scoring 0.7 to 1.0.
- **Faithful:** 15 scored 1.0. cb_11 scored 0.96, a **false alarm** from the judge. It added up $50,000 unpaid and $42,000 of spend to reach the $92,000 balance, treated that as "stated by the records", and failed the finding that says the records state no cause. No record states it. Threshold 1.0 is kept, so a single contradiction in a long answer still fails, and this one is read by hand.
