# Frontend — Review queue

Single-page app for the Credit Risk Review demo.

Login, see the open cases, open one, read the agent's report with the evidence it cited, and approve or reject the recommendation. A manager can confirm a pause or a large cut; an analyst or manager can carry out an approved hand-over by picking the action.

Stack: Vite + React + TypeScript + Tailwind CSS + React Router + TanStack Query. Multi-stage build: `node:22-alpine` runs the bundle, `nginx:1.27-alpine` serves it. In production it sits behind the gateway at port 8080; `/api/auth/...` and `/api/orchestrator/...` go to the backend, everything else lands on the SPA.

## Local

```
npm install
npm run dev
```

The dev server runs on 5173 and proxies `/api/*` to `http://127.0.0.1:8080`, which is the gateway container from `docker compose`. Bring the backend up first:

```
docker compose up -d
open http://localhost:8080/
```

The login page depends on how the bundle was built:

- **Auth0** when `AUTH0_DOMAIN`, `AUTH0_AUDIENCE` and `AUTH0_CLIENT_ID` are in `.env` (compose passes them to the build as `VITE_AUTH0_*`; for `npm run dev`, export the three `VITE_AUTH0_*` yourself). "Sign in with Auth0" asks for the account every time, so the analyst and the manager can take turns in one browser. The access token goes into the same session as below, so the rest of the app does not change. Our tenant's values and demo users: `services/auth/README.md`. Leave `DEV_LOGIN` empty then: the services refuse dev-login person tokens once Auth0 is on.
- **Dev login** otherwise: `POST /dev_login` on the Orchestrator, enabled by `DEV_LOGIN=1` in your local `.env`.

## Scripts

- `npm run dev` — Vite dev server
- `npm run build` — type-check + production bundle to `dist/`
- `npm run preview` — serve `dist/` locally

## Files worth knowing

- `src/api/client.ts` — fetch wrapper, token storage, typed `ApiError`
- `src/api/cases.ts` — all backend calls the UI makes
- `src/auth/AuthContext.tsx` — current user + token across the app
- `src/auth/auth0.ts`, `Auth0SignIn.tsx`, `WithAuth0.tsx` — the Auth0 sign-in, used only when the build has its settings
- `src/pages/LoginPage.tsx` — Auth0 sign-in, or the dev login that picks a role and a username
- `src/pages/QueuePage.tsx` — `GET /cases` grouped by status, polled every 5s
- `src/pages/CaseDetailPage.tsx` — `GET /cases/:id`, polled every 3s while the agent is working, plus the decision form and the investigation path
- `src/components/ReportCard.tsx` — likely cause, confidence, summary, evidence with citations, ruled-out causes, next action, guideline ids
- `src/components/DecisionForm.tsx` — approve / reject + optional note; adds the `decided` dropdown when the report is a hand-over
