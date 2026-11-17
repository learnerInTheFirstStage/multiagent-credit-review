# Auth Service

Login, JWT, roles (analyst, manager, admin, service) and which analyst covers which customers. No agent.

Today: `GET /health` only. Next: KAN-37 and the login story (KAN-54).

The token the Orchestrator already checks: HS256, signed with `JWT_SECRET`, with the claims `sub`, `exp` and `role` (`analyst`, `manager`, `admin` or `service`), all three required, and `customers`, the customer ids the token may see. Every role that reads or decides cases needs `customers`, managers and admins included; it is empty when left out. `docker-compose.yml` passes `JWT_SECRET` only to the Orchestrator today. Until Auth issues tokens, `python -m app.dev_token` in the Orchestrator container makes them (`services/orchestrator/README.md`, Tokens and decisions).

Database: `auth`. Local port: 8001. Behind the gateway: `/api/auth/...`.
