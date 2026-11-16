# contracts

The one piece of code the services share: the request and response models for calls between them, and for calls from the graph to a specialist agent.

Today it holds `Health`, the review signal (`signal.py`, API doc section 3) and the specialist request and response (`investigate.py`, API doc section 2). Both are drafts until agreed in KAN-13 and KAN-14. Still to come: the analysis tool requests and responses, the report, the cause and recommendation lists.

Put each contract in its own module (for example `investigate.py`) and re-export it from `__init__.py`. Each service installs this package in its Dockerfile and imports from it:

```python
from contracts import Health
```

A change here affects every service, so all three owners review it.
