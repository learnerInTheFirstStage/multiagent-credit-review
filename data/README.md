# data

The test customers: one source file that both the Bank and the Card data generators read, so the two services tell the same story for every customer.

`shared_customers.yaml` holds the 12 scenario customers (KAN-15). Four things read it today:

- the Bank seeder, at Bank's first start on an empty database (`SHARED_CUSTOMERS_YAML` overrides the path);
- the Orchestrator's stand-in specialists (`services/orchestrator/app/agents/mock.py`), from the copy in the Orchestrator image;
- `POST /signals`, which refuses a customer this file does not hold with a 422;
- the evaluation (`eval/signals.py`, `eval/describe.py`), which builds each case's signal from it. The accepted answers in `eval/golden_set.yaml` rest on it.

The evaluation's own 14 customers live in `eval/extra_customers.yaml`, not here. A change to this file needs all three owners, then `docker compose down -v` and `docker compose up --build`, so both images take the new file and Bank seeds again.
