# Orchestrator Service

Owner: Vincent.

Turns a signal into a reviewed recommendation with evidence and carries the case through approval. All three agents run here: the main agent in the graph, and the two specialists under `app/agents/`, which the graph calls as tools in-process.

Today: `GET /health`, and `POST /signals`, which opens a case for a new signal and ignores a repeated one (no token check yet, that comes with KAN-35). Next: the tasks under the "Orchestrator Agent" epic (KAN-7).

Tables are created at startup. There are no migrations: all data is generated, so after a schema change run `docker compose down -v` and start again.

Database: `orchestrator`. Local port: 8004. Behind the gateway: `/api/orchestrator/...`.
Inside the compose network the Bank and Card services are at `http://bank:8000` and `http://card:8000`.

## Tests

```bash
cd services/orchestrator
pip install -r requirements-dev.txt ../../packages/contracts
pytest
```

They run on a throwaway SQLite file with foreign keys enforced, so no stack is needed.

## Agents

| Folder | Agent | Owner |
|---|---|---|
| `app/agents/cash_flow` | Cash Flow Agent | Allen |
| `app/agents/credit_behavior` | Credit Behavior Agent | James |

- Each folder exposes one entry function, `investigate(request) -> response`, with the shapes from `packages/contracts`. The graph calls only that function.
- An agent's tools call the read-only analysis endpoints on its service over HTTP. Nothing in an agent folder opens a database connection.
- A specialist holds no state and reports facts with evidence. It never recommends a limit action.
- Model and LangFuse settings are read in one place in this service, not in each agent folder.
