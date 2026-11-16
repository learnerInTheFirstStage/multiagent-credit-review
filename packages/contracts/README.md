# contracts

The one piece of code the services share: the request and response models for calls between them, and for calls from the graph to a specialist agent.

Today it holds only `Health`, to prove the wiring. The real contracts (the specialist request and response, the analysis tool requests and responses, the signal, the report, the cause and recommendation lists) are added once agreed in KAN-13 and KAN-14.

Put each contract in its own module (for example `investigate.py`) and re-export it from `__init__.py`. Each service installs this package in its Dockerfile and imports from it:

```python
from contracts import Health
```

A change here affects every service, so all three owners review it.
