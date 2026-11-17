# Card Service agent test set

KAN-29. The Credit Behavior Agent tested on its own: one question at a time, straight to the agent (`services/orchestrator/app/agents/card_service`, the Credit Behavior specialist) and the Card Service, without the Orchestrator's graph. The golden set in `eval/` tests the whole case; this set tests the specialist's answers.

## Files

- `cases.yaml`: 18 cases over all 12 scenario customers, each with the records its answer must cite (and must not).
- `checks.py`: the grader. Every answer must also pass five general checks:
  - `valid`: the answer is an `InvestigateResponse` the graph accepts (`build._usable`);
  - `cites_returned`: every evidence record was returned by a tool **in this request**, with the same content;
  - `figures_from_records`: every $ and % in a claim is in the records it cites;
  - `counts_from_records`: every "N paid / late / failed" is the count the cited records give;
  - `no_action`: no limit or card action is recommended.
- `run.py`: the runner. `recorded.json`: Card's answers to every case, recorded from the seeded stack, for the offline test.
- `services/orchestrator/tests/test_card_service_set.py` replays the set offline and checks that the grader catches what it must:
  - a record no tool returned, or one changed after it was returned;
  - a made-up figure or count;
  - a recommendation;
  - "normal" handed back for a customer with no data;
  - a missed change of paying account;
  - a failed repayment reported as paid since.

## Running it

From the repo root, with the Orchestrator's Python environment, and Card up (`docker compose up -d postgres card`):

```bash
services/orchestrator/.venv/bin/python eval/card_service/run.py --no-model            # code only: free, deterministic
services/orchestrator/.venv/bin/python eval/card_service/run.py                       # the model in LLM_MODEL: costs money
services/orchestrator/.venv/bin/python eval/card_service/run.py --no-model --record   # after a Card or seed change
services/orchestrator/.venv/bin/python eval/card_service/run.py --no-model --replay   # no Card needed
```

`results/latest.md` has the table and every finding; `results/<stamp>-<mode>.json` the full answers (both git-ignored). The exit code is 1 if a case failed. The runner signs the agent's token with `JWT_SECRET`, defaulting to compose's local fallback.

Today: 18 of 18 pass with no model, on the live stack and replayed. **Not yet run on a model**: no API key was at hand. That run is the owner's to start, and it is the one that tests the model; the no-model run tests the tools, the guardrails and the code-written facts the model's answer is held to.

## What the cases cover

| What KAN-29 asks | Cases |
|---|---|
| Counts come from tools; every finding cites a record returned in this request | every case (general checks) |
| "Normal" told apart from "no data" | `cb_01_normal` (c_001: findings, all paid, steady) against `cb_02_no_data` (a customer Card does not hold) and `cb_03_card_down` (Card does not answer): no findings, and `could_not_determine` says why |
| A change in how the customer pays is noticed (c_006) | `cb_04` (asked), `cb_05` (not asked, still reported), `cb_06` (c_002: no false alarm) |
| What a failed repayment means, and the right answer for c_010 | `cb_07`, `cb_08`, below |
| No trouble invented where the card side has none | c_002, c_004, c_005, c_007, c_008, c_009, c_011, c_012, whose stories are on the bank side |

## A failed repayment, and c_010

**What "failed" means in our data.** The ACH debit for a statement came back from the customer's bank: `repayments.status = RETURNED`, with the NACHA return code. Card reads it as `card_repayment.status: failed`.

- **R01** (insufficient funds) means the money was not there. That is credit stress.
- R02, R03, R16 and R29 would mean the bank account itself is the problem (closed, not found, frozen, not authorised). That is operational. No scenario customer has one yet.

A failed repayment stays owed until a completed payment for the same statement settles after the return. Card then says `paid_since: yes` and `repaid_after_days`; until then it says `paid_since: no`. A late repayment is different: it was paid, after its due date (`days_late`).

**c_010, Quarry Hill Brewing.** The scenario table left this one open.

- **The facts.** The $50,000 repayment due 2026-11-05 was returned on 2026-11-07 for R01, insufficient funds. Nothing has paid it since (`paid_since: no`). With it still owed, the card is at 92% of its $100,000 limit, and `spiking` against the same day of the three months before (46.7%). On the bank side, payroll and tax explain the cash fall.
- **The right card-side answer:** exactly those facts, each cited. The agent must not say the repayment "will be" or "was probably" paid, must not soften it because the bank side looks fine, and must recommend nothing. `cb_07` and `cb_08` check this.
- **What follows in the Orchestrator** (not this set's call): G-08b, "failed repayment, still unpaid", allows `pause_card`. Until now the stand-in could not say whether it was paid, so `policy.UNPAID_MEANS` counts `not_recorded` as unpaid, and `golden_set.yaml` accepts `reduce_limit` or `pause_card`. With Card now saying `no`, the golden set's owner can narrow c_010 to `pause_card`.

**For KAN-36.** When the graph switches to this agent, the golden set's `must_cite` ids change from the stand-in's to Card's. For c_010 that is `rep_c_010_3` → `rep_c_010_202611`.
