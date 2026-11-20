# Card Service agent test set

KAN-29. The Credit Behavior Agent tested on its own: one question at a time, straight to the agent (`services/orchestrator/app/agents/card_service`, the Credit Behavior specialist) and the Card Service, without the Orchestrator's graph. The golden set in `eval/` tests the whole case; this set tests the specialist's answers.

## Files

- `cases.yaml`: 19 cases over all 12 scenario customers, each with the records its answer must cite (and must not).
- `checks.py`: the grader. Every answer must also pass six general checks:
  - `valid`: the answer is an `InvestigateResponse` the graph accepts (`build._usable`);
  - `cites_returned`: every evidence record was returned by a tool **in this request**, with the same content;
  - `figures_from_records`: every $ and % in a claim is in the records it cites;
  - `counts_from_records`: every "N paid / late / failed" is the count the cited records give (a date such as "due 2026-11-05 failed" or "due Nov 5 failed" is not a count; "1 of 3 failed" is 1);
  - `wording_from_records`: a claim citing a repayment with `paid_since: no` does not say it was paid since or later, or will or probably will be paid; a claim citing a `card_balance` whose trend is rising or spiking does not call it normal, usual, steady or the like. Code only, by pattern, so it catches the plain phrasings, not every paraphrase;
  - `no_action`: no limit or card action is recommended.
  - The runner adds `card_calls`: every call to Card was answered. Replayed, a call with no recording fails the case; recording, a reply that is not JSON is not recorded and fails the case.
- `run.py`: the runner. `recorded.json`: Card's answers to every case, recorded from the seeded stack, for the offline test.
- `services/orchestrator/tests/test_card_service_set.py` replays the set offline and checks that the grader catches what it must:
  - a record no tool returned, or one changed after it was returned;
  - a made-up figure or count, and no count read from a date;
  - a recommendation;
  - "normal" handed back for a customer with no data;
  - a missed change of paying account;
  - a failed repayment reported as paid since, by its record or by its wording;
  - a rising or spiking balance called normal or steady;
  - a replayed call with no recording, and a Card reply that is not JSON.

## Running it

From the repo root, with the Orchestrator's Python environment, and Card up (`docker compose up -d postgres card`):

```bash
services/orchestrator/.venv/bin/python eval/card_service/run.py --no-model            # code only: free, deterministic
services/orchestrator/.venv/bin/python eval/card_service/run.py                       # the model in LLM_MODEL: costs money
services/orchestrator/.venv/bin/python eval/card_service/run.py --no-model --record   # after a Card or seed change
services/orchestrator/.venv/bin/python eval/card_service/run.py --no-model --replay   # no Card needed
```

`results/latest.md` has the table and every finding; `results/<stamp>-<mode>.json` the full answers (both git-ignored). The exit code is 1 if a case failed. The runner signs the agent's token with `JWT_SECRET`, defaulting to compose's local fallback.

`recorded.json` holds only the calls code makes with no model: each topic's preflight, with fixed arguments. A model's own tool calls are not in it, so a model run needs the live stack. Replayed with a model, any call outside the recording fails its case (`card_calls`) rather than passing as "Card did not answer".

## What 19 of 19 means today

Today: 19 of 19 pass with no model, replayed. The first 18 also passed on the live stack when `recorded.json` was made; `cb_19` never calls Card.

**With no model, the findings come from the agent's code, not from judgment.** `findings.from_records` writes one finding per record, word for word from its numbers. So the no-model run tests Card's data (the seed, the tools' arithmetic, the trends) and that code, plus the guardrails and this grader. It does not test whether a model reads the records right.

**With a model**, the model writes the findings and the guardrails drop the ones that break the rules. Code then adds its own finding for each hard fact no accepted finding cites: a failed or late repayment, and a change of the account that pays the card. A model that leaves those out still passes on them; what it writes about everything else is its own.

**Run on a model** on 2026-11-18 (claude-sonnet-5-5): the code grader passed 15 of 19, then 19 of 19 after the units fix. Both runs, and the judge's reading of them, are in `judge/README.md`.

## What the cases cover

| What KAN-29 asks | Cases |
|---|---|
| Counts come from tools; every finding cites a record returned in this request | every case (general checks) |
| "Normal" told apart from "no data" | `cb_01_normal` (c_001: findings, all paid, steady) against `cb_02_no_data` (a customer Card does not hold) and `cb_03_card_down` (Card does not answer): no findings, and `could_not_determine` says why |
| A change in how the customer pays is noticed (c_006) | `cb_04` (asked), `cb_05` (not asked, still reported), `cb_06` (c_002: no false alarm) |
| What a failed repayment means, and the right answer for c_010 | `cb_07`, `cb_08`, below |
| No trouble invented where the card side has none | c_002, c_004, c_005, c_007, c_008, c_009, c_011, c_012, whose stories are on the bank side |
| A question the card side cannot answer | `cb_19_unknown_topic`: no findings, "no such topic", and no call to Card |

### Known gaps

Cases from the PRD and the course that this set does not cover yet. Most need new Card seed data:

- a repayment returned for **R02** (account closed) or another code that means the bank account is the problem, not the money;
- a failed repayment **paid since** (`paid_since: yes`, `repaid_after_days`);
- a balance or spend series that says **`too_short_to_tell`** (a new customer);
- a **stale card feed** (Card's data stops before the review window ends);
- a **spend spike at a new vendor**;
- a **seasonal dip** on the card side (c_007's is on the bank side only);
- **injected text** in card data (a memo or merchant name that tries to instruct the agent).

## A failed repayment, and c_010

**What "failed" means in our data.** The ACH debit for a statement came back from the customer's bank: `repayments.status = RETURNED`, with the NACHA return code. Card reads it as `card_repayment.status: failed`.

- **R01** (insufficient funds) means the money was not there. That is credit stress.
- R02, R03, R16 and R29 would mean the bank account itself is the problem (closed, not found, frozen, not authorised). That is operational. No scenario customer has one yet.

A failed repayment stays owed until a completed payment for the same statement settles after the return. Card then says `paid_since: yes` and `repaid_after_days`; until then it says `paid_since: no`. A late repayment is different: it was paid, after its due date (`days_late`).

**c_010, Quarry Hill Brewing.** The scenario table left this one open until 2026-11-17; it now says `pause_card`.

- **The facts.** The $50,000 repayment due 2026-11-05 was returned on 2026-11-07 for R01, insufficient funds. Nothing has paid it since (`paid_since: no`). With it still owed, the card is at 92% of its $100,000 limit, and `spiking` against the same day of the three months before (46.7%). On the bank side, payroll and tax explain the cash fall.
- **The right card-side answer:** exactly those facts, each cited. The agent must not say the repayment "will be" or "was probably" paid, must not call the 92% normal, and must recommend nothing. `cb_07` and `cb_08` cite the records (`expect.records`); `wording_from_records` holds the wording to them (and `cb_11` for the spiking balance); `no_action` checks for a recommendation. Not softening it because the bank side looks fine is not checked beyond those phrasings.
- **What follows in the Orchestrator** (not this set's call): G-08b, "failed repayment, still unpaid", allows `pause_card`. The stand-in cannot say whether it was paid, so `policy.UNPAID_MEANS` counts `not_recorded` as unpaid. With Card saying `no`, `golden_set.yaml` accepts `pause_card` only (decided 2026-11-17).

**For KAN-36.** When the graph switches to this agent, the golden set's `must_cite` ids change from the stand-in's to Card's. For c_010 that is `rep_c_010_3` → `rep_c_010_202611`. For c_006, the paying-account change record's id is moving to `rep_c_006_funding`, a stable id, in a parallel Card change; `recorded.json` is re-recorded with it.
