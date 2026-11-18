"""One table of phrasings both specialists' action filters must agree on (cash_flow and card_service
guardrails.recommends_an_action). A specialist measures; it never says what to do with the limit or the card.
"""

# A limit or card action, however it is worded: both filters refuse every one.
REFUSED = [
    # Words joined by underscores: a model can write an order shaped like an id into could_not_determine.
    "Reduce_the_credit_limit_to_500_now",
    "Pause_the_card",
    # Seen passing the Card filter after "suggests" was let through as a reading.
    "Spend suggests reducing the limit.",
    "Recent spend suggests lowering the card limit.",
    "It suggests pausing the card.",
    # Seen passing after "must have" was let through as an inference: here it is an order.
    "The customer must have their limit reduced.",
    "The card must have its limit cut.",
    # The subjunctive after "suggests": passed both filters.
    "Spend suggests that the card limit be reduced.",
    "Spend suggests the card limit be reduced.",
    "It suggests the card be paused.",
    # After "suggests" and a rise or fall, a limit named within reach is its object; a card or an account
    # is, unless it only qualifies what rises or falls.
    "Spend suggests reducing the customer's credit limit.",
    "Spend suggests cutting the limit sharply.",
    "Spend suggests reducing exposure by lowering the limit.",
    "Spend suggests reducing risk with a limit reduction.",
    "Spend suggests reducing the in-store card limit.",
    "Spend suggests reducing Northwind Trading Company's credit limit.",
    # Pausing, freezing or closing is only ever an action, whatever the card qualifies.
    "It suggests pausing card spending.",
    # Already refused by both.
    "Lowering the credit limit is warranted.",
    "Reducing the limit would be prudent.",
    "The card ought to be frozen until the repayment clears.",
    "We recommend reducing the credit limit.",
    "The limit should be lowered.",
    "Analyst should reduce_limit to $50,000 now.",
]

# Readings and facts: both filters let every one through.
ALLOWED = [
    "The repayment must have been paid by hand.",
    "The repayment must have been made after the card limit was lowered.",
    "The pattern suggests that the spend rose in October.",
    "Whether spend suggests the customer is increasingly relying on the card.",
    # A rise or fall of something the card, the limit or the account only qualifies: refused by the Card
    # filter, which took any card, limit or account within 40 characters of the gerund for its object.
    "Utilization data suggests increasing card usage over the last three cycles.",
    "The spend series suggests increasing reliance on the card.",
    "Card spend suggests decreasing activity on the account since September.",
    "The data suggests decreasing card balances.",
    "Whether the balance is close to the card limit for long.",
    "The limit is $100,000.",
    "The card balance was $92,000 against the $100,000 limit.",
    "Linked cash fell by $230,000 in the window.",
]
