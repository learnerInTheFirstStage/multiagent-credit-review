# gateway

Nginx. The only container with a public port (8080 locally).

| Path | Goes to |
|---|---|
| `/health` | the gateway itself |
| `/api/auth/...` | Auth Service |
| `/api/orchestrator/...` | Orchestrator Service |
| everything else | frontend |

The Bank and Card services are not routed. Only the Orchestrator calls them.

The gateway adds an `X-Request-ID` header to every request it forwards.
