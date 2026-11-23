# Auth Service

People sign in through Auth0 (KAN-37, KAN-54). This service issues no tokens: it holds the Auth0 post-login Action (`auth0/post-login.js`) and answers `GET /health` for the stack. No agent.

## Tokens

- **A person's** (`analyst`, `manager`, `admin`): an RS256 access token from Auth0. The Orchestrator and Card check it against the tenant's published keys, its issuer and the API audience. The Action adds three claims: `https://credit-review.demo/role`, the person's one role, `https://credit-review.demo/customers`, the customer ids they review, and `https://credit-review.demo/email`, which the Orchestrator stamps on a decision as `by_name` so the app shows who decided by email (the sub stays who decided). Managers and admins need customers too, or their queue is empty. Card checks the token again when the Orchestrator passes on the approver's token with an action.
- **A service's** (`service`): HS256, signed with `JWT_SECRET`. The signal sender uses one, and each specialist signs one for its case that names one customer. Auth0 is not involved.

With `AUTH0_DOMAIN` and `AUTH0_AUDIENCE` set, the services accept HS256 tokens for role `service` only. With both unset, people's tokens are HS256 too, made by `python -m app.dev_token` in the Orchestrator container (`services/orchestrator/README.md`, Tokens and decisions). Local runs, the tests and the evaluation work that way.

## Our tenant

Set up on 2026-11-19 in a development tenant the team shares with another project. None of these values is secret.

| | |
|---|---|
| `AUTH0_DOMAIN` | `dev-pd2uh1dimju6bx2a.us.auth0.com` |
| `AUTH0_AUDIENCE` | `https://api.credit-review.demo` (API "Credit Review API") |
| `AUTH0_CLIENT_ID` | `Nwe76UaM6WetJUAsrc3xfEt8yiMzc6tj` (SPA "Credit Review Dashboard"; callbacks `/login` on `http://localhost` and `http://127.0.0.1`, ports 8080, 5173 and 8088 (kind, `k8s/README.md`); logout and web origins the same addresses without `/login`) |
| Action | `credit-review-claims` on the post-login trigger |
| Demo users | `analyst@credit-review.demo` (analyst), `manager@credit-review.demo` (manager) and `admin@credit-review.demo` (admin), customers `c_001` to `c_012`; ask the team lead for the passwords |

## Setting up the Auth0 tenant

1. Create an API with the identifier `https://api.credit-review.demo` (the audience; another one must also go into `AUDIENCE` in `auth0/post-login.js`). Signing algorithm: RS256. The Action touches only logins that ask for this API, so other apps on the same tenant keep working as they did.
2. Create a Single Page Application for the dashboard, and let it request tokens for the API (its API access tab). Allowed callback URLs: the dashboard's `/login`, for example `http://localhost:8080/login`. Logout and web origin URLs: the dashboard's address, `http://localhost:8080`.
3. Create the users. Give each one role:
   - with Auth0 Roles, create `analyst`, `manager` and `admin` and assign one;
   - otherwise put the role in the user's app_metadata: `{"role": "manager"}`.
   Put their customers in app_metadata: `{"customers": ["c_002", "c_010"]}`.
4. Create a post-login Action from `auth0/post-login.js`, deploy it and add it to the post-login trigger. A login with no role, or more than one, is refused.
5. In `.env`, set `AUTH0_DOMAIN` to the tenant's domain (for example `your-tenant.us.auth0.com`), `AUTH0_AUDIENCE` to the API identifier and `AUTH0_CLIENT_ID` to the SPA's client id, and leave `DEV_LOGIN` empty. Then `docker compose up -d --build`: the frontend is built with them and its sign-in page signs in through Auth0, and the Orchestrator and Card check its tokens.

From then on the Orchestrator and Card accept only service tokens from `app.dev_token` and the dev login: both still make person tokens, but the services refuse them (401). The dashboard asks Auth0 for a token with `audience` set to the API identifier (`frontend/src/auth/WithAuth0.tsx`); without it, Auth0 returns an access token the services cannot check. Public sign-up on the tenant's database connection should be off: a signed-up account gets no token for this API (the Action refuses an account with no role), but it is better not to have one.

Database: `auth` (unused for now). Local port: 8001. Behind the gateway: `/api/auth/...`.
