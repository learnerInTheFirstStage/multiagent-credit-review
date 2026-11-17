"""Evidence id helpers.

Convention agreed with Vincent (PR#4 Q3):

- Row-level ids come straight from the row. linked_accounts.id is already
  `acct_c002_a`; bank_transactions.id becomes `txn_{id}`; the daily_balances
  composite key becomes `bal_{account_id}_{YYYYMMDD}`.
- Aggregate ids use `agg_<4-hex>`. The hash is a deterministic MD5 over
  `tool_name + customer_id + start + end + extra_params`, so two calls with
  the same inputs produce the same id, as the report check needs
  (plain-string compare).

The `kind` field is a free string per PRD, but we keep a short controlled
vocabulary here so the model's prompt can rely on it.
"""

import hashlib
from datetime import date


def txn_evidence_id(row_id: int) -> str:
    return f"txn_{row_id}"


def account_evidence_id(account_id: str) -> str:
    # Account ids in the yaml are already prefixed as acct_*; keep them as-is.
    return account_id


def balance_evidence_id(account_id: str, day: date) -> str:
    return f"bal_{account_id}_{day.strftime('%Y%m%d')}"


def aggregate_evidence_id(tool_name: str, customer_id: str, *parts: str) -> str:
    """Deterministic short hash. parts can be dates, counts, categories, etc."""
    canonical = "|".join([tool_name, customer_id, *parts])
    digest = hashlib.md5(canonical.encode()).hexdigest()
    return f"agg_{digest[:4]}"


# Controlled vocabulary for Evidence.kind on the Bank side.
BANK_TRANSACTION = "bank_transaction"
DAILY_BALANCE = "daily_balance"
LINKED_ACCOUNT = "linked_account"
CASH_SUMMARY = "cash_summary"
WEEKLY_INFLOW_SERIES = "weekly_inflow_series"
PAYEE_HISTORY = "payee_history"
ACCOUNT_REPORTING_STATUS = "account_reporting_status"
BALANCE_VS_TXN_RECONCILIATION = "balance_vs_txn_reconciliation"
PRIOR_YEAR_COMPARISON = "prior_year_comparison"
