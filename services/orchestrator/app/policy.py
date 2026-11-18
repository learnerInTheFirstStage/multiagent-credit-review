"""The numbers and readings the credit guidelines rest on. The Orchestrator owner decides them."""

# 2026-11-17: when Bank or Card gives nothing, the case waits and asks again (OUTAGE_RETRY_SECONDS), then fails with
# the reason, instead of going to a person under G-11.
POLICY_VERSION = "2026-11-17"

EXPLAINS_BP = 5000        # "explains the fall", "the fall matches": at least half of it (G-01, G-04)
LARGE_PART_BP = 2500      # "a large part of the fall", "the fall is a payment to": at least a quarter (G-02, G-09)
SHARP_BP = 2500           # "shrink sharply": weekly inflows at or under a quarter of the level they are compared with (G-05)
IN_LINE_BP = 10000        # "in line with its history": not more than the most the payee was usually paid (G-02)
WITHIN_DAYS = 5           # "paid within days" (G-08a)
SAME_FALL_BP = (5000, 15000)   # "the same fall": this window's listed payments against what went out in last year's record (G-03)
RECOVERED_BP = 9000       # "cash recovered": cash before this fall against cash before the earlier one (G-03)
SEASON_DAYS = 31          # "the same period of an earlier year": a whole number of years ago, give or take this
REPEAT_WINDOW_DAYS = 183  # "the last six months"
REPEAT_REQUESTS = 2       # this many document requests in the window is "not only for a short time" (G-14)
REPEAT_CAUSES = ("revenue_loss",)   # an earlier request counts toward that repeat only when its review gave one of these causes
LARGE_LIMIT_CENTS = None  # "the limit is very large" (G-12). No number yet, so G-12 cannot be cited
MIN_CASH_CENTS = None     # "the minimum Ramp asks for" (G-13). No number yet, so G-13 cannot be cited

# The values of card_repayment.paid_since that count as "has not been paid" (G-08b).
# "not_recorded" is the reading the owner has to rule on: the team's data does not say for c_010.
UNPAID_MEANS = ("no", "not_recorded")

# When Bank or Card gives nothing at all, the case waits and asks again: this many seconds after the first attempt,
# then after the second, and so on. The attempt after the last one hands the case to a person as failed (app.runner).
OUTAGE_RETRY_SECONDS = (60, 300, 900)

# Readings of the policy that the owner has not ruled on. Each can be switched off on its own.
RULES = {
    "every_cited_guideline_allows": True,   # 2.A rule 5
    "hand_over_cites_a_guideline": True,    # 2.A rule 6
    "none_fits_is_checked": True,           # 2.A, G-11
    "must_address": True,                   # 2.A rule 8, and gap rule G1
}
