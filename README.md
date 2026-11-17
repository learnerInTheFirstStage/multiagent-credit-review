# multiagent-credit-review

Credit Risk Review Agent: a course proof of concept modelled on the credit risk review workflow at Ramp. A risk engine flags a customer whose numbers moved, three agents work out why, and an analyst approves before anything changes.

All customers and data are simulated. This project is not affiliated with Ramp.

## Status

- **Orchestrator**: takes a review signal at `POST /signals`, investigates the case on a LangGraph graph, writes a report that code checks, pauses for the analyst and, where needed, the manager, and sends an approved action to Card. It keeps closed reviews as history, traces to LangFuse, and has a golden-set evaluation in `eval/`. It has run on Claude Sonnet 5.5 from a signal to the analyst's decision; the paid evaluation has not been run yet. The two specialists are still in-process stand-ins that read `data/shared_customers.yaml`. See `services/orchestrator/README.md`.
- **Bank**: creates its six tables and seeds the 12 customers at start. No endpoint besides `/health` yet.
- **Card**: creates its five tables (customers, credit_limits, card_transactions, repayments, applied_actions) and seeds the 12 customers at start, with about 14 months of card history. `GET /snapshots` gives each customer's limit, balance and last three repayments (KAN-27). Its analysis tools serve the Credit Behavior Agent (KAN-25, KAN-26), and `POST /customers/{id}/actions` carries out an approved `reduce_limit` or `pause_card` once per approval (KAN-28).
- **Auth, frontend**: skeletons. Auth answers `/health`; tokens come from `python -m app.dev_token` in the Orchestrator container. The frontend is a placeholder page.

## Architecture

```mermaid
flowchart LR
  browser[Browser] --> gateway[API gateway, Nginx]
  gateway --> frontend[Frontend]
  gateway --> auth[Auth Service]
  gateway --> orch[Orchestrator Service with all three agents]
  orch -->|read-only analysis tools| bank[Bank Service]
  orch -->|analysis tools and approved actions| card[Card Service]
  auth --> authdb[(auth)]
  orch --> orchdb[(orchestrator)]
  bank --> bankdb[(bank)]
  card --> carddb[(card)]
```

Four FastAPI services, each with its own Postgres database and login role. The gateway exposes only the frontend, Auth and the Orchestrator. The Bank and Card services are called by the Orchestrator only.

All three agents run in the Orchestrator Service: the main agent and the two specialists (Cash Flow, Credit Behavior), which it calls as tools in-process. Bank and Card run no model. They own the data and serve read-only analysis tools over HTTP. Both specialists are built (`app/agents/cash_flow`, `app/agents/card_service`) and Bank and Card serve their tools, but the graph still calls the stand-ins in `services/orchestrator/app/agents/mock.py` until KAN-36 swaps them.

## Run it

Needs Docker.

```bash
docker compose up --build
```

| URL | What |
|---|---|
| http://localhost:8080 | frontend placeholder |
| http://localhost:8080/api/auth/health | Auth through the gateway |
| http://localhost:8080/api/orchestrator/health | Orchestrator through the gateway |
| http://localhost:8001/health to :8004 | auth, bank, card, orchestrator directly (local only) |

To start again with empty databases: `docker compose down -v`.

### Configuration

Compose falls back to local defaults for the passwords and the signing secret, so no `.env` file is needed on your machine. To override them, or on a server, copy `.env.example` to `.env` and set real values. `.env` is ignored by git.

| Variable | Used by | Notes |
|---|---|---|
| `POSTGRES_PASSWORD` | postgres | superuser password |
| `AUTH_DB_PASSWORD`, `BANK_DB_PASSWORD`, `CARD_DB_PASSWORD`, `ORCHESTRATOR_DB_PASSWORD` | postgres and the matching service | letters, digits and hyphens only, because they go into a connection URL |
| `JWT_SECRET` | the Orchestrator, Bank and Card | signs and checks tokens. One shared secret in the POC, 32 characters or more. The compose fallback is public, so anyone can sign tokens on a stack that runs without `.env`: set a real one on any shared or deployed stack |
| `LLM_MODEL`, `LLM_API_KEY`, `LLM_STRUCTURED_OUTPUT` | Orchestrator | the model cases run on, as `provider:model`, for example `anthropic:claude-sonnet-5-5` or `openai:<model>` (both packages are installed; only Anthropic has been run). Empty by default: signals are stored and their cases wait as `queued`. `LLM_STRUCTURED_OUTPUT=json_schema` is recommended with Anthropic |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | Orchestrator | LangFuse tracing. Empty by default: cases run without tracing. Keys go in your own `.env` only. Set the base URL your LangFuse project shows, or keys from another region are refused |

The Orchestrator also reads these. `docker-compose.yml` does not pass them yet, so on the main stack they keep the defaults shown; the evaluation overlay sets them (`eval/README.md`).

| Variable | Default | What |
|---|---|---|
| `INVESTIGATION_FLOW` | empty, which is `agent` | who chooses the questions: `agent` (the model), `fixed` (a written plan) or `fixed_rules` (the plan, then what code's gap list names). Used to compare arms in the evaluation |
| `EMBEDDINGS_MODEL` | `hash:384` (set in the image) | the embedder for the search over past reviews. `fastembed:BAAI/bge-small-en-v1.5` needs an image built with `WITH_EMBEDDINGS=1` |
| `HISTORY_SEED_FILE` | `/eval/history.yaml` | past reviews seeded at every start |
| `SHARED_CUSTOMERS_FILE` | `/data/shared_customers.yaml`, the image's copy of `data/shared_customers.yaml` | the customer files the stand-in specialists answer from, separated by `:`. A signal for a customer they do not hold gets a 422 |

Database passwords are applied when the Postgres volume is first created. After changing one, run `docker compose down -v` and start again.

Reserved for later, so every service uses the same name: `AS_OF_DATE`.

## Layout

| Path | What | Owner |
|---|---|---|
| `services/bank` | Bank Service: bank data and read-only analysis tools | Allen |
| `services/card` | Card Service: card data, read-only analysis tools, approved actions | James |
| `services/orchestrator` | Orchestrator Service: the graph, cases and approvals | Vincent |
| `services/orchestrator/app/agents/cash_flow` | Cash Flow Agent | Allen |
| `services/orchestrator/app/agents/card_service` | Credit Behavior Agent (Card Service agent) | James |
| `services/auth` | login, JWT, roles | to be assigned |
| `gateway` | Nginx API gateway | to be assigned |
| `frontend` | review app (React) | to be assigned |
| `packages/contracts` | models shared by the services | all three |
| `data` | test customers, one source for both data generators | all three |
| `eval` | golden dataset runs and results | to be assigned |
| `infra/postgres` | creates the four databases | to be assigned |

## Working agreements

- One branch per Jira ticket, named after it: `KAN-20-cash-flow-tools`. Open a pull request into `main` and put the ticket key in the title.
- Another member reviews before merge. GitHub does not enforce this on our plan, so it is on us.
- Services never import each other's code and never read each other's database. They talk over HTTP. Shared types live in `packages/contracts`.
- Inside the Orchestrator, each agent folder exposes one entry function. The graph calls that function and nothing else in the folder. An agent reaches data only through the analysis tools, never through a database connection.
- A change to `packages/contracts` or `data` needs all three owners.
- No secrets and no real customer data in git.
- `main` always starts with `docker compose up --build`.

Board: https://lqc0304.atlassian.net/jira/software/projects/KAN/boards/2
