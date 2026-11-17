# frontend

Today: one static placeholder page served by Nginx. It calls the two health endpoints behind the gateway, so you can see the whole path working.

Next (KAN-38 and the stories under the "Frontend review app" epic): a React app with React Router, built into static files and served by this same Nginx container. Pages: login, case queue, case page, approve or reject. A decision is approve or reject with an optional note. When the analyst approves a report that hands the case over, it also carries `decided` (`keep_limit`, `reduce_limit`, `pause_card` or `request_documents`). There is no edit. The endpoints it needs (`GET /cases`, `GET /cases/{id}`, `GET /cases/{id}/path`, `POST /cases/{id}/decision`) are described in `services/orchestrator/README.md`.

The frontend talks only to the gateway: `/api/auth/...` and `/api/orchestrator/...`.
