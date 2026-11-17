# Card Service

Owner: James.

Customers, limits, card spend and repayments, served as read-only analysis tools. It also carries out an action after a person approves it. No model runs here.

The Credit Behavior Agent that calls these tools lives in `services/orchestrator/app/agents/credit_behavior`.

Today: `GET /snapshots` (below) and `GET /health`. Every start refuses to run without `JWT_SECRET`, creates four tables and, when the database is empty, seeds the 12 customers from `data/shared_customers.yaml` (`SHARED_CUSTOMERS_YAML` overrides the path). After changing that file, run `docker compose down -v` and `docker compose up --build`, since the file is copied into the image. To seed an empty database by hand: `docker compose exec card python -m app.db.seed`. Tests: `services/card/tests`. Nothing calls Card's data yet: the Orchestrator's stand-in Credit Behavior specialist reads the YAML file directly.

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

## Tables

Money is integer cents. Columns follow what commercial card platforms expose (Ramp's Transaction and Transfer objects, Stripe Issuing, Brex).

| Table | A row is | Notes |
|---|---|---|
| `customers` | a business with a card program | the current limit, `card_status` (`active`, `paused`), `funding_account_id` (the bank account autopay pulls from) and since when, and when the card and payment feeds last reported |
| `credit_limits` | one limit the customer has had | seeded with the initial limit. `approval_id` is unique, so an approved change (KAN-28) applies once |
| `card_transactions` | one authorization and its settlement | `PENDING` → `CLEARED`, or `DECLINED` with the issuer's reason (`AUTHORIZER_BUSINESS_LIMIT`, `SUSPECTED_FRAUD`, ...); merchant descriptor and normalised name; ISO 18245 MCC; spend category; merchant amount and currency (a few hotel stays are in euros); card present; recurring |
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
