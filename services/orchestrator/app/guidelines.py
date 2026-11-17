"""The credit guidelines a report has to follow.

Our own written rules for the POC: Ramp's real credit policy is not public. The
text is tab 3 of "3. Scenario table", where the team can read it. The
Orchestrator owner decides the final wording.
"""

from pydantic import BaseModel, Field

from app.graph.report import Recommendation


class Guideline(BaseModel):
    id: str = Field(pattern=r"^G-[0-9]{2}[a-z]?$")
    title: str = Field(min_length=1)
    # The facts that have to hold for this guideline to apply.
    when: str = Field(min_length=1)
    # What a report that cites this guideline may recommend.
    allows: list[Recommendation] = Field(min_length=1)
    # What else to do or to know. It goes to the model with the rest.
    note: str = ""


GUIDELINES = [
    Guideline(
        id="G-01",
        title="Regular payroll or tax payment",
        when="The fall matches a regular payroll run or a tax payment, and income is steady",
        allows=["keep_limit"],
    ),
    Guideline(
        id="G-02",
        title="Planned large vendor payment",
        when="The fall is a payment to a vendor the customer has paid before, in line with its history",
        allows=["keep_limit"],
    ),
    Guideline(
        id="G-03",
        title="Seasonal dip",
        when="The same fall happened in the same period of an earlier year, and cash recovered",
        allows=["keep_limit"],
    ),
    Guideline(
        id="G-04",
        title="Cash moved to an account we cannot see",
        when="A transfer to the customer's own unlinked account explains the fall, and the business is healthy",
        allows=["keep_limit"],
        note="Ask the customer to link the account.",
    ),
    Guideline(
        id="G-05",
        title="Income no longer visible, repayments continue",
        when="Inflows stop or shrink sharply, but repayments are on time",
        allows=["request_documents"],
    ),
    Guideline(
        id="G-06",
        title="Confirmed revenue decline",
        when="Income is clearly down over several months",
        allows=["reduce_limit", "request_documents"],
    ),
    Guideline(
        id="G-07",
        title="Revenue decline with repayment trouble",
        when="Income is down, and a repayment was late or failed",
        allows=["reduce_limit", "pause_card"],
    ),
    Guideline(
        id="G-08",
        title="Failed repayment with high utilization",
        when="A repayment failed and most of the limit is used",
        allows=["pause_card", "reduce_limit"],
    ),
    Guideline(
        id="G-09",
        title="Payments to new payees with unknown purpose",
        when="A large part of the fall went to payees with no history",
        allows=["request_documents"],
    ),
    Guideline(
        id="G-10",
        title="Missing or conflicting data",
        when="A linked account is not reporting, or balances and transactions disagree",
        allows=["needs_human", "request_documents"],
    ),
    Guideline(
        id="G-11",
        title="Cause cannot be determined",
        when="More than one explanation remains, or none fits",
        allows=["needs_human"],
    ),
    Guideline(
        id="G-12",
        title="Large exposure",
        when="The limit is very large",
        allows=["needs_human"],
        note="The agent prepares evidence only. A person decides.",
    ),
    Guideline(
        id="G-13",
        title="Cash below the minimum",
        when="Linked cash is below the minimum Ramp asks for",
        allows=["pause_card", "reduce_limit"],
    ),
    Guideline(
        id="G-14",
        title="Suspected revenue decline",
        when="Income is lower, but only for a short time, and nothing else looks wrong",
        allows=["request_documents"],
        note="The limit stays while we wait.",
    ),
]


def load() -> list[dict]:
    """Every guideline, as the graph keeps them. A new copy each time.

    With 14 guidelines all of them go into the prompt. If the list grows, a search
    for the ones that apply goes behind this same function.
    """
    return [guideline.model_dump() for guideline in GUIDELINES]
