"""Models shared by the services.

One module per contract, re-exported here, so that people adding different
contracts do not edit the same file. The business contracts (the specialist
request and response, the analysis tools, the signal, the report) are added
once agreed. See KAN-13 and KAN-14.
"""

from contracts.health import Health
from contracts.signal import Signal

__all__ = ["Health", "Signal"]
