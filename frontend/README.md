# Frontend — Review queue

Single-page app for the Credit Risk Review demo.

Login, see the cases that wait on you (or every open or closed case), open one, read the agent's report with the evidence it cited, and approve or reject the recommendation. A rejection needs a reason (wrong cause, wrong action, missing evidence, data problem, or other with a note), and the decisions list shows it with the note. On a hand-over the analyst or manager picks the outcome from the choices the backend offers, each with what approving it will do; a picked pause or cut goes through the same steps as a recommended one. Before approving, the form says what approving will do, as the backend reports it: send the case to a manager, send the action to Card, draft a request for documents for the person to send, or only record the decision. A pause or a large cut needs a manager other than the person who approved the step before.

When request documents is approved, the case waits for the customer ("Waiting for documents") with the limit unchanged. The case page shows what was asked for, the due date, who asked and when, and the message code drafted for the customer, with a Copy button; nothing is sent to the customer from the app. When the documents come in, an analyst or a manager records what they showed (keep the limit, reduce it or pause the card, under the same rules), or closes the case without a change.

Once code has sent the action, the case page shows Card's receipt: the card status or the limit before and after, applied or already applied, when, who approved it and the approval id. A closed case with no action says that nothing was sent to Card and why. An analyst or a manager can retry a case that waits for Bank or Card, or failed for want of them. When Card did not confirm an approved action, the person who approved it can send it again, and a manager can close the case as failed, after which someone must check the card at Card.

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

- **Auth0** when `AUTH0_DOMAIN`, `AUTH0_AUDIENCE` and `AUTH0_CLIENT_ID` are in `.env` (compose passes them to the build as `VITE_AUTH0_*`; for `npm run dev`, export the three `VITE_AUTH0_*` yourself). Its "Sign in" button asks for the account every time, so the analyst and the manager can take turns in one browser. The access token goes into the same session as below, so the rest of the app does not change. Our tenant's values and demo users: `services/auth/README.md`. Leave `DEV_LOGIN` empty then: the services refuse dev-login person tokens once Auth0 is on.
- **Dev login** otherwise: `POST /dev_login` on the Orchestrator, enabled by `DEV_LOGIN=1` in your local `.env`.

## Scripts

- `npm run dev` — Vite dev server
- `npm run build` — type-check + production bundle to `dist/`
- `npm run preview` — serve `dist/` locally

## Files worth knowing

- `src/api/client.ts` — fetch wrapper, token storage, typed `ApiError`
- `src/lib/log.ts`: the app's log: a JSON line in the browser console for a failed call (method, path, status, the gateway's request ID), an event stream that falls back to polling, a page that failed to render (`src/components/AppErrorBoundary.tsx`, which shows a reload message instead of a blank page) and an uncaught error
- `src/api/cases.ts` — all backend calls the UI makes
- `src/auth/AuthContext.tsx` — current user + token across the app
- `src/auth/auth0.ts`, `Auth0SignIn.tsx`, `WithAuth0.tsx` — the Auth0 sign-in, used only when the build has its settings
- `src/pages/LoginPage.tsx` — Auth0 sign-in, or the dev login that picks a role and a username
- `src/pages/QueuePage.tsx` — `GET /cases` in three tabs, polled every 5s and refreshed by the event stream: "Waiting on me" (the analyst's approvals and documents; for a manager the manager step first, leaving out cases they approved the step before; a Card retry waits on its approver and on managers, and everyone else sees who must send it again; one Card refused waits on managers only, since sending it again would get the same answer), "All open" and "Closed", each grouped by status, with a Card retry grouped on its own before the statuses; a row shows the recommendation, confidence, the engine's cut, whose move it is and how long it has waited
- `src/pages/CaseDetailPage.tsx` — `GET /cases/:id`, polled every 3s while the agent is working. Top to bottom: a verdict strip (the recommendation, or the outcome a person picked, who signs, and Go to decision); after a decision, what it did, with Card's receipt and the next steps; "Retry now" on an outage (`POST /cases/:id/retry`); the engine's proposal; the report; the investigation path (its raw JSON only with `?debug` in the URL); the decisions made (each with its reason and note); the documents request; and the decision form or the Card retry panel
- `src/components/EngineLimitHeader.tsx` — what the engine proposed and where the customer stands, from the signal's `sig_` records (code's figures, put in words): the current and proposed limit, what the customer owes now and what a cut below it means for spending, the fact behind each review reason (the cash fall, the failed repayment and its due date, the account that stopped reporting), each linked account's last report, and the repayment record. An account reads as its bank names it, "Chase Business Checking ••4821" (`accountLabel` in `src/lib/format.ts`), with its id in the tooltip; one without a name shows its id. The evidence chips and the evidence drawer name a `linked_account` record the same way, and the drawer lists its fields as Name and Last 4 digits
- `src/components/NextSteps.tsx` — once a case is over, what happens next, simulated for the demo (nothing is sent; ticks stay in the browser): Card's change and what it means for spending, the adverse action notice due within 30 days with its reasons drafted from the signal's figures (and, for a pause after a failed repayment, that compliance confirms whether one is owed), the repayment that lifts a pause, and that monitoring goes on
- `src/components/PastReviewCard.tsx` — in place of a report on a review imported from earlier records (`past_review`): its date, figures, cause, outcome, findings and decisions with their notes, saying it was not investigated in this system; the queue marks such rows as imported
- `src/components/ReportCard.tsx` — who handed a hand-over to a person, likely cause, confidence, summary, what the report found for each review reason, evidence, ruled-out causes and next action; what else was found and the cited guidelines as cards (when each applies, what it allows, its note) fold under Supporting detail, which opens by itself when code gave up; every cited id opens its record
- `src/components/InvestigationTimeline.tsx` — how the case was investigated, from `GET /cases/:id/path`: a short story, one sentence per fact, from which the steps open: each question (specialist, topic, the causes it checks for, the specialist's answer with each finding's confidence and sources, what it could not tell, records returned), each gap check where it ran with the questions it found unasked and why, the past-case searches, and each report draft with what the rule check refused it for; more than 4 record ids fold behind their count
- `src/components/DecisionForm.tsx` — approve / reject with a required rationale of at least three words (credit decisions are later sampled for the analysis behind them); a reject picks its `reason`; approving the agent's recommendation sends `agree`, approving an outcome a person picked sends no reason; at the analyst step of a hand-over, approve or reject as on any case, and approving then asks which outcome (`decided`) in the same toggle, with what approving it will do; a hand-over's reject reasons leave out "wrong action", as it made no recommendation; at the documents step, what the documents showed or "Close without a change" (a reject); says what approving will do, from the case's `approval_outcomes`; at the manager step, shows a notice instead of the form to the person who approved the step before; every decision names the step the page showed (`step`), so one made on a page the case has moved on from gets a 409 and the page re-reads the case
- `src/components/DocumentsRequestPanel.tsx` — the case's `documents_request`: "Waiting for the customer" while the case waits (asked for, due, requested by and when, the drafted message with Copy), kept as "Documents requested" after it closes
- `src/components/CardOutcome.tsx` — at the top of the case page under the verdict strip, with NextSteps: Card's receipt from the case's `action.result`, or why nothing was sent; and the panel that replaces the decision form when Card did not confirm an approved action (send it again, or close as failed with a required note: a failure at Card is not a judgment of the report, so it takes no reject reason); when the case's `card_refusal` says Card refused the action, the panel shows Card's words and does not offer to send it again
