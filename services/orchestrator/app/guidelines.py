"""The credit guidelines a report has to follow.

Our own written rules for the POC: Ramp's real credit policy is not public. The
text starts from tab 3 of "3. Scenario table". The Orchestrator owner decides
the final wording.

Two of the sheet's guidelines are split in two here, because one fact decides
between different actions and issuers act on that fact:

- G-08, a failed repayment. Issuers stop spend until it is paid, and restore it
  once it is. So "since paid" and "still unpaid" are separate guidelines.
- G-10, missing or conflicting data. A feed that stopped is the customer's to
  fix, and issuers give time to reconnect. Records that disagree are for a person.

Each guideline also declares the facts it needs, with the numbers of app/policy.py.
Code checks a report's cited records against them (app/graph/needs.py).
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import policy
from app.graph.causes import KINDS
from app.graph.report import Recommendation

# A value to equal; or {"in": [...]}; or {"gte": n} and/or {"lte": n}.
Test = str | int | bool | dict


class Need(BaseModel):
    """One fact a guideline's condition rests on. Met by records the report cites, checked by code."""

    model_config = ConfigDict(extra="forbid")

    # A kind of record in causes.KINDS, or "none_fits": no guideline that allows an action fits the case.
    kind: str = Field(min_length=1)
    # Field of the record's data -> the test its value must pass. A record without the field does not pass.
    where: dict[str, Test] = {}
    # The shares of the fall of the matching records must add up to this.
    share_at_least_bp: int | None = Field(default=None, ge=1, le=10000)
    # The need in plain words: shown to both models, used in refusals. No figure (a test checks it).
    says: str = Field(min_length=1)

    @field_validator("kind")
    @classmethod
    def _a_kind_someone_makes(cls, kind: str) -> str:
        if kind not in KINDS and kind != "none_fits":
            raise ValueError(f"no record is of the kind {kind}")
        return kind

    @field_validator("where")
    @classmethod
    def _tests_code_knows(cls, where: dict) -> dict:
        for field, test in where.items():
            if not isinstance(test, dict):
                continue
            # An empty test would pass any value: the need would only ask that the field be there.
            if not test:
                raise ValueError(f"{field}: a test needs in, gte or lte")
            if unknown := sorted(set(test) - {"in", "gte", "lte"}):
                raise ValueError(f"{field}: code knows no test {unknown}")
            # A string would match by substring, and a bound that is not a number cannot be compared.
            if "in" in test and not isinstance(test["in"], list):
                raise ValueError(f"{field}: in takes a list")
            if any(type(test[bound]) is not int for bound in ("gte", "lte") if bound in test):
                raise ValueError(f"{field}: gte and lte take a whole number")
        return where


class Guideline(BaseModel):
    id: str = Field(pattern=r"^G-[0-9]{2}[a-z]?$")
    title: str = Field(min_length=1)
    # The facts that have to hold for this guideline to apply.
    when: str = Field(min_length=1)
    # What a report that cites this guideline may recommend.
    allows: list[Recommendation] = Field(min_length=1)
    # What else to do or to know. It goes to the model with the rest.
    note: str = ""
    # What `when` rests on, as records. A report that cites this guideline must cite a record for each.
    needs: list[Need] = Field(min_length=1)


def _built() -> list[Guideline]:
    """Every guideline, with the policy values as they are now: a value set in app.policy after import is used.

    Each call makes its own objects, the needs several guidelines share included: a change to one build never
    reaches the next.
    """
    steady = Need(kind="weekly_inflow_series", where={"income_trend": "steady"}, says="weekly inflows that are steady")
    all_paid = Need(kind="repayment_statuses", where={"all_paid": True}, says="every repayment paid on time")
    return [
        Guideline(
            id="G-01",
            title="Regular payroll or tax payment",
            when="The fall matches a regular payroll run or a tax payment, and income is steady",
            allows=["keep_limit"],
            needs=[
                Need(
                    kind="bank_transaction",
                    where={"category": {"in": ["payroll", "tax"]}},
                    share_at_least_bp=policy.EXPLAINS_BP,
                    says="payroll or tax payments that together make up most of the fall",
                ),
                steady,
            ],
        ),
        Guideline(
            id="G-02",
            title="Planned large vendor payment",
            when="The fall is a payment to a vendor the customer has paid before, in line with its history",
            allows=["keep_limit"],
            needs=[
                Need(
                    kind="bank_transaction",
                    where={"category": "vendor_known"},
                    share_at_least_bp=policy.LARGE_PART_BP,
                    says="a payment to a vendor paid before that is a large part of the fall",
                ),
                Need(
                    kind="payee_history",
                    where={"vs_usual_bp": {"lte": policy.IN_LINE_BP}},
                    says="the history of that payee, with this payment no larger than what it was usually paid",
                ),
            ],
        ),
        Guideline(
            id="G-03",
            title="Seasonal dip",
            when="The same fall happened in the same period of an earlier year, and cash recovered",
            allows=["keep_limit"],
            needs=[
                Need(
                    kind="prior_year_comparison",
                    where={"window_vs_prior_out_bp": {"gte": policy.SAME_FALL_BP[0], "lte": policy.SAME_FALL_BP[1]}},
                    says="last year's record for the same weeks, with a similar amount going out",
                ),
                Need(
                    kind="cash_recovery",
                    where={"now_vs_then_bp": {"gte": policy.RECOVERED_BP}},
                    says="an earlier review from the same season, "
                    "with cash back near the level it had before that fall",
                ),
            ],
        ),
        Guideline(
            id="G-04",
            title="Cash moved to an account we cannot see",
            when="A transfer to the customer's own unlinked account explains the fall, and the business is healthy",
            allows=["keep_limit"],
            note="Ask the customer to link the account.",
            # "healthy" is read as steady inflows and every repayment paid.
            needs=[
                Need(
                    kind="bank_transaction",
                    where={"category": "transfer_own_account"},
                    share_at_least_bp=policy.EXPLAINS_BP,
                    says="a transfer to the customer's own account elsewhere that makes up most of the fall",
                ),
                steady,
                all_paid,
            ],
        ),
        Guideline(
            id="G-05",
            title="Income no longer visible, repayments continue",
            when="Inflows stop or shrink sharply, but repayments are on time",
            allows=["request_documents"],
            needs=[
                Need(
                    kind="weekly_inflow_series",
                    where={
                        "income_trend": {"in": ["stopped", "shrinking"]},
                        "vs_compared_bp": {"lte": policy.SHARP_BP},
                    },
                    says="weekly inflows that stopped or shrank sharply",
                ),
                all_paid,
                # Repayments that go on while income stops are paid from somewhere. Which account pays the card,
                # set against what the linked accounts paid into it (xs_account_match), tells income moved out of
                # view from income lost (c_006 against c_106). Changed or not, the record must be in before G-05
                # can be cited, and while nothing else is permitted the gap pass asks Card for it (G2).
                Need(kind="funding_account_change", says="which account pays the card"),
            ],
        ),
        Guideline(
            id="G-06",
            title="Confirmed revenue decline",
            when="Income is clearly down over several months",
            allows=["reduce_limit", "request_documents"],
            # "several months" rests on the typed label.
            needs=[
                Need(
                    kind="weekly_inflow_series",
                    where={"income_trend": "shrinking"},
                    says="weekly inflows that have been shrinking",
                ),
            ],
        ),
        Guideline(
            id="G-07",
            title="Revenue decline with repayment trouble",
            when="Income is down, and a repayment was late or failed",
            allows=["reduce_limit", "pause_card"],
            needs=[
                Need(
                    kind="weekly_inflow_series",
                    where={"income_trend": {"in": ["shrinking", "stopped"]}},
                    says="weekly inflows that are shrinking or stopped",
                ),
                Need(
                    kind="card_repayment",
                    where={"status": {"in": ["late", "failed"]}},
                    says="a repayment that was late or failed",
                ),
            ],
        ),
        Guideline(
            id="G-08a",
            title="Failed repayment, since paid",
            when="A repayment failed and was paid within days, and income is steady",
            allows=["keep_limit"],
            needs=[
                Need(
                    kind="card_repayment",
                    where={"status": "failed", "paid_since": "yes", "repaid_after_days": {"lte": policy.WITHIN_DAYS}},
                    says="a repayment that failed and was paid within days",
                ),
                steady,
            ],
        ),
        Guideline(
            id="G-08b",
            title="Failed repayment, still unpaid",
            when="A repayment failed and has not been paid",
            allows=["pause_card"],
            note="Spend stops until the repayment is made.",
            needs=[
                Need(
                    kind="card_repayment",
                    where={"status": "failed", "paid_since": {"in": list(policy.UNPAID_MEANS)}},
                    says="a repayment that failed and has not been paid",
                ),
            ],
        ),
        Guideline(
            id="G-09",
            title="Payments to new payees with unknown purpose",
            when="A large part of the fall went to payees with no history",
            allows=["request_documents"],
            needs=[
                Need(
                    kind="bank_transaction",
                    where={"category": "vendor_new"},
                    share_at_least_bp=policy.LARGE_PART_BP,
                    says="payments to payees never paid before that together are a large part of the fall",
                ),
            ],
        ),
        Guideline(
            id="G-10a",
            title="Data gap the customer can fix",
            when="A linked account is not reporting",
            allows=["request_documents"],
            note="Ask the customer to reconnect the account or to upload its statements.",
            needs=[
                Need(kind="linked_account", where={"is_stale": True}, says="a linked account that is not reporting"),
            ],
        ),
        Guideline(
            id="G-10b",
            title="Records disagree",
            when="Balances and transactions do not match",
            allows=["needs_human"],
            needs=[
                Need(
                    kind="balance_reconciliation",
                    where={"reconciled": False},
                    says="a reconciliation that says balances and transactions do not match",
                ),
            ],
        ),
        Guideline(
            id="G-11",
            title="Cause cannot be determined",
            when="More than one explanation remains, or none fits",
            allows=["needs_human"],
            # Decided by code from the whole ledger and from what was asked. "More than one explanation" is not checked.
            needs=[
                Need(
                    kind="none_fits",
                    says="no guideline that allows an action fits the records of the case, "
                    "and none still needs a record nobody asked for",
                ),
            ],
        ),
        Guideline(
            id="G-12",
            title="Large exposure",
            when="The limit is very large",
            allows=["needs_human"],
            note="The agent prepares evidence only. A person decides.",
            # False while policy.LARGE_LIMIT_CENTS is None, so G-12 cannot be cited until the owner sets it.
            needs=[
                Need(
                    kind="limit_change",
                    where={"very_large": True},
                    says="a limit above the line for a very large limit",
                ),
            ],
        ),
        Guideline(
            id="G-13",
            title="Cash below the minimum",
            when="Linked cash is below the minimum Ramp asks for",
            allows=["pause_card", "reduce_limit"],
            # False while policy.MIN_CASH_CENTS is None: closed on purpose until the owner sets it.
            needs=[
                Need(kind="cash_change", where={"below_minimum": True}, says="linked cash below the minimum"),
            ],
        ),
        Guideline(
            id="G-14",
            title="Suspected revenue decline",
            when="Income is lower, but only for a short time, and nothing else looks wrong",
            allows=["request_documents"],
            note="The limit stays while we wait.",
            # "nothing else looks wrong" is read as every repayment paid, with the must-address list for the rest.
            # "only for a short time" is read as no repeat of requests for weak income (decision 3).
            needs=[
                Need(
                    kind="weekly_inflow_series",
                    where={"income_trend": "too_short_to_tell"},
                    says="weekly inflows that were lower for too short a time to call a trend",
                ),
                all_paid,
                Need(
                    kind="review_history",
                    where={"document_requests_for_income_in_window": {"lte": policy.REPEAT_REQUESTS - 1}},
                    says="no repeat of document requests for weak income in the last six months",
                ),
            ],
        ),
    ]


def __getattr__(name: str):
    # GUIDELINES is built when it is read, as load() is: a list built at import would keep the policy of that moment.
    if name == "GUIDELINES":
        return _built()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def load() -> list[dict]:
    """Every guideline, as the graph keeps them, with the policy values of this call. A new copy each time.

    With 16 guidelines all of them go into the prompt. If the list grows, a search
    for the ones that apply goes behind this same function.
    """
    return [guideline.model_dump() for guideline in _built()]
