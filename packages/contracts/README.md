# contracts

The one piece of code the services share: the request and response models for calls between them.

Today it holds only `Health`, to prove the wiring. The real contracts (`POST /investigate`, the snapshot, the report, the cause and recommendation lists) are added once agreed in KAN-13 and KAN-14.

Put each contract in its own module (for example `investigate.py`) and re-export it from `__init__.py`. Each service installs this package in its Dockerfile and imports from it:

```python
from contracts import Health
```

A change here affects every service, so all three owners review it.
