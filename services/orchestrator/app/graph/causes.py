"""Causes, topics and the records each one rests on. Plain data: the check and the gap rules read it."""

CAUSES = ("payroll_or_tax", "planned_vendor_payment", "seasonal_dip", "funds_moved_out_of_view",
          "revenue_loss", "unexplained_outflow", "insufficient_data")

# The topics of each tool, in catalogue order.
TOPICS = {
    "AskCashFlow": ("transactions", "income", "payee_history", "accounts", "reconciliation", "prior_year"),
    "AskCreditBehavior": ("repayments", "paying_account", "card_use"),
}
# Topics that need the id of one record from an earlier answer.
NEEDS_RECORD = {("AskCashFlow", "payee_history")}

# Where a kind of record comes from.
KIND_TOPIC = {
    "daily_balance": ("AskCashFlow", "transactions"),
    "bank_transaction": ("AskCashFlow", "transactions"),
    "payment_summary": ("AskCashFlow", "transactions"),
    "balance_reconciliation": ("AskCashFlow", "reconciliation"),
    "weekly_inflow_series": ("AskCashFlow", "income"),
    "payee_history": ("AskCashFlow", "payee_history"),
    "linked_account": ("AskCashFlow", "accounts"),
    "prior_year_comparison": ("AskCashFlow", "prior_year"),
    "card_repayment": ("AskCreditBehavior", "repayments"),
    "funding_account_change": ("AskCreditBehavior", "paying_account"),
    "card_balance": ("AskCreditBehavior", "card_use"),
    "card_spend_series": ("AskCreditBehavior", "card_use"),
}
# Kinds code gives when the case opens or works out itself. Nobody has to ask for them.
OWN_KINDS = ("cash_change", "limit_change", "card_position", "repayment_statuses", "account_sync",
             "review_history", "earlier_review", "cash_recovery", "account_match")
KINDS = (*KIND_TOPIC, *OWN_KINDS)

# The causes an answer on a topic can bear on.
TOPIC_TESTS = {
    "transactions": ["payroll_or_tax", "planned_vendor_payment", "funds_moved_out_of_view", "unexplained_outflow"],
    "income": ["revenue_loss"],
    "payee_history": ["planned_vendor_payment", "unexplained_outflow"],
    "accounts": ["insufficient_data", "funds_moved_out_of_view"],
    "reconciliation": ["insufficient_data", "unexplained_outflow"],
    "prior_year": ["seasonal_dip"],
    "repayments": ["revenue_loss"],
    "paying_account": ["funds_moved_out_of_view"],
    "card_use": ["revenue_loss"],
}

# The record a likely cause rests on. Any one of the listed needs is enough. no_clear_cause claims nothing.
CAUSE_NEEDS = {
    "payroll_or_tax": [
        {"kind": "bank_transaction", "where": {"category": {"in": ["payroll", "tax"]}},
         "says": "a payroll or tax payment"},
    ],
    "planned_vendor_payment": [
        {"kind": "bank_transaction", "where": {"category": "vendor_known"}, "says": "a payment to a vendor paid before"},
    ],
    # This year's record only. An earlier review says what was decided then: it carries no cause (7.3).
    "seasonal_dip": [
        {"kind": "prior_year_comparison", "says": "last year's record for the same weeks"},
    ],
    "funds_moved_out_of_view": [
        {"kind": "bank_transaction", "where": {"category": "transfer_own_account"},
         "says": "a transfer to the customer's own account elsewhere"},
        {"kind": "funding_account_change", "where": {"changed": True}, "says": "a change of the account that pays the card"},
    ],
    "revenue_loss": [
        {"kind": "weekly_inflow_series", "where": {"income_trend": {"in": ["shrinking", "stopped", "too_short_to_tell"]}},
         "says": "weekly inflows that are down"},
    ],
    "unexplained_outflow": [
        {"kind": "bank_transaction", "where": {"category": {"in": ["vendor_new", "transfer_other", "other"]}},
         "says": "a payment with no known purpose"},
    ],
    "insufficient_data": [
        {"kind": "linked_account", "where": {"is_stale": True}, "says": "a linked account that is not reporting"},
        {"kind": "balance_reconciliation", "where": {"reconciled": False},
         "says": "a reconciliation that says balances and transactions do not match"},
    ],
}

# What a report that acts may not pass over. "trigger" is a need over the ledger.
# A cited guideline must have a need of the kind in "covered_by".
# An entry applies only when some loaded guideline has a need of its "covered_by" kind.
MUST_ADDRESS = [
    {"trigger": {"kind": "repayment_statuses", "where": {"any_failed": True}},
     "covered_by": "card_repayment", "says": "A repayment failed"},
    {"trigger": {"kind": "account_sync", "where": {"any_behind": True}},
     "covered_by": "linked_account", "says": "A linked account is behind"},
    {"trigger": {"kind": "balance_reconciliation", "where": {"reconciled": False}},
     "covered_by": "balance_reconciliation", "says": "Balances and transactions do not match"},
]
