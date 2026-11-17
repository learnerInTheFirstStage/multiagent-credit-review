# Orchestrator Service

Owner: Vincent.

Turns a signal into a reviewed recommendation with evidence and carries the case through approval. All three agents run here: the main agent in the graph, and the two specialists under `app/agents/`, which the graph calls as tools in-process.

| Endpoint | What it does |
|---|---|
| `GET /health` | Fails if the database is unreachable |
| `POST /signals` | Opens a case, or joins the case the customer already has open. A repeated signal is ignored; the same `signal_id` with other content is a 409. A customer the data files do not hold is a 422. A new case is investigated after the answer is sent; a joined signal is stored and starts no new run. Needs a `service` or `admin` token |
| `GET /cases` | The queue: the open cases of the customers the token may see, the longest waiting first. `?status=` lists one status instead, closed ones included. `?limit=` (50, at most 200), `?offset=`. Answers `{"cases": [...], "total": n}` |
| `GET /cases/{id}` | The case page: status, whose move it is, the checked report, the decisions, the action |
| `GET /cases/{id}/path` | How the case was investigated, as ids and labels: the questions with their topic, focus and outcome, the searches, the gap passes, the checked drafts, the earlier reviews it was given, and how it was handed over. Same access rule as the case page. Empty for a case nobody ran |
| `POST /cases/{id}/decision` | `{"decision": "approve" or "reject"}`, with an optional `note` (at most 500 characters) and, only when the analyst step approves a report that hands the case over, `decided`: what the person concluded (`keep_limit`, `reduce_limit`, `pause_card` or `request_documents`). Nothing goes to Card for `decided`; anywhere else it is a 409. Who decides comes from the token |

Every endpoint but `/health` needs a token. Every error has one shape, `{"error": "forbidden", "message": "..."}`, with the error names `unauthorized`, `forbidden`, `not_found`, `conflict`, `invalid_request`, `card_unavailable`, `unavailable` and, for anything unhandled, `internal_server_error`.

## Running cases

The service runs cases only when `LLM_MODEL` is set (`provider:model`). The packages for Anthropic and OpenAI are installed, so `anthropic:claude-sonnet-5-5` or `openai:<model>` works with a key in `LLM_API_KEY` and no code change. Only Anthropic has been run so far. All three agents use this one model. Set `LLM_STRUCTURED_OUTPUT=json_schema` with it: the gap review and the report then come back in the provider's own structured form. With the default, a tool call, a first draft sometimes arrives malformed and costs one of the two drafts. Without it a signal is stored, its case waits as `queued`, and the next start with a model picks it up. A start also picks up a case that a restart stopped part-way, and corrects a status in the table that is older than what the graph saved. Graph state is saved in this service's database by the Postgres checkpointer, so a case waiting for approval survives a restart.

Until the real specialists exist, the running service uses the stand-ins in `app/agents/mock.py`. They answer from `data/shared_customers.yaml`, so all 12 test customers are covered. A stand-in does not read the question. It reads the topic asked and the focus (`record_id`, `account_id`, the dates) and returns its side's facts on that topic; with no topic it returns everything it holds. It never returns the customer's story or the expected answer, and it calls no model.

## Guidelines

`app/guidelines.py` holds the credit guidelines. They start from the 14 in tab 3 of "3. Scenario table", with G-08 and G-10 each split in two, so there are 16. Each says when it applies, which recommendations it allows, and what it needs: the record kinds, fields and shares that must hold. The numbers come from `app/policy.py`, which also has four rules that can be switched off. All 16 go into the investigator's opening brief and the report prompt. A report that recommends an action has to cite a guideline that allows it, and code checks the cited records against that guideline's needs. G-12 and G-13 cannot be cited while `LARGE_LIMIT_CENTS` and `MIN_CASH_CENTS` in `policy.py` are unset.

## Tracing

With `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` set, every run of the graph is sent to LangFuse. Without them cases run the same, untraced.

- A case is one LangFuse session, named by its `case_id`. Each run is a trace in it: the investigation, then one per decision, with the person who decided as the trace's user.
- Every step of the graph and every model call is in the trace.
- Tracing is set up once, in `app/runtime.py`. An agent folder needs no tracing code: a model call made inside a specialist lands in the same trace, as long as it goes through LangChain or LangGraph. A call made with a provider's own SDK does not show up.
- A trace holds the case's data: the signal, the specialists' answers, the report. It never holds the approver's token.
- If LangFuse cannot be reached, cases run the same and the traces are lost. Stopping the service can then take a few seconds longer.
- When a manager closes a case that Card never confirmed, no run of the graph happens, so there is no trace of it. It is in the case's decisions and in the service log.

## Tokens and decisions

A token is a bearer token signed with `JWT_SECRET` (HS256), with the claims `sub`, `role`, `customers`, `exp`. To read or decide a case, the token must list the case's customer.

Until the Auth Service issues tokens, make one for local use:

```bash
docker compose exec orchestrator python -m app.dev_token --role analyst --sub u_analyst --customers c_010
docker compose exec orchestrator python -m app.dev_token --role service --sub signal-generator
```

| Step | Who may decide |
|---|---|
| Analyst approval | analyst or manager |
| Manager approval | a manager who did not approve the analyst step |

An approved reduction or pause goes to `POST {CARD_URL}/customers/{id}/actions` inside the decision request, with the approver's own token and an `approval_id` that stays the same on a retry. The token is never saved. The body is `{"type": "reduce_limit", "limit_cents": <the engine's limit>}` or `{"type": "pause_card"}`, plus `case_id`, `customer_id`, `approved_by` and `approval_id` (`<case_id>:analyst` or `<case_id>:manager`). `keep_limit` and `request_documents` send nothing and close the case as `done`.

Card's endpoint is not built yet, so today every approved reduction or pause gets the 502 below and waits to be retried.

If Card does not confirm the action, the answer is 502 and the approval is kept. Then two things can happen:

- The person who approved sends the approval again, and the action is retried. Nobody else can retry it. A retry records no new decision, so the note kept is the one the approval first carried.
- A manager sends a rejection, and the case closes as `failed` with nothing more sent. It is `failed` and not `rejected` because Card may have applied the action and lost its answer, so a person has to check. A case is also `failed` when its run raised; such a case is not picked up again and leaves no review.

Tables are created at startup. There are no migrations: all data is generated, so after a schema change run `docker compose down -v` and start again. An old volume needs no reset for the `reviews` table: a start adds a missing table.

## History

When a case closes (approved, rejected, or closed as failed after Card never confirmed), a review is written in the same transaction as the status: the labels of the checked report, the records it cites, the decisions with their notes, what was asked. A case that broke before its report was checked leaves none. A report the service gave up on is kept, but is not a precedent.

- When a case opens, the customer's reviews dated strictly before the signal's day become citable records: `hist_{customer}_summary` counts every earlier review whose report passed the check, and the three newest of them get a record of their own. When an approved review from the same season of an earlier year exists, the case also opens with `hist_{customer}_recovery`. A missing `reviews` table fails the case: a broken lookup must not read as a first review.
- Every start with a model configured writes the review of any closed case that has none, from the state the graph saved, and logs how many it wrote.
- Every start also seeds the reviews in `HISTORY_SEED_FILE`: one path or several separated by `:`, by default `/eval/history.yaml`, the copy of `eval/history.yaml` in the image. It needs no model, inserts only what is not there yet, and a missing or broken file is logged and does not stop the start.
- The main stack seeds one review, `seed_c_007_2025`, the past review c_007's story names. Search over past cases therefore has one precedent to find there. The evaluation names its own second file with the other seeds.
- A seeded case is closed, so it is not in the queue. `GET /cases?status=done` lists it to a token with its customer, and its case page has no report: a seed has no graph state.

To seed by hand, or to seed several files: `docker compose exec orchestrator python -m app.seed_history /eval/history.yaml`.

Database: `orchestrator`. Local port: 8004. Behind the gateway: `/api/orchestrator/...`.
Inside the compose network the Bank and Card services are at `http://bank:8000` and `http://card:8000`.

## The graph

`app/graph` is one LangGraph graph, one thread per case. Code fixes the order of the steps.

| Step | Decided by | What it does |
|---|---|---|
| 1. Open the case | code | Turns the signal into `sig_` records and earlier reviews into `hist_` records, loads the guidelines, sets the 7-day window |
| 2. Investigate | model | Asks one specialist one question on one topic, or says it has enough. At most 6 questions. Its first 3 refused calls use no question. It may also search past cases twice (`SearchPastCases`) |
| Ask a specialist | code | Checks the call (topic, dates, focus, no figure the records lack), runs the specialist, checks the whole answer against the contract, stores its records |
| 3. Look for gaps | code, then model | Code's gap rules run first; if they find gaps, the model is not asked. Otherwise the model says what is still unexplained, and a gap that states a figure is dropped. May send the case back to step 2 twice |
| 4. Fetch the guidelines | code | All 16 |
| 5. Draft the report | model | Structured output |
| 6. Check the report | code | Cited evidence was returned; each review reason is addressed; cited guidelines exist, allow the recommendation, and have their needs met by the cited records; a hand-over cites a hand-over guideline; a reduction lowers the limit; a cited record shows the likely cause; every figure appears in the records it cites. One redraft, then code writes a `needs_human` report itself. A low-confidence report becomes `needs_human` |
| 7. Analyst approval | person | The graph pauses. An answer that is not a decision pauses it again |
| 8. Manager approval | person | Every card pause, and a cut over half the limit |
| 9. Act on the decision | code | Sends an approved reduction or pause to Card |

`INVESTIGATION_FLOW` picks who asks the questions in step 2: `agent` (the model, the default), `fixed` (a written 8-question plan, no model before the report) or `fixed_rules` (the plan, then up to 4 follow-ups from code's gap list). The evaluation compares the three.

`build_graph` takes the model, the two specialists, the guideline loader, the function that calls Card and a checkpointer, and optionally the structured-output method, the earlier-reviews lookup, the past-case search, a plan and `follow_gaps`. The tests pass a scripted model and the stand-in specialists. Step 9 is separate from the approvals because a resumed step runs again from its first line.

A case is started once on its thread and resumed with a decision `{"decision": "approve" or "reject", "by": who}`, plus `note` and `decided` when given. If the call to Card fails, the case stays approved and open, and running the thread again sends the action again.

## Tests

```bash
cd services/orchestrator
pip install -r requirements-dev.txt ../../packages/contracts
pytest
```

They run on a throwaway SQLite file with foreign keys enforced, so no stack is needed. To run them on Postgres, which adds the tests that only mean something there (the checkpointer, and two requests at the same moment):

```bash
TEST_DATABASE_URL=postgresql+psycopg://user:password@127.0.0.1:5432/dbname pytest
```

## Agents

| Folder | Agent | Owner |
|---|---|---|
| `app/agents/cash_flow` | Cash Flow Agent | Allen |
| `app/agents/card_service` | Credit Behavior Agent (Card Service agent) | James |

- Each folder exposes one entry function, `investigate(request) -> response`, with the shapes from `packages/contracts`. The graph calls only that function.
- An agent's tools call the read-only analysis endpoints on its service over HTTP. Nothing in an agent folder opens a database connection.
- A specialist holds no state and reports facts with evidence. It never recommends a limit action.
- A problem the tools found, such as an account that stopped syncing or records that disagree, is a finding with evidence. When the tools could not answer at all, `findings` is empty and `could_not_determine` says why.
- The Orchestrator fixes the topics in its tool schemas. Cash Flow: `transactions`, `income`, `payee_history`, `accounts`, `reconciliation`, `prior_year`. Credit Behavior: `repayments`, `paying_account`, `card_use`. `payee_history` comes with the `record_id` of a bank transaction an earlier answer returned; `account_id` is always one the case has seen; a question's dates reach at most 400 days before the signal.
- Evidence ids start with a prefix that one service owns: Bank uses `txn_`, `acct_` and `bal_`, Card uses `rep_` and `bal_card_` (the Card side has still to confirm). An aggregate is `agg_` plus a short id worked out from the tool's inputs. The same fact keeps the same id, with the same content, in every call, because the report check compares ids as plain strings.
- `kind` and the keys in `data` are not free. The report check, the guideline needs, the gap rules and the cross-side match read exact kinds and keys (`app/graph/causes.py`, `app/guidelines.py`, `app/graph/needs.py`, `app/graph/facts.py`): for example `bank_transaction` with `category`, `amount_cents`, `share_of_fall_bp` and `to_account`, `funding_account_change` with `changed`, `from` and `to` (`to` is matched against the Bank side's `to_account`), `weekly_inflow_series` with `income_trend`, `linked_account` with `is_stale`, `balance_reconciliation` with `reconciled`. A specialist that names them differently still passes validation, but its records never meet a guideline, and its cases end handed over. Until the names are written into `packages/contracts`, the stand-ins in `app/agents/mock.py` are the reference.
- An answer is refused whole when the wrong agent answers, when an id starts with `sig_`, `hist_` or `xs_` or a kind is one of the Orchestrator's own, or when a known id comes back with other content. A refused answer counts as no answer and the case goes on.
- Model and LangFuse settings are read in one place in this service, not in each agent folder.
- Until the real agents can be called, `app/agents/mock.py` stands in for both. `mock.cash_flow` and `mock.credit_behavior` take the same request and return the same response as the real `investigate` functions, built from `data/shared_customers.yaml`. `POST /signals` also refuses a customer these files do not hold (KAN-36: ask Bank and Card instead).
