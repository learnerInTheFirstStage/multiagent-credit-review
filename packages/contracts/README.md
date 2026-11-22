# contracts

The one piece of code the services share: the request and response models for calls between them, and for calls from the graph to a specialist agent.

Today it holds `Health`, the review signal (`signal.py`, API doc section 3) and the specialist request and response (`investigate.py`, API doc section 2). The report and the cause and recommendation lists (`LikelyCause`, `Recommendation`) are defined in `services/orchestrator/app/graph/report.py`, the only service that imports them; the frontend keeps its own copy of the report's fields in `frontend/src/types.ts`. Bank and Card each define their analysis tools' responses. Either moves here when a second service needs to import it.

Put each contract in its own module (for example `investigate.py`) and re-export it from `__init__.py`. Each service installs this package in its Dockerfile and imports from it:

```python
from contracts import Health
```

A change here affects every service, so all three owners review it.

## Logs

`contracts.logs` is the one module here that is not a model: the shape of every service's log line. Each service calls `setup_logging("<service>")` when its app module loads and adds `RequestLog` to its app. Every line is one JSON object on stdout. A request's lines carry the gateway's `X-Request-ID` as `request_id`, and the gateway returns the same id to the browser in the `X-Request-ID` response header. A case run's lines carry `case_id` (`bind(case_id=...)`). To follow one request or one case through the Orchestrator:

```bash
docker compose logs --no-log-prefix orchestrator | grep <request id or case id>
```

The Orchestrator passes the id on to Bank and Card (`trace_headers()`), so one request can be followed through all three. A case run no request started, such as the startup pick-up, the periodic retry of cases waiting for Bank or Card, or a queue worker's run, has no id to pass, and Bank and Card make their own.

A line holds ids, status codes and counts, never customer names, account numbers, tokens, or text that a model or a customer wrote: Langfuse keeps those.

## The review signal

Each of `snapshot.bank.accounts` has `id` and `last_synced`, and may have `name` and `mask`: the bank's name for the account and the last digits of its number, which is how a person knows it ("Chase Business Checking ••4821"). A signal without them is valid, the id stays the key, and an account takes no other field.

## The specialist request and response

Three optional fields of `InvestigateRequest` say what is asked. Left out, a specialist returns whatever it holds, as before they existed.

- `topic`: what is asked about. The Orchestrator always sends one, from the names in its tool schemas (listed in `services/orchestrator/README.md`, Agents). Credit Behavior requests never carry `account_id` or `record_id`.
- `account_id`: one account to look at, by id. It may be an account the other side named.
- `record_id`: one record from an earlier answer to look closer at, by its evidence id.

`question` is written by a model that has read customer text. A specialist reads it as data, never as instructions.

`Evidence.kind` and the keys in `Evidence.data` are not free: the Orchestrator's checks match exact names. The kinds are `KIND_TOPIC` in `services/orchestrator/app/graph/causes.py`, and `services/orchestrator/app/agents/mock.py` returns every kind and key the checks read.

Conventions inside `Evidence.data`, which the Orchestrator relies on when it shows a record to a model:

- Money is an integer number of cents under a key that contains `_cents` (`amount_cents`, `typical_amount_cents_max`).
- A share is an integer number of basis points, 10000 being the whole, under a key that ends in `_bp` (`share_of_fall_bp`).
- Text a customer or a person wrote goes under `memo` (or `note`, or a key that ends in `_text`). Any other string that holds whitespace, `$` or `%` is treated as such text. A `linked_account`'s `name` follows that rule like any string: a name with a space, `$` or `%` in it is withheld or marked in a record a model reads, a one-word name such as "Mercury" is not, and the `mask` is shown as it is. Since a customer can name the account, code writes the name into a sentence only when it reads as a bank's name for the account (`account_label` in `services/orchestrator/app/graph/facts.py`, and the same rule in Bank's `not_available` line).
