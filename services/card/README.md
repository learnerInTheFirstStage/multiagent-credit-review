# Card Service

Owner: James.

Customers, limits, card spend and repayments, served as read-only analysis tools. It also carries out an action after a person approves it. No model runs here.

The Credit Behavior Agent that calls these tools lives in `services/orchestrator/app/agents/card_service`.

Today: `GET /snapshots`, the analysis tools, `POST /customers/{id}/actions` (all below) and `GET /health`. Every start refuses to run without `JWT_SECRET`, creates five tables and, when the database is empty, seeds the 12 customers from `data/shared_customers.yaml` (`SHARED_CUSTOMERS_YAML` overrides the path). After changing that file, run `docker compose down -v` and `docker compose up --build`, since the file is copied into the image. To seed an empty database by hand: `docker compose exec card python -m app.db.seed`. Tests: `services/card/tests`. Nothing calls Card's data yet: the Orchestrator's stand-in Credit Behavior specialist reads the YAML file directly.

Next: the tasks under the "Card Service + Credit Behavior Agent" epic (KAN-6). The topics the Credit Behavior Agent must answer are listed in `services/orchestrator/README.md` (Agents). The evidence kinds are `KIND_TOPIC` in `services/orchestrator/app/graph/causes.py`, and `services/orchestrator/app/agents/mock.py` returns every kind and key the checks read.

Database: `card`. Local port: 8003. Not routed by the gateway; only the Orchestrator calls it.

## GET /snapshots

A few raw numbers per customer, with no judgment attached. Every case starts from these: they are the card half of a signal's snapshot (`contracts.signal.CardSnapshot`), plus the limit and the account that pays the card.

```bash
TOKEN=$(docker compose exec -T card python -m app.dev_token --sub detection-job --scope snapshots:read)
curl -s -H "Authorization: Bearer $TOKEN" "localhost:8003/snapshots?customer_id=c_010"
```

```json
{"as_of": "2026-11-10",
 "customers": [{"customer_id": "c_010", "credit_limit_cents": 10000000, "balance_cents": 9200000,
                "used_share_bp": 9200, "card_status": "active", "funding_account_id": "acct_c010_a",
                "repayments": [{"due": "2026-09-05", "amount_cents": 4500000, "status": "paid"},
                               {"due": "2026-10-05", "amount_cents": 4500000, "status": "paid"},
                               {"due": "2026-11-05", "amount_cents": 5000000, "status": "failed"}]}]}
```

- Query: `customer_id` for one customer (all when left out); `as_of` to read the ledger at the end of an earlier day (default and latest: `AS_OF_DATE`, or the shared file's `signal_date` when it is unset). A later day is a 422, an unknown customer a 404.
- Token: role `service` with `snapshots:read` in a `scope` claim (a list, or a space-separated string). An agent's token names one customer and no scope, so it cannot read every customer. `python -m app.dev_token` makes one for local use.
- `balance_cents`: everything authorized and not declined, less every completed repayment, at the end of `as_of`. `used_share_bp` is that over the limit in basis points.
- `repayments`: the last three due on or before `as_of` whose outcome is known, oldest first. `paid` settled by its due date, `late` settled after it (or is past due and not settled yet), `failed` came back from the bank. The rules are in `app/ledger.py`, which the analysis tools use too.

## Analysis tools

`GET /customers/{customer_id}/tools/<name>?...`, called by the Credit Behavior Agent with a short-lived service token that names the customer (any other token is a 403). The envelope is the Bank tools' one: `{data, evidence}`. Code works out every number; "unusual" means unlike this customer's own past, and where there is too little past a tool says `too_short_to_tell`, never steady. Code is in `app/api/tools.py`.

| Tool | Orchestrator topic | Arguments | Answers | Evidence |
|---|---|---|---|---|
| `repayments` | repayments | `count` (1-13, default 6) | the latest repayments that are due, each `paid`, `late` (with `days_late`) or `failed` (with the NACHA code and `paid_since`), and the counts | `card_repayment` per repayment, `repayment_summary` |
| `paying_account` | paying_account | `count` (2-13, default 3) | whether the account the card is paid from changed in the three months before the as-of date (`customers.funding_account_since`, from the account that paid the repayments before it): `changed`, `from`, `to`, `changed_on`; `count` only picks the repayments listed with it | `funding_account_change` under `rep_<customer>_funding`, the same whatever `count`; and the repayments listed |
| `utilization` | card_use | `on` (a day, default the as-of date) | balance over the limit at the end of that day and `utilization_trend` against the same day of the three months before: `spiking` (25+ points up and at 90%+), `rising` (15+ up), `falling` (15+ down), `steady`, or `too_short_to_tell` | `card_balance` for the day and the three earlier ones |
| `spend_by_cycle` | card_use | `cycles` (3-13, default 6) | spend per statement cycle; the open cycle (projected when not over) against the median of the closed ones, `unusual` at +50% / -40%; `too_short_to_tell` with fewer than 3 closed cycles | `card_spend_series` |
| `transactions` | drill-down | `start`, `end`, and any of `merchant`, `category`, `mcc`, `card_id`, `state` (approved, declined, any), `min_amount_cents`, `limit` (≤ 50) | the matching transactions, largest first, with totals and declines by reason | `card_transaction` per row, `card_transaction_search` |
| `spend_breakdown` | drill-down | `start`, `end`, `by` (category, merchant, card, mcc) | approved spend per group against the period of the same length just before, with each group's share and whether it is new | `card_spend_breakdown` |

Windows end on or before the as-of date and start at most 400 days before it.

Evidence ids keep their content, since the Orchestrator refuses an answer in which a known id comes back changed. A row's record depends only on the row, read at the as-of date: `rep_c_010_202611` (repayment), `ctx_c_003_00942` (transaction), `bal_card_c_010_20261110` (the balance at the end of a day), `rep_c_006_funding` (the paying account; unchanged reads `changed: false` with `from` = `to`). An aggregate is `agg_` plus 8 hex of a hash over the tool, the customer and its arguments, so the same question gives the same id; an argument left out of the hash shapes `data`, never the record (the `transactions` page size: `shown` and `truncated` are in `data` only). The kinds and keys the Orchestrator's rules read are kept as it reads them (`app/api/evidence.py`): for example `card_repayment.status` is `paid`, `late` or `failed`, and `funding_account_change` has `changed`, `from` and `to`.

What the seeded customers give (tested in `tests/test_tools.py`): c_003 utilization `rising`, spend `up`, the Nov 5 repayment `late` by 3 days, two declines at the limit; c_010 `spiking`, the Nov 5 repayment `failed`, R01, `paid_since: no`; c_006 paying account changed from `acct_c006_a` to `acct_external_new_c006` on 2026-09-20; every other customer `steady`, spend not unusual, account unchanged.

## POST /customers/{customer_id}/actions

Carries out an action a person approved. Called by the Orchestrator's code (`services/orchestrator/app/card_client.py`) after the approval step, with the approver's own token; never by an agent. Code: `app/api/actions.py`.

```json
{"type": "reduce_limit", "limit_cents": 5800000, "case_id": "case_ab12cd34", "customer_id": "c_010",
 "approved_by": "u_analyst", "approval_id": "case_ab12cd34:analyst"}
{"type": "pause_card", "case_id": "case_ab12cd34", "customer_id": "c_010",
 "approved_by": "u_manager", "approval_id": "case_ab12cd34:manager"}
```

Card checks it all again:

- **The approver's own token.** Role `analyst`, `manager` or `admin` (a service token is a 403), `customers` names this customer, and `sub` is `approved_by`. With `AUTH0_DOMAIN` and `AUTH0_AUDIENCE` set, it is an Auth0 token, and a person's token signed with `JWT_SECRET` is a 401 (`services/auth/README.md`).
- **An approval id.** Without `approval_id`, or with one that is not `<case_id>:analyst` or `<case_id>:manager`, it is a 422. So is any other field, a `limit_cents` below 1 or not below the current limit, or a `limit_cents` on a pause.
- **The role again**, by the Orchestrator's own rules (`MANAGER_CUT`, `MAY_DECIDE`). A pause, or a cut of more than half the current limit, needs the `:manager` step, which only a manager decides. Any other reduction needs the `:analyst` step (an analyst or a manager); a manager's approval also covers it. An admin decides neither step, as in the Orchestrator.
- **Each approval once.** The receipt goes into `applied_actions`, keyed by `approval_id`, in the same transaction as the change, with the customer row locked, and is looked up only under that lock, so copies of one approval sent at once give one `applied` and the rest `already_applied`. A retry of the same approval by the same approver (the Orchestrator sends the same id when it retries) gets the stored receipt back as `already_applied` and changes nothing; the same id with another action or another approver is a 409, and a role that may not decide the step gets a 403 before any receipt is looked up. `tests/test_actions_postgres.py` checks the concurrent case on a real Postgres when `CARD_PG_URL` is set.

A reduction sets `customers.credit_limit_cents` and adds a `credit_limits` row with the `approval_id`, dated the data's as-of day, so `/snapshots` and the tools read the new limit. A pause sets `card_status` to `paused`. The answer, 200 either way:

```json
{"approval_id": "case_ab12cd34:analyst", "case_id": "case_ab12cd34", "customer_id": "c_010", "type": "reduce_limit",
 "status": "applied", "approved_by": "u_analyst", "approver_role": "analyst", "applied_at": "2026-11-17T21:42:59Z",
 "details": {"previous_limit_cents": 10000000, "limit_cents": 5800000, "cut_bp": 4200}}
```

## Tables

Money is integer cents. Columns follow what commercial card platforms expose (Ramp's Transaction and Transfer objects, Stripe Issuing, Brex).

| Table | A row is | Notes |
|---|---|---|
| `customers` | a business with a card program | the current limit, `card_status` (`active`, `paused`), `funding_account_id` (the bank account autopay pulls from) and since when, and when the card and payment feeds last reported |
| `credit_limits` | one limit the customer has had | seeded with the initial limit. `approval_id` is unique, so an approved change (KAN-28) applies once |
| `card_transactions` | one authorization and its settlement | `PENDING` → `CLEARED`, or `DECLINED` with the issuer's reason (`AUTHORIZER_BUSINESS_LIMIT`, `SUSPECTED_FRAUD`, ...); merchant descriptor and normalised name; ISO 18245 MCC; spend category; merchant amount and currency (a few hotel stays are in euros); card present; recurring |
| `applied_actions` | one approved action Card carried out | keyed by `approval_id`; the type, the new limit, who approved it and in which role, when, and what changed |
| `repayments` | one statement and the payment that settles it | statement period and balance, due date, `COMPLETED` / `RETURNED` / `INITIATED` / `SCHEDULED`, NACHA return code on a returned payment, and the account it was pulled from |

Ids: `ctx_c_001_00001` for a transaction (in time order), `rep_c_001_202611` for the repayment due in that month, `crd_c_001_1` for a card. `funding_account_id` uses the Bank Service's linked account ids (`acct_c006_a`), which is what lets the Orchestrator match the two sides.

## How the seed follows the file

The file fixes these numbers, and the seed reproduces them exactly (`tests/test_seed.py` checks each one against the file):

- the credit limit;
- the balance on the signal date, `round(credit_limit_cents * utilization_pct)`, so utilization worked out from the rows is the file's to the basis point;
- `last_3_repayments`: the statements due on Sep 5, Oct 5 and Nov 5, in that order;
- the amounts of the last three repayments, by the rule the evaluation's signal uses (`eval/signals.py`): `monthly_card_spend_cents`, except that the latest one, when late or failed, has its event's amount from `signal_window.card_events` (c_003's late one is $40K, c_010's failed one is $50K). A `late_repayment` or `failed_repayment` event whose status is not the latest repayment's stops the seed with an error. `tests/test_signal_agreement.py` checks `/snapshots` against the signal for all 27 customers;
- c_006's paying account: the first of the last three repayments comes from `acct_c006_a`, the last two from `acct_external_new_c006`.

The ledger, in short:

- A statement covers the 11th of one month to the 10th of the next and is due on the 5th of the month after, by ACH autopay. On 2026-11-10 the open cycle is Oct 11 to Nov 10. History starts 14 cycles earlier, on 2025-08-11, more than the 400 days a question may reach back.
- The balance is everything authorized and not declined, less every completed repayment. It never passes the limit (tested).
- **paid**: settled on the due date. **late**: paid by hand (`ach_manual`) three days after the due date. **failed**: `RETURNED`, `R01` insufficient funds, two days after the debit, and still owed on the signal date, unless the event says `repaid_after_days: N`. Then a second row for the same statement, `rep_{cid}_{YYYYMM}_r1` (`ach_manual`, `COMPLETED`), settles N days after the return, on or before the signal date, and the statement is no longer owed. The tools read that row as the failed repayment's `paid_since: yes` and `repaid_after_days`, not as a repayment of its own (`Book.due_by` keeps one row per statement). Without `repaid_after_days` (with or without `still_unpaid: true`) the statement stays owed; an event with both is an error.
- The last three closed cycles spend exactly their repayment's amount; the ones before spend the file's `monthly_card_spend_cents`, give or take 3%. The open cycle spends whatever brings the signal-date balance to the file's figure. For the two customers with a `utilization_trend` (c_003 rising, c_010 spiking) that spend is weighted towards the end of the cycle, and on each day it ends at 90% of the limit or more, one more authorization is declined `AUTHORIZER_BUSINESS_LIMIT`.
- Merchants, cards and subscriptions per customer are card-side detail the file leaves to this seeder (`PROFILES` in `app/db/seed.py`). The same file always gives the same rows and ids.

### More than one file

`SHARED_CUSTOMERS_YAML` takes several paths joined by `:`, for example `data/shared_customers.yaml:eval/extra_customers.yaml`, and the seed writes the customers of all of them (27 for those two). The files must have the same `schema_version` and `clock`, and a customer id in two files stops the seed with an error naming both. With no variable set, only the team's file is read, as before. The image holds only the team's file, so in a container any other file must be mounted at the path the variable names.

Every rule-relevant number comes from the file, so an evaluation customer is seeded like a team one. Only the card-side detail (`PROFILES`) is not in the file: each evaluation customer takes its team twin's (`TWINS` in `app/db/seed.py`: c_102 from c_002; c_103, c_203, c_303 from c_003; c_106 from c_006; c_107, c_307 from c_007; c_110, c_111 from c_010; c_201, c_301 from c_001; c_208 from c_008; c_304, c_314 from c_004), except c_013, a caterer twin of no one, which has a profile of its own. Any other id gets a plain `FALLBACK_PROFILE`. The twelve team customers' rows are unchanged.

If the open cycle's spend would take the balance past the limit on a day a statement is still owed (paid late in the cycle, or returned and paid since), the seed draws that cycle again with the spend before the last such payment capped at the limit, and the rest from that day on. Of the 27, this happens to c_203 (late, paid Nov 8) and c_111 (repaid Nov 10). c_111 is c_010's facts with the $50K statement paid on the signal date, yet still 92% used after it: its open cycle spends $92K, not c_010's $42K, of which $42K lands on Nov 10 itself. So on the card side c_111 is not a one-fact twin of c_010: its open cycle is about twice its usual spend.

### Where the file and a card ledger disagree

With a monthly statement and a 25-day grace period, the balance just before a due date holds a whole statement plus most of the next cycle, about 1.85 times a month's spend. So the seed refuses (a `ValueError` naming the customer) any `monthly_card_spend_cents` above 45% of the limit, rather than quietly spending less than the file says. The team's file once had four such customers (c_003, c_004, c_010, c_012, and their evaluation twins); their spend in the file is now the 45% figure the seed used to cap them at.

Also, for every customer the balance the file gives (limit times utilization) is not the same as a month's spend, so the open cycle's spend differs from the closed cycles by up to about 30% either way (c_004 up 29%, c_009 down 28%), and much more where the file gives a trend (c_003 up 89% on its `monthly_card_spend_cents`). A spend-trend tool needs a threshold wider than that before it calls a change unusual.

c_013's file gives no `utilization_trend`, but its failed $48K statement is still owed on top of the open cycle, so the balance on the signal date (88%) is about twice the earlier months' (45%), and the `utilization` tool calls it `rising`. The Orchestrator's stand-in reads only the file and gives no trend. No guideline reads the trend.

The file puts c_006's change of paying account in the signal window but also says the last two of the last three repayments use the new account. Those cannot both hold, since the Oct 5 repayment is before the window. The seed follows the second (`data_integrity`): the change is on 2026-09-20.
