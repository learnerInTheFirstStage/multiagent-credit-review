# Orchestrator Service

Owner: Vincent.

Turns a signal into a reviewed recommendation with evidence and carries the case through approval. All three agents run here: the main agent in the graph, and the two specialists under `app/agents/`, which the graph calls as tools in-process.

| Endpoint | What it does |
|---|---|
| `GET /health` | Fails if the database is unreachable |
| `POST /signals` | Opens a case, or joins the case the customer already has open. A repeated signal is ignored; the same `signal_id` with other content is a 409. A customer the data files do not hold is a 422. A new case is investigated after the answer is sent; a joined signal is stored and starts no new run. Needs a `service` or `admin` token |
| `GET /signals/examples` | The test customers a signal can be sent for: `[{"customer_id", "name"}]`, the customers the data files hold, so `POST /signals` accepts each one. Same tokens as `POST /signals` |
| `GET /signals/examples/{customer_id}` | That customer's signal, built by `eval/signals.py` (the image copies it to `/eval`), with a new `signal_id` on every call, so sending it again joins the open case instead of being a duplicate. A customer the data files do not hold is a 404. Same tokens as `POST /signals`. The review app's "Send a test signal" page uses both |
| `GET /cases` | The queue: the open cases of the customers the token may see, the longest waiting first. `?status=` lists one status instead, closed ones included. `?limit=` (50, at most 200), `?offset=`. Answers `{"cases": [...], "total": n}`. Each row carries `outage` (see below), `imported` (a review imported from earlier records, with no signal), `documents_due` (while the case waits for the customer's documents), `signal_reasons` (every review reason the case's signals named), `waiting_for` as on the case page, the checked report's `recommendation` and `confidence` (`null` before there is one), and `cut_share_bp`: the engine's cut as basis points of the current limit, from the signal the case runs on, rounded as its `sig_limit` record, `null` when it cuts nothing. The row is read from the `cases` table, never the graph state: `runner.keep_row` copies `waiting_for`, `recommendation` and `confidence` there each time it writes the status the graph saved |
| `GET /cases/{id}` | The case page: status, whose move it is, the checked report, `guidelines` (the ones the report cites, in its order, as the case ran on them: `id`, `title`, `when`, `allows`, `note` or `null`), `approval_outcomes` (see below), the decisions (each with `step`, `decision`, `by`, `by_role` and `decided_at` from the token and the service's clock, and `note`, `reason` and `decided` when given), `documents_request` (see "Requesting documents") or `null`, the action, and `outage`: why the case waits for Bank or Card, or failed for want of them, or `null`; `card_refusal`: Card's words when it refused the approved action at the last attempt (sending it again will not help), `null` once Card applies it or when it only failed to answer; `past_review`: for a review imported from earlier records (a seed), what it recorded (the signal's figures, cause, recommendation, confidence, guidelines, claims and decisions with their notes), `null` for every case that ran here |
| `GET /cases/{id}/path` | How the case was investigated, as ids and labels: the questions with their topic, focus and outcome and what the specialist answered (its findings with their confidence and records, and what it could not determine), the searches, the gap passes, the checked drafts, the earlier reviews it was given, how it was handed over, and `outage` as on the case page. Same access rule as the case page. Empty for a case nobody ran |
| `POST /cases/{id}/decision` | `{"decision": "approve" or "reject"}`, with an optional `note` (at most 500 characters), `reason` and `decided`. `reason` is one label at any step: `agree`, `wrong_cause`, `wrong_action`, `missing_evidence`, `data_problem` or `other`, else a 422; it is kept on the decision and with the case's review. When the analyst step approves a report that hands the case over, `decided` is optional: what the person concluded (`keep_limit`, `reduce_limit`, `pause_card` or `request_documents`). Approving the documents step needs it: what the customer's documents showed (`keep_limit`, `reduce_limit` or `pause_card`), else a 409. A decided `reduce_limit` or `pause_card` is carried out as a recommended one would be (the manager step for a pause or a cut over half the limit an analyst approved, then Card); `request_documents` makes the case wait for documents; `keep_limit` or none closes it as done. A decided `reduce_limit` is a 409 unless the engine's limit is above 0 and below the current limit (Card would refuse it), and so is `decided` anywhere else. An optional `step` (`analyst`, `manager`, `documents` or `retry`) names the step the person's page showed; when the case has moved on since, the decision is a 409 and nothing is recorded. Who decides, in which role and when come from the token and the service's clock |
| `POST /cases/{id}/retry` | Runs a case that is `waiting_for_data`, or `failed` because Bank or Card did not answer, again now, from the question that met the outage. Answers 202 with the case page (`investigating`); the run happens after the answer. An analyst or a manager whose token lists the customer. Any other case is a 409, and so is a failed case whose customer has another case open. A 503 while no model is configured |
| `POST /dev_login` | `{"sub", "role"}` and an optional `customers` list: an HS256 token for an analyst, a manager or a service, with no password. It answers only when `DEV_LOGIN` is `1`, `true` or `yes` (a 404 otherwise): the review app's sign-in on a local stack without Auth0. With Auth0 on, the person tokens it makes are refused |

Every endpoint but `/health` and `POST /dev_login` needs a token. Every error has one shape, `{"error": "forbidden", "message": "..."}`, with the error names `unauthorized`, `forbidden`, `not_found`, `conflict`, `invalid_request`, `card_unavailable`, `unavailable` and, for anything unhandled, `internal_server_error`.

## Running cases

The service runs cases only when `LLM_MODEL` is set (`provider:model`). The packages for Anthropic and OpenAI are installed, so `anthropic:claude-sonnet-5-5` or `openai:<model>` works with a key in `LLM_API_KEY` and no code change. Only Anthropic has been run so far. All three agents use this one model. Set `LLM_STRUCTURED_OUTPUT=json_schema` with it: the gap review and the report then come back in the provider's own structured form. With the default, a tool call, a first draft sometimes arrives malformed and costs one of the two drafts. Without it a signal is stored, its case waits as `queued`, and the next start with a model picks it up. A start also picks up a case that a restart stopped part-way, and corrects a status in the table that is older than what the graph saved. Graph state is saved in this service's database by the Postgres checkpointer, so a case waiting for approval survives a restart.

The running service asks the real specialists (`app/agents/cash_flow`, `app/agents/card_service`), which call Bank and Card (`app/runtime.py` `specialists`). With `SPECIALISTS=stand_in` it uses the stand-ins in `app/agents/mock.py` instead. They answer from `data/shared_customers.yaml` (and, in the evaluation, `eval/extra_customers.yaml`), the same files Bank and Card seed from, and they emit the same ids, kinds and keys (Bank's parity test checks this). A record holds what the service's own record holds where the file gives the value or the service's seeder sets it by a fixed rule: the day of a payment, the weekly inflows week by week, a repayment's due date, statement, amount and how it was paid, the day of the card balance. Why a repayment failed is not in a stand-in's record. A stand-in does not read the question. It reads the topic asked and the focus (`record_id`, `account_id`, the dates) and returns its side's facts on that topic; with no topic it returns everything it holds. It never returns the customer's story or the expected answer, and it calls no model.

A case's status is one of `queued`, `investigating`, `waiting_for_data`, `needs_approval`, `needs_manager` and `awaiting_documents`, which are open (a customer has one open case at a time, and a new signal joins it), or `done`, `rejected` and `failed`, which are closed.

## When Bank or Card gives nothing

A specialist whose every call to its service failed as an outage answers `errors.Unanswered`. That is neither an answer nor "no answer": the graph raises `errors.CaseWaiting` before recording the question, so its checkpoint stays before that question. A later run asks only that question again and keeps everything asked before it. Nothing about the outage reaches the report, and a case that met one never ends as a G-11 hand-over on "no guideline fits".

`app/agents/errors.py` sorts the outage by its codes. When several codes were met, the one retrying helps least decides (data, then configuration, then transient).

| Kind | Codes | What happens | Logged |
|---|---|---|---|
| transient | `unreachable`, `timeout`, `http_5xx` | The case waits and is retried | WARNING |
| configuration | `http_401`, `http_403`, `http_404` (a route the service does not have), or a code nobody classified | The case waits and is retried; someone has to fix the setup (a token secret, `BANK_URL` or `CARD_URL`) | ERROR |
| data | `unknown_customer` | Retrying cannot help: the case fails at once | ERROR |

A waiting case is `waiting_for_data`. The wait is kept in the `outage_waits` table: the service, the codes, the kind, how many runs met the outage (`attempts`), when it was first met, and when the case runs next. A loop in the service runs every case whose retry is due, at start (so a case that came due while the service was down runs at once) and then every `OUTAGE_CHECK_SECONDS` (15 by default, and 15 with a warning for a value that is not a positive number of seconds). A start does not otherwise pick up a waiting case.

The retry delays are `OUTAGE_RETRY_SECONDS = (60, 300, 900)` in `app/policy.py`: attempt 1 waits 60 seconds, attempt 2 five minutes, attempt 3 fifteen minutes. Attempt 4, about 21 minutes after the first, fails the case. A failed case has no report and leaves no review; its `outage` says why, for example "Bank Service unavailable (http_503) since 2026-11-18T10:00Z, retried 3 times". A run that ends without an outage removes the wait, and so does a run that fails for another reason. A run that gets past the question and meets an outage at a later one meets a new outage: its attempts and its "since" start again.

A person then has two moves. `POST /cases/{id}/retry` runs the case again now; it counts as one more attempt at that question, so a failed case that meets the outage there again fails again at once, and a waiting one waits for the delay of its new attempt, or fails if that was its last. Or the person leaves it: a failed case is closed already, so the customer's next signal opens a new case. A waiting case cannot be closed by hand (`/decision` is a 409 while nothing waits for a decision); it closes as `failed` when its retries are used up.

`waiting_for_data` and `awaiting_documents` are in the partial unique index that keeps one open case per customer. `create_all` does not change an index that is already there, so on a database made before these statuses existed only the service's own check guards it: run `docker compose down -v` to get the new index. Nor does it add a column: the `cases` table's `waiting_for`, `recommendation` and `confidence` are missing from a database made before them, so every request that reads a case fails there (the queue, the case page, decisions, retries, `POST /signals`, the event stream), and the seeded reviews are not loaded at start, until `docker compose down -v`.

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

A token is a bearer token signed with `JWT_SECRET` (HS256), with the claims `sub`, `role`, `customers`, `exp`. To read or decide a case, the token must list the case's customer. With `AUTH0_DOMAIN` and `AUTH0_AUDIENCE` set, a person's token comes from Auth0 instead and `JWT_SECRET` signs services' tokens only (`services/auth/README.md`).

Without Auth0, make a token for local use:

```bash
docker compose exec orchestrator python -m app.dev_token --role analyst --sub u_analyst --customers c_010
docker compose exec orchestrator python -m app.dev_token --role service --sub signal-generator
```

| Step | Who may decide |
|---|---|
| Analyst approval | analyst or manager |
| Documents | analyst or manager |
| Manager approval | a manager who did not give the approval before it |

A pause, or a cut over half the limit, needs a manager's authority, as delegated credit authority works: whoever approves within their own authority signs alone, and only an action above an analyst's goes up to a manager. So the manager step follows an analyst's approval of one; a manager who approves it at the analyst or documents step has given the manager's approval, and it goes to Card at once.

An approved reduction or pause goes to `POST {CARD_URL}/customers/{id}/actions` inside the decision request, with the approver's own token and an `approval_id` that stays the same on a retry. The token is never saved. The body is `{"type": "reduce_limit", "limit_cents": <the engine's limit>}` or `{"type": "pause_card"}`, plus `case_id`, `customer_id`, `approved_by` and `approval_id`: `<case_id>:manager` for the manager step, or for a manager who approved an action that needs a manager at the analyst or documents step; `<case_id>:analyst` otherwise, the documents step included (the steps Card knows, by authority). `keep_limit` sends nothing and closes the case as `done`.

The case page's `approval_outcomes` says what approving does at the step the case waits at, for the reader's role (an analyst's approval of a pause goes to a manager, a manager's to Card), per choice the person can make there: the recommendation at the analyst step, or on a hand-over `keep_limit`, `reduce_limit`, `pause_card` and `request_documents`; the choice being confirmed at the manager step; `keep_limit`, `reduce_limit` and `pause_card` at the documents step. Each has `next` (`manager`, `card`, `awaiting_documents` or `done`), `action`, the body above without the ids, or `null`, and `manager_because`: why the choice needs a manager's authority (`pause`, `cut_over_half`), or `[]`. The analyst and documents steps route by the same function (`app/graph/build.py`), and `act` sends the same action, so the page and the graph agree. It is `null` when the case waits for nobody or for a Card retry. A hand-over and the documents step leave `reduce_limit` out when the engine's limit is not a lower limit Card can set, the 409 above.

### Requesting documents

Approving `request_documents` (recommended, or decided on a hand-over) does not close the case. Code drafts `documents_request` from the signal alone, with no model: `asked_for` (when a feed stopped, only that account: reconnect it or send its statements from its last sync; else statements for each account for the review week; invoices or contracts for the week's payments on a `large_cut`; proof of each failed repayment), `message` (the review reasons in words, the list, the due date, that the limit stays as it is, and that the review is finished with what we have if nothing comes by then), both naming each account as the customer knows it, "Chase Business Checking ••4821" from the signal's `name` and `mask` (else "the linked bank account that last updated on Nov 5, 2026"), never by our id, and writing each day as "Dec 5, 2026"; `due` (the approval's day plus `DOCUMENTS_DUE_DAYS`, 14, in `app/policy.py`), `requested_at` and `requested_by` (the approval's). Nothing goes to Card or to the customer: the person sends it. The case is `awaiting_documents`, waiting for `documents`, and a new signal for the customer joins it. Nothing happens on its own when the due date passes.

When the documents come in, an analyst or a manager approves with `decided` (`keep_limit`, `reduce_limit` or `pause_card`), carried out as above, or rejects: the case closes as `rejected` with nothing sent. The customer was asked either way, so the review counts as a request for documents (G-14) however the case closed. The monthly report counts it as "decided on the documents", unless it was a hand-over or someone rejected.

Card checks the approver's role again and applies each `approval_id` once (`services/card`, KAN-28).

If Card does not confirm the action, the answer is 502 and the approval is kept. When Card refused it (400, 403, 404, 409 or 422, for example a limit that is not below Card's current one), the message gives Card's reason and does not suggest a retry, since the same approval gets the same answer. Then two things can happen:

- The person who approved sends the approval again, and the action is retried. Nobody else can retry it. A retry records no new decision, so the note and reason kept are the ones the approval first carried.
- A manager sends a rejection, and the case closes as `failed` with nothing more sent. It is `failed` and not `rejected` because Card may have applied the action and lost its answer, so a person has to check. A case is also `failed` when its run raised; such a case is not picked up again and leaves no review. A case that failed because Bank or Card did not answer is the one failed case that can be run again (`POST /cases/{id}/retry`).

Tables are created at startup. There are no migrations: all data is generated, so after a schema change run `docker compose down -v` and start again. `cases` gained `waiting_for`, `recommendation`, `confidence`, `card_refusal` and `documents_due`: on a volume made before them, every read of a case fails until it is reset. An old volume needs no reset for the `reviews` and `outage_waits` tables: a start adds a missing table.

## History

When a case closes (approved, rejected, or closed as failed after Card never confirmed), a review is written in the same transaction as the status: the labels of the checked report, the records it cites, the decisions with their notes and reasons, what was asked. A case that broke before its report was checked leaves none. A report the service gave up on is kept, but is not a precedent.

- When a case opens, the customer's reviews dated strictly before the signal's day become citable records: `hist_{customer}_summary` counts every earlier review whose report passed the check, and the three newest of them get a record of their own. When an approved review from the same season of an earlier year exists, the case also opens with `hist_{customer}_recovery`. A missing `reviews` table fails the case: a broken lookup must not read as a first review.
- Every start with a model configured writes the review of any closed case that has none, from the state the graph saved, and logs how many it wrote.
- Every start also seeds the reviews in `HISTORY_SEED_FILE`: one path or several separated by `:`, by default `/eval/history.yaml`, the copy of `eval/history.yaml` in the image. It needs no model, inserts only what is not there yet, and a missing or broken file is logged and does not stop the start.
- The main stack seeds one review, `seed_c_007_2025`, the past review c_007's story names. Search over past cases therefore has one precedent to find there. The evaluation names its own second file with the other seeds.
- A seeded case is closed, so it is not in the queue. `GET /cases?status=done` lists it to a token with its customer, and its case page has no report: a seed has no graph state.

To seed by hand, or to seed several files: `docker compose exec orchestrator python -m app.seed_history /eval/history.yaml`.

## Monthly report

A small archival PDF of a month's cases, kept outside the service's database, which the POC drops on a schema change. A limit cut or a card pause is an adverse action on an existing account, and its records are kept for 12 months for business credit (Reg B, 12 CFR 1002.12(b)(2); under (b)(5), 60 days for a business with over $1M in revenue unless it asks). Credit decisions are also expected to be reviewed independently and on an ongoing basis (Interagency Guidance on Credit Risk Review Systems, 2020): the report shows month by month how often a person approved, rejected or decided by hand.

- **Format:** PDF/A-3U (ISO 19005-3), the archival form of PDF.
  - Its font, Liberation Sans (`app/fonts`, SIL Open Font License), is embedded, so the file reads the same years from now with no app or font installed.
  - Its figures and rows are attached inside it as `report.json`, so one stored object holds the page for people and the data for machines.
  - fpdf2 refuses to write anything the standard forbids. To check a file with veraPDF, the PDF Association's validator: `docker run --rm -v "$PWD":/data:ro verapdf/cli --format text /data/<file>.pdf`.

- **Contents** (`app/reports.py`):
  - cases opened, closed, still open, and failed before a report;
  - outcomes and failed report checks;
  - limit cuts (with the amount removed) and card pauses;
  - who decided;
  - one row per closed case.
- **What it leaves out:**
  - names, notes and evidence: it shows ids, labels and amounts only;
  - seeded reviews.
- **Where the data comes from:** the `cases` and `reviews` tables only. No agent or model is called.
- **Months:** calendar months in UTC, by when a case closed.
- **On demand:** `POST /reports/monthly/2026-11` with a manager's or an admin's token. The answer gives where the report is kept, its sha256, its figures and, for a bucket, a download link that works for 15 minutes. A month that has not ended is reported to date and says so. A month that has not begun is a 422.
- **On its own:** at every start and then every `REPORT_CHECK_SECONDS` (default an hour), the service makes last month's report if none is kept yet. A service that was down on the 1st catches up when it starts.
- **Where it is kept:**
  - in `REPORTS_BUCKET` when set: an S3 bucket, or a Google Cloud Storage one written `gs://<bucket>`;
  - else in `REPORTS_DIR`, which compose sets to the `reports` volume;
  - with neither set, no report is made and the endpoint answers 503.
- **Copies:** every run keeps a new copy, named `monthly/<month>/credit-review-<month>-<UTC time>-<manual|scheduled>.pdf`. None is overwritten. In a bucket the write is conditional, and the object carries its sha256 and its format in its metadata.
- **S3 credentials:** boto3's own chain, that is the EC2 instance role or `AWS_PROFILE`, never keys in `.env`. Give the bucket Object Lock so no copy can be deleted early.
- **Cloud Storage credentials:** Google's own chain, that is the VM's service account, never a key file. The account needs to create and read objects, and Token Creator on itself to sign the download link. Give the bucket a retention policy so no copy can be deleted early (`k8s/gcp/create.sh` does all three).

To read a locally kept report: `docker compose cp orchestrator:/reports ./reports`.

Database: `orchestrator`. Local port: 8004. Behind the gateway: `/api/orchestrator/...`.
Inside the compose network the Bank and Card services are at `http://bank:8000` and `http://card:8000`.

## The graph

`app/graph` is one LangGraph graph, one thread per case. Code fixes the order of the steps.

| Step | Decided by | What it does |
|---|---|---|
| 1. Open the case | code | Turns the signal into `sig_` records and earlier reviews into `hist_` records, loads the guidelines, sets the 7-day window |
| 2. Investigate | model | Asks one specialist one question on one topic, or says it has enough. At most 6 questions. Its first 3 refused calls use no question. It may also search past cases twice (`SearchPastCases`) |
| Ask a specialist | code | Checks the call (topic, dates, focus, no figure the records lack), runs the specialist, checks the whole answer against the contract, stores its records. When the specialist's service gave nothing at all, stops the case before the question (see "When Bank or Card gives nothing") |
| 3. Look for gaps | code, then model | Code's gap rules run first; if they find gaps, the model is not asked. Otherwise the model says what is still unexplained, and a gap that states a figure is dropped. May send the case back to step 2 twice |
| 4. Fetch the guidelines | code | All 16 |
| 5. Draft the report | model | Structured output |
| 6. Check the report | code | Cited evidence was returned; each review reason is addressed; cited guidelines exist, allow the recommendation, and have their needs met by the cited records; a hand-over cites a hand-over guideline; a reduction lowers the limit; a cited record shows the likely cause; every figure appears in the records it cites. One redraft, then code writes a `needs_human` report itself. A low-confidence report becomes `needs_human` |
| 7. Analyst approval | person | The graph pauses. An answer that is not a decision pauses it again |
| Request documents | code | After an approved `request_documents`: drafts `documents_request` from the signal (see "Requesting documents") |
| Documents | person | The graph pauses until a person records what the documents showed. An approval without it pauses it again |
| 8. Manager approval | person | Every card pause, and a cut over half the limit, recommended, decided on a hand-over or shown by the documents, that an analyst approved (a manager's own approval is the manager's) |
| 9. Act on the decision | code | Sends an approved reduction (to the engine's limit) or pause to Card: the recommendation, or the last thing a person decided |

`INVESTIGATION_FLOW` picks who asks the questions in step 2: `agent` (the model, the default), `fixed` (a written 8-question plan, no model before the report) or `fixed_rules` (the plan, then up to 4 follow-ups from code's gap list). The evaluation compares the three.

`build_graph` takes the model, the two specialists, the guideline loader, the function that calls Card and a checkpointer, and optionally the structured-output method, the earlier-reviews lookup, the past-case search, a plan and `follow_gaps`. The tests pass a scripted model and the stand-in specialists. Step 9 is separate from the approvals because a resumed step runs again from its first line.

A case is started once on its thread and resumed with a decision `{"decision": "approve" or "reject", "by": who}`, plus `note`, `reason` and `decided` when given. If the call to Card fails, the case stays approved and open, and running the thread again sends the action again.

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

- Each folder exposes one entry function that takes an investigate request and returns an investigate response (`cash_flow(request)`, `card_service(request)`), with the shapes from `packages/contracts`. The graph calls only that function.
- An agent's tools call the read-only analysis endpoints on its service over HTTP. Nothing in an agent folder opens a database connection.
- A specialist holds no state and reports facts with evidence. It never recommends a limit action.
- A problem the tools found, such as an account that stopped syncing or records that disagree, is a finding with evidence. When the tools could not answer at all, `findings` is empty and `could_not_determine` says why. When every call failed as an outage, the answer is an `errors.Unanswered` and the case waits (see "When Bank or Card gives nothing").
- The Orchestrator fixes the topics in its tool schemas. Cash Flow: `transactions`, `income`, `payee_history`, `accounts`, `reconciliation`, `prior_year`. Credit Behavior: `repayments`, `paying_account`, `card_use`. `payee_history` comes with the `record_id` of a bank transaction an earlier answer returned; `account_id` is always one the case has seen; a question's dates reach at most 400 days before the signal.
- Evidence ids start with a prefix that one service owns: Bank uses `txn_`, `acct_` and `bal_`, Card uses `rep_`, `bal_card_` and `ctx_` (a card transaction). An aggregate is `agg_` plus a short id worked out from the tool's inputs. The same fact keeps the same id, with the same content, in every call, because the report check compares ids as plain strings.
- `kind` and the keys in `data` are not free. The report check, the guideline needs, the gap rules and the cross-side match read exact kinds and keys (`app/graph/causes.py`, `app/guidelines.py`, `app/graph/needs.py`, `app/graph/facts.py`): for example `bank_transaction` with `category`, `amount_cents`, `share_of_fall_bp` and `to_account`, `funding_account_change` with `changed`, `from` and `to` (`to` is matched against the Bank side's `to_account`), `weekly_inflow_series` with `income_trend`, `linked_account` with `is_stale`, `balance_reconciliation` with `reconciled`. A specialist that names them differently still passes validation, but its records never meet a guideline, and its cases end handed over. Until the names are written into `packages/contracts`, the stand-ins in `app/agents/mock.py` are the reference.
- An answer is refused whole when the wrong agent answers, when an id starts with `sig_`, `hist_` or `xs_` or a kind is one of the Orchestrator's own, or when a known id comes back with other content. A refused answer counts as no answer and the case goes on.
- Model and LangFuse settings are read in one place in this service, not in each agent folder.
- The service calls the real agents. With `SPECIALISTS=stand_in`, `app/agents/mock.py` stands in for both: `mock.cash_flow` and `mock.credit_behavior` take the same request type and return the same response type as the real `cash_flow` and `card_service`, built from `data/shared_customers.yaml`; the tests use them too. `POST /signals` refuses a customer these files do not hold.
