# Orchestrator Service

Owner: Vincent.

Turns a signal into a reviewed recommendation with evidence and carries the case through approval. It asks the Bank and Card services over HTTP and never reads their databases.

Today: `GET /health` only. Next: the tasks under the "Orchestrator Agent" epic (KAN-7).

Database: `orchestrator`. Local port: 8004. Behind the gateway: `/api/orchestrator/...`.
Inside the compose network the specialists are at `http://bank:8000` and `http://card:8000`.
