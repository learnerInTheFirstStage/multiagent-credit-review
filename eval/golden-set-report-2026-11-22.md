# Golden set run, 2026-11-22

All 27 golden-set customers ran once on Claude Sonnet 5.5 (`claude-sonnet-5-5` in LangFuse) with the real Cash Flow and Credit Behavior agents, which ask Bank and Card, on the agent flow. The code was PR 47's head, e953bd8; its investigation code (`app/graph`, `app/agents`, the guidelines, policy and prompts) is the same as `main` at bc6af57, which adds PR 49 (the Kubernetes files and Cloud Storage for the monthly report) and docs. POLICY_VERSION 2026-11-18, `prompts.py` hash 9bc3617f75ac (33de7a7757cc in the 2026-11-18 run).

This run did not go through `eval/run.py`. The signals came from `eval/signals.py` and were sent to a local stack five at a time, and every case was left waiting for the analyst, so the cases can be shown (`demo/`). Each report was graded with `eval/grade.py` (`grade` and `grade_path`), as `scripts/e2e.py` grades. Grading is by code against `eval/golden_set.yaml`; no model judges.

## Result

- Recommendation right for 24 of 27, cause right for 23 of 27, hand-over right for 24, guideline right for 24. The same as on 2026-11-18 (`golden-set-report-2026-11-18.md`), with the same four misses: c_007, c_107 and c_307 gave up after the check refused both drafts, and c_314 was handed over with a cause the case does not accept.
- No limit cut or pause where the case does not accept one. The three cuts (c_003, c_203, c_303) and three pauses (c_010, c_110, c_013) are all accepted answers.
- $11.71 for 534 model calls (4.24 million input and 0.32 million output tokens): $0.41 a case at the median, at most $0.66. From LangFuse, every model call in each case's session, the specialists' included.
- A median of 102 seconds from the signal to the report (51 to 163), timed by polling every 10 seconds.
- The check passed 22 of 27 first drafts. It refused the first draft of c_007, c_107, c_111, c_307 and c_314; c_111 and c_314 passed on their second.
- The past-case search ran once in every case. In the 2026-11-18 run it never ran; KAN-69's prompt change asks for one search after the specialists answer.

## Every case

| Customer | Case | Recommendation | Accepted | Cause | Rec. | Cause | Questions | Specialist tool calls | Drafts | Model calls | Cost | Seconds |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| c_001 | `case_664d2681` | keep_limit | keep_limit | payroll_or_tax | right | right | 5 | 8 | 1 | 19 | $0.38 | 92 |
| c_002 | `case_d183bdda` | keep_limit | keep_limit | funds_moved_out_of_view | right | right | 3 | 8 | 1 | 13 | $0.27 | 61 |
| c_003 | `case_5f03f548` | reduce_limit | reduce_limit | revenue_loss | right | right | 5 | 13 | 1 | 24 | $0.47 | 102 |
| c_004 | `case_470878e5` | request_documents | request_documents | payroll_or_tax | right | right | 5 | 16 | 1 | 25 | $0.60 | 123 |
| c_005 | `case_b99e9a27` | request_documents | request_documents | insufficient_data | right | right | 4 | 13 | 1 | 19 | $0.40 | 92 |
| c_006 | `case_3d5efc12` | request_documents | request_documents | funds_moved_out_of_view | right | right | 5 | 10 | 1 | 19 | $0.40 | 92 |
| c_007 | `case_7ce34ee5` | needs_human | keep_limit | no_clear_cause | wrong | wrong | 4 | 11 | 2 | 21 | $0.47 | 133 |
| c_008 | `case_6de560a7` | request_documents | request_documents | unexplained_outflow | right | right | 4 | 11 | 1 | 16 | $0.31 | 72 |
| c_009 | `case_cc92c3fd` | keep_limit | keep_limit | planned_vendor_payment | right | right | 6 | 17 | 1 | 26 | $0.55 | 112 |
| c_010 | `case_1e7d1f94` | pause_card | pause_card | payroll_or_tax | right | right | 6 | 15 | 1 | 24 | $0.54 | 123 |
| c_011 | `case_13562897` | needs_human | needs_human | no_clear_cause | right | right | 5 | 12 | 1 | 18 | $0.37 | 72 |
| c_012 | `case_0f4464ad` | needs_human | needs_human | insufficient_data | right | right | 6 | 14 | 1 | 24 | $0.55 | 112 |
| c_013 | `case_3cffa30b` | pause_card | pause_card | no_clear_cause | right | right | 6 | 12 | 1 | 22 | $0.50 | 112 |
| c_102 | `case_0b3f461b` | request_documents | request_documents | unexplained_outflow | right | right | 3 | 8 | 1 | 13 | $0.25 | 51 |
| c_103 | `case_f861304e` | request_documents | reduce_limit, request_documents | revenue_loss | right | right | 5 | 13 | 1 | 20 | $0.41 | 82 |
| c_106 | `case_676a01b2` | request_documents | reduce_limit, request_documents | revenue_loss | right | right | 6 | 25 | 1 | 29 | $0.66 | 133 |
| c_107 | `case_50a213d9` | needs_human | reduce_limit | no_clear_cause | wrong | wrong | 6 | 19 | 2 | 23 | $0.63 | 163 |
| c_110 | `case_705e6cf3` | pause_card | pause_card | payroll_or_tax | right | right | 6 | 19 | 1 | 26 | $0.60 | 123 |
| c_111 | `case_1d825b40` | keep_limit | keep_limit | payroll_or_tax | right | right | 5 | 16 | 2 | 27 | $0.63 | 153 |
| c_201 | `case_6f8036ea` | keep_limit | keep_limit | payroll_or_tax | right | right | 2 | 5 | 1 | 9 | $0.19 | 51 |
| c_203 | `case_3f7abe19` | reduce_limit | reduce_limit | revenue_loss | right | right | 6 | 15 | 1 | 23 | $0.47 | 102 |
| c_208 | `case_65b15bff` | request_documents | request_documents | unexplained_outflow | right | right | 2 | 8 | 1 | 10 | $0.20 | 51 |
| c_301 | `case_3fc83826` | keep_limit | keep_limit | payroll_or_tax | right | right | 2 | 9 | 1 | 11 | $0.22 | 51 |
| c_303 | `case_b8836b24` | reduce_limit | reduce_limit | revenue_loss | right | right | 4 | 12 | 1 | 16 | $0.34 | 82 |
| c_304 | `case_2416555c` | request_documents | request_documents | payroll_or_tax | right | right | 3 | 10 | 1 | 16 | $0.33 | 82 |
| c_307 | `case_a33ab91a` | needs_human | needs_human | no_clear_cause | wrong | wrong | 6 | 16 | 2 | 25 | $0.59 | 133 |
| c_314 | `case_a0128860` | needs_human | needs_human | payroll_or_tax | right | wrong | 4 | 7 | 2 | 16 | $0.39 | 112 |

c_307 accepts needs_human, but the grader counts a give-up as wrong on every check (`grade.py`, `gave_up`): it reached a person by failing.

## The tool calls against the business logic

Each case's questions (from its path page) were joined with every Bank and Card tool its specialists called (from LangFuse), and five reviewers judged them case by case as a credit analyst would, against `app/guidelines.py`, `app/graph/needs.py` and `app/graph/causes.py`; a second reviewer checked each problem-level finding.

The investigation itself holds up:

- 124 questions, all answered, none refused. 342 specialist tool calls, each matched to the question it served.
- Every case started from its review reason: transactions first in 26 cases, Card repayments first in c_013, the one case without a large cut.
- In every case the records a guideline needs were in hand before the first gap pass: the code gap rules never had a question to send (50 gap passes, all by the model). The 19 cases that name a record the path must reach all reached it.
- None of the four misses came from the questions. In c_007, c_107 and c_307 the records were there; both drafts named unexplained_outflow under G-09, which needs a payment categorised `vendor_new`, and the supplier payment is `vendor_known` with no earlier payment on record. That is the same label-against-record conflict as on 2026-11-18.

Verdicts: 7 cases sound, 19 with minor issues, 1 problem: c_107, where the give-up left the one cut the golden set requires unmade, although the records for G-06 and G-07 were in hand. c_007 and c_307 gave up the same way, but there the outcome did no harm: c_307 accepts a hand-over, and c_007 accepts keeping the limit, which a hand-over also leaves unchanged. The minor issues cost rounds and money and changed no cause or action:

| Pattern | How often | Where the cheapest fix sits |
|---|---|---|
| The model's gap passes send the case back for questions that are not needed | 23 questions came right after a pass that named gaps; the reviewers judged 16 of them unneeded, 4 fair but settling nothing, and 3 useful. All 9 cases that used the 6-question limit ended on such a question | Prompt (GAPS) |
| A gap pass reads the large-cut reason as asking why the engine chose its number, which no tool holds | 13 gap lines in 6 cases name the engine's limit | One sentence in the GAPS prompt |
| seasonal_dip stays "untested" until prior_year is asked, even when income is falling | prior_year asked 13 times, nothing on record 10 times | One condition in `look_for_gaps` |
| The investigator asks again for a topic whose records it already holds | 11 questions | Investigator prompt |
| Payee history asked for a running cost, which no guideline reads | 7 questions | Investigator and tool descriptions |
| A specialist repeats a call it, or an earlier answer, already made | 62 of 342 calls identical to an earlier one in the case | Cash Flow agent prompt |
| A repayment returned for insufficient funds while the account held the money: no tool explains it, and the gap passes that raise it never reach the report | 4 cases (c_010, c_013, c_110, c_111) | Test data, or a team decision on showing open gaps |
| A transactions list filtered by size read as payments missing from the books | 3 cases | Bank tool result |

Two notes for the team, not changed here:

- c_314's cause is graded wrong for payroll_or_tax, the cause its records show: the tax payment is 79% of the fall, and c_004, its twin with the same bank data and another earlier review, accepts payroll_or_tax. Adding it to c_314's causes would make cause right 24 of 27.
- Bank's last-year comparison sets the window's payments (7 days) against every payment of the last-year weeks the window spans, 14 days when it crosses two calendar weeks (`services/bank/app/ledger.py`, `prior_year`; the record carries both `window_days` and `period_days`). G-03's same-fall test reads that share. Per week, c_007's fall would be about twice last year's, and c_007's accepted keep_limit rests on G-03.

The search ran in all 27 cases and changed no answer. The planted "always keep the limit" note in the seeded reviews came back 8 times and was ignored each time.

## Limits

- One run of each case. Run-to-run stability is unknown, as on 2026-11-18.
- The `eval/run.py` request checks (t_01 to t_05) and its summary did not run; grading used the same grader.
- The reviewers' judgments of the tool calls are a reading against the rules and common practice, not a graded check.
