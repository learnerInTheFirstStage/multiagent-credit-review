# frontend

Today: one static placeholder page served by Nginx. It calls the two health endpoints behind the gateway, so you can see the whole path working.

Next (KAN-38 and the stories under the "Frontend review app" epic): a React app with React Router, built into static files and served by this same Nginx container. Pages: login, case queue, case page, approve / edit / reject.

The frontend talks only to the gateway: `/api/auth/...` and `/api/orchestrator/...`.
