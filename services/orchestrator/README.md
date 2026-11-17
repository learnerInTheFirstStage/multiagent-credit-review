# Orchestrator Service

Owner: Vincent.

Turns a signal into a reviewed recommendation with evidence and carries the case through approval. All three agents run here: the main agent in the graph, and the two specialists under `app/agents/`, which the graph calls as tools in-process.

| Endpoint | What it does |
|---|---|
| `GET /health` | Fails if the database is unreachable |
| `POST /signals` | Opens a case, or joins the case the customer already has open. A repeated signal is ignored. A new case is investigated after the answer is sent. No token check yet |
| `GET /cases/{id}` | The case page: status, whose move it is, the checked report, the decisions, the action |
| `POST /cases/{id}/decision` | `{"decision": "approve" or "reject"}`. Who decides comes from the token |

`GET /cases` (the queue) is not built yet.

## Running cases

The service runs cases only when `LLM_MODEL` is set (`provider:model`, with that provider's package installed). Without it a signal is stored, its case waits as `queued`, and the next start with a model picks it up. A start also picks up a case that a restart stopped part-way, and corrects a status in the table that is older than what the graph saved. Graph state is saved in this service's database by the Postgres checkpointer, so a case waiting for approval survives a restart.

Until the real pieces exist, the running service uses the stand-in specialists and no guidelines (KAN-33), so a report can only recommend `needs_human`.

## Tokens and decisions

`GET /cases/{id}` and the decision endpoint need a bearer token signed with `JWT_SECRET` (HS256), with the claims `sub`, `role`, `customers`, `exp`. The token must list the case's customer.

| Step | Who may decide |
|---|---|
| Analyst approval | analyst or manager |
| Manager approval | a manager who did not approve the analyst step |

An approved reduction or pause goes to `POST {CARD_URL}/customers/{id}/actions` inside the decision request, with the approver's own token and an `approval_id` that stays the same on a retry. The token is never saved.

If Card does not confirm the action, the answer is 502 and the approval is kept. Then two things can happen:

- The person who approved sends the approval again, and the action is retried. Nobody else can retry it.
- A manager sends a rejection, and the case closes as `failed` with nothing more sent. It is `failed` and not `rejected` because Card may have applied the action and lost its answer, so a person has to check.

Tables are created at startup. There are no migrations: all data is generated, so after a schema change run `docker compose down -v` and start again.

Database: `orchestrator`. Local port: 8004. Behind the gateway: `/api/orchestrator/...`.
Inside the compose network the Bank and Card services are at `http://bank:8000` and `http://card:8000`.

## The graph

`app/graph` is one LangGraph graph, one thread per case. Code fixes the order of the steps.

| Step | Decided by | What it does |
|---|---|---|
| 1. Open the case | code | Loads the signal, sets the 7-day window |
| 2. Investigate | model | Asks one specialist one question, or says it has enough. At most 6 questions |
| Ask a specialist | code | Runs the specialist, checks its answer against the contract, stores it |
| 3. Look for gaps | model | Says what is still unexplained. May send the case back to step 2 twice |
| 4. Fetch the guidelines | code | All of them for now |
| 5. Draft the report | model | Structured output |
| 6. Check the report | code | Cited evidence was returned, each review reason is addressed, a cited guideline allows the recommendation, a reduction lowers the limit. One redraft, then `needs_human` |
| 7. Analyst approval | person | The graph pauses. An answer that is not a decision pauses it again |
| 8. Manager approval | person | Only for a card pause or a cut over half the limit |
| 9. Act on the decision | code | Sends an approved reduction or pause to Card |

`build_graph` takes the model, the two specialists, the guideline loader, the function that calls Card, and a checkpointer. The tests pass a scripted model and the stand-in specialists. Step 9 is separate from the approvals because a resumed step runs again from its first line.

A case is started once on its thread and resumed with a decision `{"decision": "approve" or "reject", "by": who}`. If the call to Card fails, the case stays approved and open, and running the thread again sends the action again.

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
| `app/agents/credit_behavior` | Credit Behavior Agent | James |

- Each folder exposes one entry function, `investigate(request) -> response`, with the shapes from `packages/contracts`. The graph calls only that function.
- An agent's tools call the read-only analysis endpoints on its service over HTTP. Nothing in an agent folder opens a database connection.
- A specialist holds no state and reports facts with evidence. It never recommends a limit action.
- A problem the tools found, such as an account that stopped syncing or records that disagree, is a finding with evidence. When the tools could not answer at all, `findings` is empty and `could_not_determine` says why.
- Evidence ids start with a prefix that one service owns: Bank uses `txn_`, `acct_` and `bal_`, Card uses `rep_` and `bal_card_` (the Card side has still to confirm). An aggregate is `agg_` plus a short id worked out from the tool's inputs. The same fact keeps the same id in every call, because the report check compares ids as plain strings. `kind` is a free string for now.
- Model and LangFuse settings are read in one place in this service, not in each agent folder.
- Until the real agents can be called, `app/agents/mock.py` stands in for both. `mock.cash_flow` and `mock.credit_behavior` take the same request and return the same response as the real `investigate` functions, with canned answers for c_002 and c_010.
