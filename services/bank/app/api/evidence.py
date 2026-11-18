"""Evidence ids and kinds: the vocabulary the Orchestrator's rules read.

The kinds and ids are those of the Orchestrator's stand-in Cash Flow specialist
(services/orchestrator/app/agents/mock.py), which the rules in services/orchestrator/app/graph
were written against.

One id, one content: a record's content depends only on the rows behind it and the service
clock, never on the tool or the arguments that returned it.

- A transaction is cited by the ref stored on its row. One of the review window is txn_<customer>_<nn>,
  numbered in the data file's order. An earlier payment seeded from payee history is
  txn_<customer>_p<nn>; the everyday business, payroll runs and tax payments of a week before the window,
  txn_<customer>_w<weeks back>_<nn>;
  of the n-th of last year's weeks, txn_<customer>_y<n>_<nn> (<nn> the window's transaction it recurs from).
- A linked account is cited by its own id (acct_*).
- The rest are about the review window, the as-of date less the window's days to the as-of date:
  bal_<customer> and agg_<customer>_<topic>. A question about other dates gets records of its own,
  with the window's hash appended (bal_<customer>_<hash8>, agg_<customer>_<topic>_<hash8>).
- The history of a payee is agg_<customer>_payee_history when it is about the review window's first
  payment to a vendor paid before; about any other payment, the payment's number is appended
  (agg_<customer>_payee_history_p<nn> for an earlier one).
"""

import hashlib
from datetime import date

BANK_TRANSACTION = "bank_transaction"
DAILY_BALANCE = "daily_balance"
LINKED_ACCOUNT = "linked_account"
PAYMENT_SUMMARY = "payment_summary"
BALANCE_RECONCILIATION = "balance_reconciliation"
WEEKLY_INFLOW_SERIES = "weekly_inflow_series"
PAYEE_HISTORY = "payee_history"
PRIOR_YEAR_COMPARISON = "prior_year_comparison"


def window_suffix(start: date, end: date, canonical: tuple[date, date]) -> str:
    """Nothing for the review window; for any other window, a hash of its dates."""
    if (start, end) == canonical:
        return ""
    return "_" + hashlib.sha256(f"{start.isoformat()}|{end.isoformat()}".encode()).hexdigest()[:8]


def balance_id(customer_id: str, suffix: str = "") -> str:
    return f"bal_{customer_id}{suffix}"


def aggregate_id(customer_id: str, topic: str, suffix: str = "") -> str:
    return f"agg_{customer_id}_{topic}{suffix}"
