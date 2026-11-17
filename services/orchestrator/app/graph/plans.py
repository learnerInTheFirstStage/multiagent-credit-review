"""The questions of the two baselines, where code and not the model chooses what to ask (8.4).

`fixed` asks the plan and nothing more. `fixed_rules` asks the plan, then what the code gaps name.
"""

from app.graph.causes import TOPIC_TESTS

FIXED_PLAN = [   # every topic outside causes.NEEDS_RECORD, bank side first
    {"tool": "AskCashFlow", "args": {"topic": "transactions", "question": "Which payments went out in the window?",
        "separates": ["payroll_or_tax", "planned_vendor_payment", "funds_moved_out_of_view", "unexplained_outflow"]}},
    {"tool": "AskCashFlow", "args": {"topic": "income", "question": "How do weekly inflows compare with before?",
        "separates": ["revenue_loss"]}},
    {"tool": "AskCashFlow", "args": {"topic": "reconciliation",
        "question": "Do balances and transactions agree for the window?",
        "separates": ["insufficient_data", "unexplained_outflow"]}},
    {"tool": "AskCashFlow", "args": {"topic": "accounts", "question": "Are all linked accounts reporting?",
        "separates": ["insufficient_data"]}},
    {"tool": "AskCashFlow", "args": {"topic": "prior_year", "question": "What happened in the same weeks last year?",
        "separates": ["seasonal_dip"]}},
    {"tool": "AskCreditBehavior", "args": {"topic": "repayments", "question": "How were the last repayments made?",
        "separates": ["revenue_loss"]}},
    {"tool": "AskCreditBehavior", "args": {"topic": "paying_account",
        "question": "Did the account that pays the card change?",
        "separates": ["funds_moved_out_of_view"]}},
    {"tool": "AskCreditBehavior", "args": {"topic": "card_use", "question": "How is the card being used?",
        "separates": ["revenue_loss"]}},
]
MAX_FOLLOW_UPS = 4

# The words of a follow-up: the plan's question for its topic. A payee's history is the one topic the plan cannot ask.
_QUESTIONS = {step["args"]["topic"]: step["args"]["question"] for step in FIXED_PLAN} | {
    "payee_history": "How was the payee of this payment paid before?",
}


def follow_up(ask: dict) -> dict:
    """A code gap's `ask`, completed with a question and the topic's TOPIC_TESTS as separates."""
    topic = ask["args"]["topic"]
    return {
        "tool": ask["tool"],
        "args": {**ask["args"], "question": _QUESTIONS[topic], "separates": list(TOPIC_TESTS[topic])},
    }
