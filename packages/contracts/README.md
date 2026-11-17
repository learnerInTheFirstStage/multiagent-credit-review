# contracts

The one piece of code the services share: the request and response models for calls between them, and for calls from the graph to a specialist agent.

Today it holds `Health`, the review signal (`signal.py`, API doc section 3) and the specialist request and response (`investigate.py`, API doc section 2). Both are drafts until agreed in KAN-13 and KAN-14. Still to come: the analysis tool requests and responses, the report, the cause and recommendation lists. The report and the two lists (`LikelyCause`, `Recommendation`) exist as drafts in `services/orchestrator/app/graph/report.py` and move here once agreed.

Put each contract in its own module (for example `investigate.py`) and re-export it from `__init__.py`. Each service installs this package in its Dockerfile and imports from it:

```python
from contracts import Health
```

A change here affects every service, so all three owners review it.

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
- Text a customer or a person wrote goes under `memo` (or `note`, or a key that ends in `_text`). Any other string that holds whitespace, `$` or `%` is treated as such text.
