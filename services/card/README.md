# Card Service

Owner: James.

Customers, limits, card spend and repayments, served as read-only analysis tools. It also carries out an action after a person approves it. No model runs here.

The Credit Behavior Agent that calls these tools lives in `services/orchestrator/app/agents/credit_behavior`.

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
| `paying_account` | paying_account | `count` (2-13, default 3) | whether the account the card is paid from changed across those repayments: `changed`, `from`, `to`, `changed_on` | `funding_account_change`, and the repayments compared |
| `utilization` | card_use | `on` (a day, default the as-of date) | balance over the limit at the end of that day and `utilization_trend` against the same day of the three months before: `spiking` (25+ points up and at 90%+), `rising` (15+ up), `falling` (15+ down), `steady`, or `too_short_to_tell` | `card_balance` for the day and the three earlier ones |
| `spend_by_cycle` | card_use | `cycles` (3-13, default 6) | spend per statement cycle; the open cycle (projected when not over) against the median of the closed ones, `unusual` at +50% / -40%; `too_short_to_tell` with fewer than 3 closed cycles | `card_spend_series` |
| `transactions` | drill-down | `start`, `end`, and any of `merchant`, `category`, `mcc`, `card_id`, `state` (approved, declined, any), `min_amount_cents`, `limit` (≤ 50) | the matching transactions, largest first, with totals and declines by reason | `card_transaction` per row, `card_transaction_search` |
| `spend_breakdown` | drill-down | `start`, `end`, `by` (category, merchant, card, mcc) | approved spend per group against the period of the same length just before, with each group's share and whether it is new | `card_spend_breakdown` |

Windows end on or before the as-of date and start at most 400 days before it.

Evidence ids keep their content, since the Orchestrator refuses an answer in which a known id comes back changed. A row's record depends only on the row, read at the as-of date: `rep_c_010_202611` (repayment), `ctx_c_003_00942` (transaction), `bal_card_c_010_20261110` (the balance at the end of a day). An aggregate is `agg_` plus 8 hex of a hash over the tool, the customer and its arguments, so the same question gives the same id. The kinds and keys the Orchestrator's rules read are kept as it reads them (`app/api/evidence.py`): for example `card_repayment.status` is `paid`, `late` or `failed`, and `funding_account_change` has `changed`, `from` and `to`.

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

- **The approver's own token.** Role `analyst`, `manager` or `admin` (a service token is a 403), `customers` names this customer, and `sub` is `approved_by`.
- **An approval id.** Without `approval_id`, or with one that is not `<case_id>:analyst` or `<case_id>:manager`, it is a 422. So is any other field, `limit_cents` that is not below the current limit, or a `limit_cents` on a pause.
- **The role again**, by the Orchestrator's own rules (`MANAGER_CUT`, `MAY_DECIDE`). A pause, or a cut of more than half the current limit, needs the `:manager` step, which only a manager decides. Any other reduction needs the `:analyst` step (an analyst or a manager); a manager's approval also covers it. An admin decides neither step, as in the Orchestrator.
- **Each approval once.** The receipt goes into `applied_actions`, keyed by `approval_id`, in the same transaction as the change, with the customer row locked. A retry of the same approval (the Orchestrator sends the same id when it retries) gets the stored receipt back as `already_applied` and changes nothing; the same id with another action is a 409.

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
- the amount of a late or failed repayment, from `signal_window.card_events`: c_003's late one is $40K, c_010's failed one is $50K;
- c_006's paying account: the first of the last three repayments comes from `acct_c006_a`, the last two from `acct_external_new_c006`.

The ledger, in short:

- A statement covers the 11th of one month to the 10th of the next and is due on the 5th of the month after, by ACH autopay. On 2026-11-10 the open cycle is Oct 11 to Nov 10. History starts 14 cycles earlier, on 2025-08-11, more than the 400 days a question may reach back.
- The balance is everything authorized and not declined, less every completed repayment. It never passes the limit (tested).
- **paid**: settled on the due date. **late**: paid by hand (`ach_manual`) three days after the due date. **failed**: `RETURNED`, `R01` insufficient funds, two days after the debit, and still owed on the signal date.
- Each closed cycle spends the file's `monthly_card_spend_cents`, give or take 3%, capped at 45% of the limit (see below). The open cycle spends whatever brings the signal-date balance to the file's figure. For the two customers with a `utilization_trend` (c_003 rising, c_010 spiking) that spend is weighted towards the end of the cycle, and on each day it ends at 90% of the limit or more, one more authorization is declined `AUTHORIZER_BUSINESS_LIMIT`.
- Merchants, cards and subscriptions per customer are card-side detail the file leaves to this seeder (`PROFILES` in `app/db/seed.py`). The same file always gives the same rows and ids.

### Where the file and a card ledger disagree

With a monthly statement and a 25-day grace period, the balance just before a due date holds a whole statement plus most of the next cycle, about 1.85 times a month's spend. Four customers' `monthly_card_spend_cents` is too high for their limit on those terms, so the seed caps their closed cycles at 45% of the limit:

| Customer | Monthly spend in the file | Limit | Seeded closed cycles |
|---|---|---|---|
| c_003 | $100K | $125K | $56.25K |
| c_004 | $80K | $150K | $67.5K |
| c_010 | $90K | $100K | $45K |
| c_012 | $65K | $120K | $54K |

Also, for every customer the balance the file gives (limit times utilization) is not the same as a month's spend, so the open cycle's spend differs from the closed cycles by up to about 30% either way (c_004 up 29%, c_009 down 28%). A spend-trend tool needs a threshold wider than that before it calls a change unusual.

The file puts c_006's change of paying account in the signal window but also says the last two of the last three repayments use the new account. Those cannot both hold, since the Oct 5 repayment is before the window. The seed follows the second (`data_integrity`): the change is on 2026-09-20.
