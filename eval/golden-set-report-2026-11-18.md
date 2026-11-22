# Golden set runs, 2026-11-17 and 2026-11-18

The full golden set ran once on 2026-11-18 on Claude Sonnet 5.5 at c5ffe0f (PR #31's code, merged on 2026-11-19), with the real Cash Flow and Credit Behavior agents, which ask Bank and Card. This report covers that run, the runs that led there and the Opus run on its misses. Grading is by code against `eval/golden_set.yaml`; no model judges. Every case of the full and Opus runs is listed at the end with its LangFuse session.

Times are US Pacific (UTC-8), the clock in the run ids. LangFuse shows the same runs in UTC, so the full run appears there on 2026-11-19.

## Result

Full run `20261118-184057-agent`, 27 cases, one run each:

- Recommendation right for 24 of 27 (89%, 95% interval 72% to 96%), cause right for 23 of 27 (85%, 68% to 94%). On the 25 held-out cases: 22 and 21.
- No limit cut where the case does not accept one (6 of 6), and no wrong answer at high confidence.
- Doing nothing clever: the engine alone (always cut) is right for 6 of 27, always handing over for 4 of 27.
- The 14 cases graded together on 2026-11-17 (the team's 12 scenarios plus c_013 and c_203) went from cause right on 11 to 13 in the full run; their recommendation was right on 13 both times. Outside the README and tests, their code (660e2c1, 836e1ec) has the same files as 9e230f6, the commit PR #31 changed, so PR #31 is the only code difference.
- $11.51 in model cost ($0.43 a case on average, at most $0.67), and a median of 95 seconds from the case opening to its report (at most 152). The runner had no LangFuse keys, so its own summary records no usage. The cost comes from LangFuse and covers every model call in the case's session, the Cash Flow and Credit Behavior agents' calls included.

## How the result got there

Terms used below. The investigator is the Orchestrator's model that picks the questions to Bank and Card. The check is the service's code check of each report draft. A refused draft is redrafted once, with the check's problems; if the redraft is refused too, the service gives up and hands the case to a person with no cause and no evidence. A report that passes the check at low confidence is handed over by code. The gap pass runs when the investigator stops asking. Code lists questions nobody asked that a guideline still needs, a model reviews causes no question has tested, and anything open goes back to the investigator. The grader counts a give-up as wrong on recommendation, cause, hand-over and, where the case names them, citations, even when a person is the accepted answer.

| When (PT) | Run | Code and change | Cases | Result |
|---|---|---|---|---|
| 11-17 18:21 | not the runner (`scripts/e2e.py`) | Real Cash Flow and Credit Behavior agents for the first time, on the old bank feed | c_203, c_106 | Recommendation right on neither. Both handed over: the check refused both c_203 drafts, and c_106 passed at low confidence although its cause was right |
| 11-17 19:50 | not the runner | Complete bank feed | c_203, c_106, c_009, c_007 | Stopped on a model provider error before any report, so not graded. The check had refused the first drafts of c_203 and c_106 |
| 11-17 21:18 and 23:20 | `20261117-211836-agent` (660e2c1), `20261117-232013-agent` (836e1ec, which differs from 660e2c1 only in the README and tests) | Payee history for every payee; G-02 reads only its own payment's history | 14: c_001 to c_013, c_203 | Recommendation 13 of 14 (c_007 gave up), cause 11 of 14 (c_006, c_007 and c_012 wrong) |
| 11-18 17:32 | `20261118-173257-agent` (9e230f6 with the G-05 change, uncommitted) | G-05 needs the record of which account pays the card | c_006, c_106 | c_006 asked Card but still named revenue_loss; c_106 was handed over at low confidence |
| 11-18 17:45 | `20261118-174517-agent` (9e230f6 with the G-05 change and the cause meanings, uncommitted) | The G-05 change and a one-line meaning per cause | c_002, c_005, c_006, c_012, c_106 | 5 of 5 on recommendation, cause, hand-over and citations |
| 11-18 18:41 | `20261118-184057-agent` (c5ffe0f) | The same, as committed, with the funds_moved_out_of_view and seasonal_dip meanings tightened after the five-case check, and POLICY_VERSION 2026-11-18 | all 27 | Recommendation 24 of 27, cause 23 of 27 |
| 11-18 18:58 | `20261118-185811-agent` (c5ffe0f, Opus 5.5) | Opus on the full run's four misses | c_007, c_107, c_307, c_314 | 3 of 4 |
| 11-18 19:06 | `20261118-190602-agent` (c5ffe0f with a reworded unexplained_outflow meaning, uncommitted) | Sonnet on the four misses again | the same four | Stopped on a model provider error at the first model call, so not graded |

The two 11-18 experiments both say 9e230f6-dirty in their summaries. The prompts.py hash there tells them apart: 81972768a026 at 17:32, as at 9e230f6, before prompts.py showed the cause meanings, and 33de7a7757cc at 17:45, as at c5ffe0f. Both still carry POLICY_VERSION 2026-11-17; the bump to 2026-11-18 came with c5ffe0f.

Two other runs bear on these figures. On 2026-11-17 at 07:07, before its first row, one c_010 run on the real model with the stand-in specialists was right (LangFuse session case_40c1a643). At 21:22 that evening, on the 21:18 run's code, `scripts/e2e.py` ran c_013 a second time (session case_0873c7a8), and it was handed over at low confidence. The 13 of 14 counts each case's first run.

## What changed, and what each change did

All four pull requests are KAN-36.

### Test data (PRs #26 and #28, merged 2026-11-18)

Before these PRs, the bank feed on main listed only the payments each scenario turned on, plus one filler transaction that made the week add up. For 12 of the 26 customers then in the golden set (PR #26 added c_013 as the 27th), more than half of the week's fall had no real transaction behind it, from 64% to 110%. For c_005 and c_012 that gap is intended. For c_009 it was 70%: $107,000 of a $152,000 fall. The first run on the real agents (11-17 18:21) used an earlier rewrite of this feed, which kept the same gap in unlisted rows, and it handed over both its cases, c_203 and c_106, which had 109% and 88% of their falls unexplained.

- Every movement of the week is listed: receipts, running costs (a new category, `operating`), payroll, tax, vendor payments and transfers. The Bank seed refuses a week that does not add up, unless the file declares the gap (c_005, c_012).
- Cash a week ago was recomputed from the listed movements for 15 customers: 6 of the team's 12 and 9 evaluation customers. Outside the intended gaps of c_005 and c_012, that removed 55% of the old unexplained amount; c_009's fall became $45,000, all of it the vendor payment. Cash now did not change, so engine limits and review reasons stayed the same.
- 13 weeks of history before the window, with payroll every two weeks and tax every quarter, and last year's matching weeks itemised.
- Payee history answers for every payee. Running costs, payroll and tax have earlier payments. A scenario's known vendor has them only where the file gives a payee history hint (c_009, c_011).
- Monthly card spend lowered to 45% of the limit for 10 customers, so balances stay under their limits. New case c_013: steady cash and a failed repayment still unpaid, so Card's records alone decide it.

c_203's cause came right in `20261117-211836-agent`, once running-cost payees had history. In the 19:50 run before it they read "never paid before", and c_203's first draft, which the check refused, named an unexplained outflow. c_106 was not graded right until PR #31.

### Rules and system (PRs #26 to #28, merged 2026-11-18)

- G-02 (planned vendor payment) is settled only by the history of the payment it is about. Before this fix, once every payee had a history, a running cost's history could rule G-02 out and break c_009. Tests caught it before any real run.
- Bank's evidence uses the record kinds, keys and ids the Orchestrator's rules read, and a record id from Bank or Card always carries the same content.
- When Bank or Card gives nothing, the case waits and asks again after 1, 5 and 15 minutes, then fails with the reason. It is never handed over as if nothing were found. POLICY_VERSION 2026-11-17.
- LangFuse records each model call once. Before, the Anthropic SDK's copy was sent too, and cost showed about 1.9 times the real figure.
- Golden set: evidence is matched by record kind and data, not by stand-in ids. c_010 accepts only pause_card. c_009 names the payment whose payee history must be reached, and c_011 the one whose payee history must be cited. c_013 made it 27 customers.
- The grader reports precision and recall of cuts and hand-overs, too harsh or too lenient, cause confusion, confidence, cost, time and guardrails.

### Cause fix (PR #31, merged 2026-11-19)

Before PR #31, two of the 14 had the right recommendation and the wrong cause.

c_006: income stopped while card repayments stayed on time, paid from a new account since 2026-09-20. The cause is funds moved out of view.
- The investigator asked Bank five questions and Card none. G-05 (income no longer visible while repayments continue; it allows request_documents) was already met by Bank's income answer and the case's own repayment record, so code found no gap and did not send the investigator to Card.
- No cause had a stated meaning. The report writer, which picks the cause, saw only the names, and the model read funds_moved_out_of_view as needing a transfer out.
- Changes: G-05 can be cited only with the record of which account pays the card, so when nothing else is permitted the gap pass asks Card for it. Each cause has a one-line meaning, shown to the investigator and the report writer; funds_moved_out_of_view covers a card now paid from an unlinked account that no linked account pays into, or a transfer to the customer's own account elsewhere that makes up most of the fall.
- With the G-05 change alone, c_006 asked Card for the paying account but still named revenue_loss. With both changes it named funds_moved_out_of_view, in the five-case check and in the full run. The meanings were not tried alone, and each result is one run.
- After the change, the investigator asked Card for the paying account on its own, before any gap pass, so the gap pass never had to.

c_012: cash fell $290,000 and the window's transactions explain $95,000, so balances and transactions do not reconcile. It named no_clear_cause where the golden set expects insufficient_data. The meaning of insufficient_data now covers books that do not reconcile, and c_012 was right in the five-case check and the full run.

Also in PR #31: c_103's reference path asks Card for the paying account, since c_103 lists G-05. POLICY_VERSION is 2026-11-18. A regression test failed on main before PR #31 merged, and a test checks that every cause has a meaning in the report brief. The Orchestrator suite passed (1911 tests). On the stand-ins, all 27 customers permit the same actions and hand-overs as before, once the paying account is asked.

## Full results

From the runner's summary and, for cost, LangFuse:

| Measure | Result |
|---|---|
| Recommendation right | 24 of 27 (89%, 95% interval 72% to 96%) |
| Cause right | 23 of 27 (85%, 68% to 94%) |
| Handed over when it should, and only then | 24 of 27 (89%) |
| Cited the evidence the answer turns on | 14 of the 17 cases that name it (82%) |
| Limit cuts: precision, recall | 6 of 6, 6 of 7 |
| Hand-overs: precision, recall | 3 of 3, 3 of 4 |
| Too harsh, too lenient | 0, 0 |
| Wrong at high confidence | 0 |
| First draft refused by the check | 5 of 27 (c_006 and c_008 then passed; c_007, c_107 and c_307 gave up) |
| Paired cases that held | 11 of 13 |
| Requests the service must refuse (t_01 to t_05) | 5 of 5 refused |
| Model cost | $11.51; $0.43 a case on average, median $0.41, at most $0.67 |
| Time from case open to report | median 95 seconds, at most 152 |

By group:
- The 14 graded on 2026-11-17: recommendation 13 of 14, cause 13 of 14.
- The other 13 evaluation cases (c_102, c_103, c_106, c_107, c_110, c_111, c_201, c_208, c_301, c_303, c_304, c_307, c_314): recommendation 11 of 13, cause 10 of 13. Before this run, only c_106 of these had run.

The paired-case line counts 13 pairs, each listed once under one of its cases, with the test for its kind of pair in `eval/grade.py` (`compare`). They are not the same 13 cases as the group above: c_203 is listed for its pair with c_003, and c_111's pair is listed under c_110. The two that did not hold are c_107 and c_307, set against c_007.

## The four misses

The full run missed four cases: c_007, c_107 and c_307 on recommendation and cause, and c_314 on cause only.

| Case | What happened |
|---|---|
| c_007, c_107, c_307 | The check refused both drafts, the service gave up, and the case went to a person |
| c_314 | Handed over correctly in one draft, with the wrong cause: payroll_or_tax, where the case accepts revenue_loss or no_clear_cause |

The three gave-up cases carry the same $150,000 supplier payment, categorised `vendor_known`. Bank has no earlier payment to that supplier. c_307 is c_007's data with a different earlier review; c_107 has c_007's payment and earlier review, with lower, shrinking income and a late repayment. Sonnet named unexplained_outflow and asked for documents under G-09 (payments to new payees with unknown purpose). The check refused both drafts on two rules, and both read the bank's category, not payee history. G-09 needs payments categorised `vendor_new` that together make up at least a quarter of the fall. unexplained_outflow needs one categorised `vendor_new`, `transfer_other` or `other`. The supplier payment is `vendor_known`, so neither held.

c_007 failed the same way on 2026-11-17, in `20261117-211836-agent`, before the cause meanings existed, so the meanings did not cause it. c_107 and c_307 first ran in the full run.

Giving the supplier a payment history would let G-02 (planned vendor payment, keep the limit) apply. c_307 would then no longer be a hand-over, and c_007's accepted causes would have to change. The data was left as it is.

None of the four led to an action the case does not accept. Three went to a person after the service gave up, and c_314 was handed over as required. c_107's required cut was not made, which is the one miss in cut recall. c_307 is the one miss in hand-over recall: it went to a person, its accepted answer, but through a give-up. The three give-ups are also the three misses in cited evidence.

## Opus 5.5 on the four misses

One run each, same code:

| | Sonnet 5.5 | Opus 5.5 |
|---|---|---|
| Recommendation right | 1 of 4 (c_314) | 3 of 4 (c_007, c_107, c_307) |
| Cost of these four | $2.28 | $3.90, about 1.7 times |

Opus's first drafts on c_007, c_107 and c_307 all named unexplained_outflow, as Sonnet's did. On c_007 and c_307 its first draft also asked for documents under G-09 and was refused on the same two rules as Sonnet's; the records it cited differed. On c_107 its first draft already had the right recommendation (reduce_limit under G-07, revenue decline with repayment trouble) and was refused only for the cause. After the refusal, each second draft was right; Sonnet's second drafts repeated the first. On first drafts alone, Opus had 1 of 4 recommendations and 0 of 4 causes right. On c_314 Opus gave up. Its first draft cited G-05 without the records G-05 needs, and its second handed over without citing a hand-over guideline. Sonnet had handed c_314 over correctly in one draft.

Opus did not run on the full set, so it has no overall figure. The POC stays on Sonnet 5.5, and the remaining misses were accepted.

## Limits

- To keep cost down, each case ran once and only the agent arm ran, not fixed or fixed_rules (`eval/README.md`, "Running an arm"). A full run costs about $12. Run-to-run stability is unknown, and the intervals are wide.
- The single-agent baseline (KAN-43) was cut and not built.
- A rewording of the unexplained_outflow meaning was tried: a payment to a known vendor does not count, even with no earlier payment on record. Its Sonnet re-run (`20261118-190602-agent`) stopped before any case completed. A comparison was inconclusive: Claude Code subagents wrote the report drafts in place of the model the service calls, for c_007, c_107 and c_307 under both wordings on two fixed sets of investigator questions, 12 drafts in all. All 12 had the right recommendation and cause and none named unexplained_outflow, so the miss was not reproduced, and the investigator, which also sees the meanings, was not run. The rewording was not committed.
- In these runs the model never called the past-case search, although search was on.
- Main has changed since c5ffe0f (among others, PR #30's Credit Behavior agent change and KAN-69's prompt change on when to search), and no full run has been made since.

## Every case

Right means both the recommendation and the cause are among the accepted answers. A give-up counts as wrong. Cost and seconds come from LangFuse, from the case opening to its report. Each link opens the case's session, with every question, answer, draft and check.

Sonnet 5.5, full run `20261118-184057-agent`:

| Case | Accepted | Recommended | Cause | Right | Cost ($) | Seconds | LangFuse |
|---|---|---|---|---|---|---|---|
| c_001 | keep_limit | keep_limit | payroll_or_tax | yes | 0.40 | 84 | [case_259a403c](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_259a403c) |
| c_002 | keep_limit | keep_limit | funds_moved_out_of_view | yes | 0.35 | 75 | [case_1ad25a0a](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_1ad25a0a) |
| c_003 | reduce_limit | reduce_limit | revenue_loss | yes | 0.56 | 122 | [case_ada57af3](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_ada57af3) |
| c_004 | request_documents | request_documents | payroll_or_tax | yes | 0.45 | 108 | [case_223ed237](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_223ed237) |
| c_005 | request_documents | request_documents | insufficient_data | yes | 0.38 | 82 | [case_45896b51](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_45896b51) |
| c_006 | request_documents | request_documents | funds_moved_out_of_view | yes | 0.54 | 124 | [case_0028d27b](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_0028d27b) |
| c_007 | keep_limit | gave up | gave up | no | 0.47 | 125 | [case_365bce44](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_365bce44) |
| c_008 | request_documents | request_documents | unexplained_outflow | yes | 0.34 | 82 | [case_29c298ec](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_29c298ec) |
| c_009 | keep_limit | keep_limit | planned_vendor_payment | yes | 0.45 | 95 | [case_714e15c2](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_714e15c2) |
| c_010 | pause_card | pause_card | payroll_or_tax | yes | 0.54 | 135 | [case_8414b582](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_8414b582) |
| c_011 | needs_human | needs_human | no_clear_cause | yes | 0.22 | 52 | [case_2434d988](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_2434d988) |
| c_012 | needs_human | needs_human | insufficient_data | yes | 0.51 | 108 | [case_05e9f851](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_05e9f851) |
| c_013 | pause_card | pause_card | no_clear_cause | yes | 0.41 | 99 | [case_bcb55d51](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_bcb55d51) |
| c_102 | request_documents | request_documents | unexplained_outflow | yes | 0.21 | 48 | [case_72e9ee56](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_72e9ee56) |
| c_103 | reduce_limit, request_documents | request_documents | revenue_loss | yes | 0.33 | 80 | [case_93fa692a](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_93fa692a) |
| c_106 | reduce_limit, request_documents | request_documents | revenue_loss | yes | 0.52 | 103 | [case_17c56aad](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_17c56aad) |
| c_107 | reduce_limit | gave up | gave up | no | 0.51 | 119 | [case_d59dcb39](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_d59dcb39) |
| c_110 | pause_card | pause_card | payroll_or_tax | yes | 0.60 | 143 | [case_809d9dbd](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_809d9dbd) |
| c_111 | keep_limit | keep_limit | payroll_or_tax | yes | 0.61 | 143 | [case_a2be77cb](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_a2be77cb) |
| c_201 | keep_limit | keep_limit | payroll_or_tax | yes | 0.26 | 54 | [case_1be91d31](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_1be91d31) |
| c_203 | reduce_limit | reduce_limit | revenue_loss | yes | 0.41 | 90 | [case_f362be51](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_f362be51) |
| c_208 | request_documents | request_documents | unexplained_outflow | yes | 0.25 | 55 | [case_f21698ba](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_f21698ba) |
| c_301 | keep_limit | keep_limit | payroll_or_tax | yes | 0.19 | 42 | [case_c40a7b82](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_c40a7b82) |
| c_303 | reduce_limit | reduce_limit | revenue_loss | yes | 0.39 | 89 | [case_57bc8133](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_57bc8133) |
| c_304 | request_documents | request_documents | payroll_or_tax | yes | 0.32 | 80 | [case_e44fd490](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_e44fd490) |
| c_307 | needs_human | gave up | gave up | no | 0.67 | 135 | [case_1ed80994](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_1ed80994) |
| c_314 | needs_human | needs_human | payroll_or_tax | cause wrong | 0.63 | 152 | [case_925c21f8](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_925c21f8) |

Opus 5.5, `20261118-185811-agent`:

| Case | Accepted | Recommended | Cause | Right | Cost ($) | Seconds | LangFuse |
|---|---|---|---|---|---|---|---|
| c_007 | keep_limit | keep_limit | seasonal_dip | yes | 0.86 | 133 | [case_ddd42248](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_ddd42248) |
| c_107 | reduce_limit | reduce_limit | revenue_loss | yes | 0.94 | 149 | [case_db9ae31e](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_db9ae31e) |
| c_307 | needs_human | needs_human | no_clear_cause | yes | 0.93 | 149 | [case_15245efd](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_15245efd) |
| c_314 | needs_human | gave up | gave up | no | 1.17 | 200 | [case_a423a36b](https://us.cloud.langfuse.com/project/cmwjz4lem03b2ad0mwuuyo28h/sessions/case_a423a36b) |

The grades are the runner's own. Graded again from the case states in LangFuse with `eval/grade.py` at c5ffe0f, every grade matches. The runner's output for these runs, and for the earlier ones in the history table, is not in the repository.
