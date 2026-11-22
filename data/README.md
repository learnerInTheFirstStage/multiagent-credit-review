# data

The test customers: one source file that both the Bank and the Card data generators read, so the two services tell the same story for every customer.

`shared_customers.yaml` holds the 12 scenario customers (KAN-15). Five things read it today:

- the Bank seeder, at Bank's first start on an empty database (`SHARED_CUSTOMERS_YAML` overrides the path);
- the Card seeder, the same way (`services/card/app/db/seed.py`);
- the Orchestrator's stand-in specialists (`services/orchestrator/app/agents/mock.py`), from the copy in the Orchestrator image;
- `POST /signals`, which refuses a customer this file does not hold with a 422;
- the evaluation (`eval/signals.py`, `eval/describe.py`), which builds each case's signal from it. The accepted answers in `eval/golden_set.yaml` rest on it.

A customer's `signal_window.bank_events` is the bank feed of the review week, every movement of cash in it, as a real feed would list it: the events the story turns on come first and keep their order (Bank and the stand-in number events in file order, so `txn_c_009_01` and the like keep their ids), then the everyday ones, each with a `counterparty` name that only the Bank seeder stores. Every event has a category of `enums.transaction_category`:

- `payroll`, `tax`: a payroll run, a tax payment. The Bank seeder repeats them before the window, at the same amount: a payroll run every two weeks, a tax payment every quarter.
- `vendor_known`, `vendor_new`: a payment to a vendor paid before, or to a payee never paid before.
- `transfer_own_account`, `transfer_other`: money moved to the customer's own account at another bank, or to someone else's.
- `income`: money coming in, such as customer receipts.
- `operating`: a running cost paid every week, such as rent, suppliers, software, utilities and card-processing fees. No guideline reads it: it is never an unexplained outflow, nor a planned vendor payment.
- `other`: anything else going out.

The feed adds up: cash a week ago less cash now is what went out less what came in, and the week's `income` events add up to `baselines.bank.weekly_inflow_cents`. Two customers leave a gap on purpose and name it in `signal_window.intended_gap` (amount and reason): c_005, part of whose cash sits in an account that stopped reporting, and c_012, whose books do not reconcile. `services/bank/tests/test_customer_files.py` checks both files, and the Bank seeder refuses a file that does not add up. `cash_now_cents` sets the engine's limit and the review reasons; `cash_week_ago_cents` follows from the week's events. `baselines.bank.weekly_operating_outflow_cents` is the running costs of a usual week: the Bank seeder generates the weeks before the window from it and the inflow baselines. No tool reports it. Those weeks, with the earlier payroll runs and tax payments and `payee_history_hint`, are every payee's history: a payee of the week that none of them pays (another vendor, a transfer, a new payee) reads as never paid before.

Each of `snapshot.bank.linked_accounts` has an `id`, the key every service and record uses, `active`, `days_stale`, and how a person knows the account, as a bank feed names it: `name` ("Chase Business Checking", a real US bank's name for the account) and `mask`, the last 4 digits of its number, quoted so YAML reads it as text. A name has no digits, `$` or `%` and at most five words, since the Orchestrator writes it into findings a model reads. The masks of one customer differ. Bank stores both, the signal carries both, and the review page and the customer's documents request name the account by them; an account without them is named by its id.

c_003, c_004, c_010 and c_012 once had a `monthly_card_spend_cents` no card ledger can carry on their limit: a statement still owed plus the next cycle's spend comes to about 1.85 months of spend, more than those limits hold. Their spend is now 45% of the limit (as is their twins' in `eval/extra_customers.yaml`), the share Card already seeded them at and the most its seed accepts, so the signal and Card show the same repayments.

The evaluation's own 15 customers live in `eval/extra_customers.yaml`, not here. A change to this file needs all three owners, then `docker compose down -v` and `docker compose up --build`, so the images take the new file and Bank and Card seed again.
