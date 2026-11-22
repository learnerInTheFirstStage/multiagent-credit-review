# gateway

Nginx. The only container with a public port (8080 locally).

| Path | Goes to |
|---|---|
| `/health` | the gateway itself |
| `/api/auth/...` | Auth placeholder: `/health` only. People sign in through Auth0 (`services/auth/README.md`) |
| `/api/orchestrator/...` | Orchestrator Service |
| everything else | frontend |

The Bank and Card services are not routed. Only the Orchestrator calls them.

The gateway adds an `X-Request-ID` header to every request it forwards and returns it to the browser. Its access log writes each path without its query string, since the event stream carries the access token as `?token=`, and ends each line with `request_id=`. `scripts/smoke.sh` checks both.
