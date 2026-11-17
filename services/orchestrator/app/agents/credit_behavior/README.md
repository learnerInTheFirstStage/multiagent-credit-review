# Credit Behavior Agent

Owner: James. KAN-26.

`credit_behavior(request: InvestigateRequest) -> InvestigateResponse`, the same signature as `mock.credit_behavior`, so the graph can swap one for the other (KAN-36). It answers one question about one customer's card: how the card is used and how it is repaid. It keeps nothing between calls and never recommends a limit action. All data and business logic stay on the Card Service; this folder reaches them only through Card's analysis tools (`services/card/README.md`), with a 3-minute service token that names the one customer.

## How a call runs

| Step | Who | What |
|---|---|---|
| 1. Preflight | code | calls the tools the topic rests on: `repayments` → last 3 repayments; `paying_account` → the paying account across them; `card_use` → utilization and spend by cycle; no topic → all four. Not counted in the budget |
| 2. Investigate | model | a tool-calling loop over the six Card tools, given the preflight results. At most 5 calls and 60 seconds; a repeated call gets the earlier result for free |
| 3. Check | code | every cited id was returned by a tool; every $ and % in a claim is in the cited records; no limit action is recommended; a `too_short_to_tell` record is never called normal. Failures go back to the model once, then are left out and said so in `could_not_determine` |
| 4. Hard facts | code | a failed or late repayment, or a change of the paying account, that no accepted finding cites gets a finding written by code (`findings.py`) |
| 5. Evidence | code | the records the findings cite, exactly as Card returned them |

Memos are wrapped in `<memo>...</memo>`, brackets escaped, before the model reads them. With no model (`LLM_MODEL` unset and none passed in) steps 2 and 3 are skipped and code answers from the preflight records, saying so in `could_not_determine`. If Card does not answer, the findings are empty and `could_not_determine` says which tool failed.

Settings: `CARD_URL` (compose sets `http://card:8000`), `JWT_SECRET`, and the service's one model (`LLM_MODEL`, read in `app/runtime.py`).

## Files

| File | What |
|---|---|
| `agent.py` | the run: preflight, the bounded loop, one repair round, the response |
| `tools.py` | the Card tools as LangChain tools and plain calls; errors become `{error, message}` |
| `guardrails.py` | memo marking, the evidence pool, the draft checks |
| `findings.py` | findings code writes from records, used for hard facts and when there is no model |
| `prompts.py` | the system prompt and the first and repair messages |

Tests: `services/orchestrator/tests/test_credit_behavior_agent.py`, against Card responses recorded from the seeded stack (`tests/fixtures/card_tools.json`) and a scripted model. To record them again after a Card change, run the stack and call each tool in that file's keys with a token from `docker compose exec card python -m app.dev_token --sub fixture --customers <id>`.

Not done yet: the swap in `app/runtime.py` (KAN-36), and a run on a real model.
