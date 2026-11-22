# multiagent-credit-review

Credit Risk Review Agent: a course proof of concept modelled on the credit risk review workflow at Ramp. A risk engine flags a customer whose numbers moved, three agents work out why, and an analyst approves before anything changes.

All customers and data are simulated. This project is not affiliated with Ramp.

## Status

- **Orchestrator**: takes a review signal at `POST /signals`, investigates the case on a LangGraph graph, writes a report that code checks, pauses for the analyst and, where needed, the manager, and sends an approved action to Card. It asks the real Cash Flow and Card agents (`SPECIALISTS=stand_in` switches to stand-ins that read the data files). If Bank or Card is down, the case waits and retries, then fails with the reason. It keeps closed reviews as history and traces every model and tool call to LangFuse. See `services/orchestrator/README.md`.
- **Bank**: seeds every customer in `data/shared_customers.yaml` (and, on a stack started with `eval/docker-compose.eval.yml`, `eval/extra_customers.yaml`) as a complete bank feed (every movement of the week, 13 weeks of history, last year's same weeks) and serves the Cash Flow agent's read-only tools. See `services/bank/README.md`.
- **Card**: seeds the same customers with about 14 months of card history. `GET /snapshots` gives each customer's limit, balance and last three repayments. Its read-only tools serve the Card agent, and `POST /customers/{id}/actions` carries out an approved `reduce_limit` or `pause_card` once per approval.
- **Evaluation**: a golden set of 27 test customers with expected answers, graded by code (`eval/`). On the real model on 2026-11-17, 14 of them ran once each: the recommendation was right on 13 and the cause on 11. On 2026-11-18 c_010 ran from signal to Card's action following this README: `pause_card` under G-08b, approved by the analyst and the manager, card paused. On 2026-11-18 the full set ran once on Claude Sonnet 5.5: the recommendation was right on 24 of 27 and the cause on 23, with no wrong cut, for $11.51. On 2026-11-22 it ran again, on code whose investigation is `main`'s, with the same result and the same four misses, for $11.71 (`eval/golden-set-report-2026-11-22.md`); those cases can be loaded to show the review app (`demo/`).
- **Frontend**: the React review app: a login page, the case queue, and a case page where the analyst, and the manager where needed, approves or rejects. People sign in through Auth0 when it is configured; otherwise the login page gets a token from the Orchestrator's `POST /dev_login`, which answers only when `DEV_LOGIN` is on. See `frontend/README.md`.
- **Auth**: with `AUTH0_DOMAIN`, `AUTH0_AUDIENCE` and `AUTH0_CLIENT_ID` set, the review app signs people in through Auth0 and the Orchestrator and Card take people's tokens from Auth0 only; services keep signing their own with `JWT_SECRET` (`services/auth/README.md`). Left empty, the review app's dev login (`DEV_LOGIN=1`) and `python -m app.dev_token` make people's tokens.

## Architecture

```mermaid
flowchart LR
  browser[Browser] -->|sign in| auth0[Auth0, hosted]
  browser --> gateway[API gateway, Nginx]
  gateway --> frontend[Frontend]
  gateway --> orch[Orchestrator Service with all three agents]
  orch -->|read-only analysis tools| bank[Bank Service]
  orch -->|analysis tools and approved actions| card[Card Service]
  orch --> orchdb[(orchestrator)]
  bank --> bankdb[(bank)]
  card --> carddb[(card)]
```

Three FastAPI services do the work, each with its own Postgres database and login role. The gateway exposes the frontend, the Orchestrator and the Auth placeholder's `/health`; Bank and Card are called by the Orchestrator only.

People sign in through Auth0, which issues their tokens. Each service checks the tokens it receives itself: every one checks a service's token against `JWT_SECRET`, and the Orchestrator and Card check a person's against Auth0's keys. Bank takes service tokens only (`services/bank/README.md`). Without Auth0 settings, people's tokens are signed with `JWT_SECRET` too, by the dev login (`DEV_LOGIN=1`) or `python -m app.dev_token`. `services/auth` is a placeholder that answers `/health` behind `/api/auth/` and keeps the Auth0 setup; it issues no tokens.

All three agents run in the Orchestrator Service: the main agent and the two specialists (Cash Flow, Card), which it calls as tools in-process. Bank and Card run no model. They own the data and serve read-only analysis tools over HTTP. The stand-ins in `services/orchestrator/app/agents/mock.py` answer from the data files instead; the tests use them.

## Quick start

You need git, Docker and Python 3.12. A model API key is needed only to run cases (steps 3 and 4).

```bash
git clone https://github.com/lqc412/multiagent-credit-review.git
cd multiagent-credit-review
```

Everything below runs on `main`.

1. Run the tests: [Run the tests](#run-the-tests). No Docker, no key. This also creates the Python environment steps 3 and 4 use.
2. Start the stack and check the health URLs: [Run it](#run-it).
3. Run one case on the real model, from signal to Card's action: [Run one case end to end](#run-one-case-end-to-end).
4. Grade a few cases against the golden set: [Run the evaluation](#run-the-evaluation).

The stacks of steps 2, 3 and 4 use the same ports, so run one at a time (`docker compose -p <project> down -v` stops one).

## Run it

Needs Docker.

```bash
docker compose up --build
```

| URL | What |
|---|---|
| http://localhost:8080 | the review app (React) |
| http://localhost:8080/api/auth/health | the Auth placeholder through the gateway |
| http://localhost:8080/api/orchestrator/health | Orchestrator through the gateway |
| http://localhost:8001/health to :8004 | auth, bank, card, orchestrator directly (local only) |

To sign in on the review app with Auth0, put the three `AUTH0_*` values in `.env` and leave `DEV_LOGIN` empty (`services/auth/README.md`). For the dev login instead, copy `.env.example` to `.env` before starting: it sets `DEV_LOGIN=1`, which turns on the dev login (a name and a role, no password). Keep it to your machine: leave `DEV_LOGIN` empty on any host others can reach. Without a model (Configuration) the queue stays empty until a signal arrives, and a signal's case waits as `queued`.

To start again with empty databases: `docker compose down -v`. A volume seeded by an older version fails at start, so run it after pulling data or schema changes too.

To show the review app with the 27 golden-set cases the real model investigated on 2026-11-22, each waiting for the analyst: `demo/load.sh`. It deletes the local stack's volumes first and needs a model in `.env`; see `demo/README.md`.

### Run one case end to end

This spends money on the model (about $0.20 to $0.55 a case). Create `.env` from the example and set the model (see Configuration), and the LangFuse keys if you want traces:

```bash
cp .env.example .env
# then in .env: LLM_MODEL=anthropic:claude-sonnet-5-5, LLM_API_KEY=<your key>, LLM_STRUCTURED_OUTPUT=json_schema
```

Then, from the repository root, with the Orchestrator's test environment (see Run the tests):

```bash
docker compose -p e2e -f docker-compose.yml -f eval/docker-compose.eval.yml up -d --build
services/orchestrator/.venv/bin/python scripts/e2e.py
docker compose -p e2e down -v
```

It sends one customer's signal (c_010 by default), prints the agent's questions and the checked report, approves as the analyst (and the manager when needed), and checks that Card did what the report says. For c_010 the report should recommend `pause_card` under G-08b, and Card's card goes from `active` to `paused`. `--customer c_009` runs another customer; any customer in the two data files works. Options and expected results are in `scripts/README.md`.

### Run the tests

The tests need Python 3.12 and nothing else: no Docker, no database server, no model, no network. Each service has its own environment:

```bash
for s in orchestrator bank card; do
  (cd services/$s && python3.12 -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt ../../packages/contracts)
done
(cd services/orchestrator && .venv/bin/python -m pytest -q)
(cd services/bank && .venv/bin/python -m pytest -q)
(cd services/card && .venv/bin/python -m pytest -q)
```

Run them in a shell without the stack's variables (`LLM_*`, `LANGFUSE_*`, `SHARED_CUSTOMERS_*`): the tests set their own. Card's Postgres-only tests run when `CARD_PG_URL` points at a Postgres database, and are skipped otherwise.

Every pull request, and every push to main, runs these three suites and the frontend build on GitHub Actions (`.github/workflows/tests.yml`). GitHub does not block a merge on a failed run on this repository's plan, so check that the run is green before merging.

### Run the evaluation

`eval/run.py` sends every golden-set customer through a fresh stack and grades the answers: recommendation, cause, hand-over, citations, precision and recall of cuts, cost and time. It costs about $0.45 a case and run. To try a few cases once:

```bash
INVESTIGATION_FLOW=agent docker compose -p goldenset -f docker-compose.yml -f eval/docker-compose.eval.yml up -d --build postgres bank card orchestrator
services/orchestrator/.venv/bin/python eval/run.py --arm agent --only c_013 c_009 --reps 1 --container "docker compose -p goldenset exec -T orchestrator"
docker compose -p goldenset down -v
```

Each arm needs a fresh stack. Everything else, including how to compare arms, is in `eval/README.md`.

### Configuration

Compose falls back to local defaults for the passwords and the signing secret, so no `.env` file is needed on your machine. To override them, or on a server, copy `.env.example` to `.env` and set real values. `.env` is ignored by git.

| Variable | Used by | Notes |
|---|---|---|
| `POSTGRES_PASSWORD` | postgres | superuser password |
| `AUTH_DB_PASSWORD`, `BANK_DB_PASSWORD`, `CARD_DB_PASSWORD`, `ORCHESTRATOR_DB_PASSWORD` | postgres and the matching service | letters, digits and hyphens only, because they go into a connection URL |
| `JWT_SECRET` | the Orchestrator, Bank and Card | signs and checks tokens. One shared secret in the POC, 32 characters or more. The compose fallback is public, so anyone can sign tokens on a stack that runs without `.env`: set a real one on any shared or deployed stack |
| `AUTH0_DOMAIN`, `AUTH0_AUDIENCE`, `AUTH0_CLIENT_ID` | the Orchestrator and Card (domain, audience); the frontend build (all three) | the Auth0 tenant's domain, the API identifier and the review app's client id. All set: people sign in through Auth0 and `JWT_SECRET` signs services' tokens only; leave `DEV_LOGIN` empty then. Empty by default (`services/auth/README.md`) |
| `LLM_MODEL`, `LLM_API_KEY`, `LLM_STRUCTURED_OUTPUT` | Orchestrator | the model cases run on, as `provider:model`, for example `anthropic:claude-sonnet-5-5` or `openai:<model>` (both packages are installed; only Anthropic has been run). Empty by default: signals are stored and their cases wait as `queued`. `LLM_STRUCTURED_OUTPUT=json_schema` is recommended with Anthropic |
| `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` | Orchestrator | LangFuse tracing. Empty by default: cases run without tracing. Keys go in your own `.env` only. Set the base URL your LangFuse project shows, or keys from another region are refused |
| `SPECIALISTS` | Orchestrator | empty: the real Cash Flow and Card agents, which call Bank and Card. `stand_in`: the stand-ins, which answer from the data files with no service and no model |
| `EMBEDDINGS_MODEL`, `WITH_EMBEDDINGS` | Orchestrator | the embedder for the search over past reviews. Default `fastembed:BAAI/bge-small-en-v1.5`, built into the image (`WITH_EMBEDDINGS=1`, which fetches the model from huggingface.co). Offline, set `WITH_EMBEDDINGS=0` and `EMBEDDINGS_MODEL=hash:384` together. An image built before this default has no fastembed and offers no search; the next `up` rebuilds it |

The Orchestrator also reads these. `docker-compose.yml` does not pass them yet, so on the main stack they keep the defaults shown; the evaluation overlay sets them (`eval/README.md`).

| Variable | Default | What |
|---|---|---|
| `INVESTIGATION_FLOW` | empty, which is `agent` | who chooses the questions: `agent` (the model), `fixed` (a written plan) or `fixed_rules` (the plan, then what code's gap list names). Used to compare arms in the evaluation |
| `HISTORY_SEED_FILE` | `/eval/history.yaml` | past reviews seeded at every start |
| `SHARED_CUSTOMERS_FILE` | `/data/shared_customers.yaml`, the image's copy of `data/shared_customers.yaml` | the customer files the stand-in specialists answer from, separated by `:`. A signal for a customer they do not hold gets a 422 |

Database passwords are applied when the Postgres volume is first created. After changing one, run `docker compose down -v` and start again.

`AS_OF_DATE`: the day Bank and Card read their data on. Unset, they use the data file's `signal_date`; set it to that date or leave it. The Orchestrator does not read it, and `docker-compose.yml` does not pass it yet.

## Layout

| Path | What | Owner |
|---|---|---|
| `services/bank` | Bank Service: bank data and read-only analysis tools | Allen |
| `services/card` | Card Service: card data, read-only analysis tools, approved actions | James |
| `services/orchestrator` | Orchestrator Service: the graph, cases and approvals | Vincent |
| `services/orchestrator/app/agents/cash_flow` | Cash Flow Agent | Allen |
| `services/orchestrator/app/agents/card_service` | Credit Behavior Agent (Card Service agent) | James |
| `services/auth` | Auth0 sign-in: the post-login Action and setup | Vincent |
| `gateway` | Nginx API gateway | to be assigned |
| `frontend` | review app (React) | to be assigned |
| `packages/contracts` | models shared by the services | all three |
| `data` | test customers, one source for both data generators | all three |
| `eval` | golden dataset runs and results | to be assigned |
| `demo` | the 2026-11-22 run's cases, loaded on request for a demo | Vincent |
| `infra/postgres` | creates the four databases | to be assigned |

## Working agreements

- One branch per Jira ticket, named after it: `KAN-20-cash-flow-tools`. Open a pull request into `main` and put the ticket key in the title.
- Another member reviews before merge. GitHub does not enforce this on our plan, so it is on us.
- Services never import each other's code and never read each other's database. They talk over HTTP. Shared types live in `packages/contracts`.
- Inside the Orchestrator, each agent folder exposes one entry function. The graph calls that function and nothing else in the folder. An agent reaches data only through the analysis tools, never through a database connection.
- A change to `packages/contracts` or `data` needs all three owners.
- No secrets and no real customer data in git.
- `main` always starts with `docker compose up`. Every service built here is rebuilt from the checkout on each `up` (`pull_policy: build` in `docker-compose.yml`), with or without `--build`, so no container runs an image from before a pull. Expect those containers to restart on each `up`.

Board: https://lqc0304.atlassian.net/jira/software/projects/KAN/boards/2
