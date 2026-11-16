"""Stand-ins for the two specialists until the real ones can be called (KAN-36).

Canned answers for c_002 and c_010, written by hand from the scenario table.
Numbers the table does not give are made up. The real agents read the data
through the analysis tools on Bank and Card.
"""

from contracts import Evidence, Finding, InvestigateRequest, InvestigateResponse

# (agent, customer) -> answers. The first one whose keywords match the question is returned.
# An entry with no keywords matches any question.
_CANNED: dict[tuple[str, str], list[tuple[tuple[str, ...], InvestigateResponse]]] = {
    ("cash_flow", "c_002"): [
        (
            ("income", "inflow", "revenue"),
            InvestigateResponse(
                agent="cash_flow",
                findings=[
                    Finding(
                        claim="Weekly inflows in this window are in line with the 12 weeks before it.",
                        confidence="high",
                        evidence_ids=["series_c002_inflows"],
                    )
                ],
                evidence=[
                    Evidence(
                        id="series_c002_inflows",
                        kind="weekly_inflow_series",
                        data={"weeks": 13, "trend": "flat"},
                    )
                ],
                could_not_determine=[],
            ),
        ),
        (
            (),
            InvestigateResponse(
                agent="cash_flow",
                findings=[
                    Finding(
                        claim="$400,000 was transferred on Nov 7 to an account that is not linked.",
                        confidence="high",
                        evidence_ids=["txn_8841"],
                    ),
                    Finding(
                        claim="A payroll run of $200,000 went out on Nov 5.",
                        confidence="high",
                        evidence_ids=["txn_8802"],
                    ),
                    Finding(
                        claim="$120,000 was paid on Nov 6 to a vendor paid in earlier months.",
                        confidence="high",
                        evidence_ids=["txn_8815"],
                    ),
                ],
                evidence=[
                    Evidence(
                        id="txn_8841",
                        kind="bank_transaction",
                        data={"date": "2026-11-07", "amount_cents": -40000000, "type": "transfer"},
                    ),
                    Evidence(
                        id="txn_8802",
                        kind="bank_transaction",
                        data={"date": "2026-11-05", "amount_cents": -20000000, "type": "payroll"},
                    ),
                    Evidence(
                        id="txn_8815",
                        kind="bank_transaction",
                        data={"date": "2026-11-06", "amount_cents": -12000000, "type": "payment"},
                    ),
                ],
                could_not_determine=["The balance of the receiving account"],
                suggested_next="Check whether inflows changed.",
            ),
        ),
    ],
    ("credit_behavior", "c_002"): [
        (
            (),
            InvestigateResponse(
                agent="credit_behavior",
                findings=[
                    Finding(
                        claim="The last three repayments were paid on time.",
                        confidence="high",
                        evidence_ids=["rep_2101", "rep_2102", "rep_2103"],
                    ),
                    Finding(
                        claim="The card balance is 28% of the limit.",
                        confidence="high",
                        evidence_ids=["bal_c002"],
                    ),
                ],
                evidence=[
                    Evidence(id="rep_2101", kind="card_repayment", data={"due": "2026-09-15", "status": "paid"}),
                    Evidence(id="rep_2102", kind="card_repayment", data={"due": "2026-10-15", "status": "paid"}),
                    Evidence(id="rep_2103", kind="card_repayment", data={"due": "2026-11-05", "status": "paid"}),
                    Evidence(
                        id="bal_c002",
                        kind="card_balance",
                        data={"balance_cents": 6160000, "limit_cents": 22000000},
                    ),
                ],
                could_not_determine=[],
            ),
        )
    ],
    ("cash_flow", "c_010"): [
        (
            ("paying account", "due date"),
            InvestigateResponse(
                agent="cash_flow",
                findings=[
                    Finding(
                        claim="The linked account held $310,000 on Nov 5, more than the $50,000 due.",
                        confidence="high",
                        evidence_ids=["bal_c010_1105"],
                    )
                ],
                evidence=[
                    Evidence(
                        id="bal_c010_1105",
                        kind="daily_balance",
                        data={"date": "2026-11-05", "closing_cents": 31000000},
                    )
                ],
                could_not_determine=["Which account the repayment was drawn from"],
            ),
        ),
        (
            (),
            InvestigateResponse(
                agent="cash_flow",
                findings=[
                    Finding(
                        claim="Payroll and a tax payment account for $220,000 of the $230,000 fall.",
                        confidence="high",
                        evidence_ids=["txn_9120", "txn_9134"],
                    )
                ],
                evidence=[
                    Evidence(
                        id="txn_9120",
                        kind="bank_transaction",
                        data={"date": "2026-11-04", "amount_cents": -15000000, "type": "payroll"},
                    ),
                    Evidence(
                        id="txn_9134",
                        kind="bank_transaction",
                        data={"date": "2026-11-06", "amount_cents": -7000000, "type": "tax"},
                    ),
                ],
                could_not_determine=[],
                suggested_next="Check repayments on the card side.",
            ),
        ),
    ],
    ("credit_behavior", "c_010"): [
        (
            (),
            InvestigateResponse(
                agent="credit_behavior",
                findings=[
                    Finding(
                        claim="The repayment of $50,000 due on Nov 5 failed.",
                        confidence="high",
                        evidence_ids=["rep_3103"],
                    ),
                    Finding(
                        claim="The card balance is 92% of the limit.",
                        confidence="high",
                        evidence_ids=["bal_c010"],
                    ),
                ],
                evidence=[
                    Evidence(
                        id="rep_3103",
                        kind="card_repayment",
                        data={"due": "2026-11-05", "amount_cents": 5000000, "status": "failed"},
                    ),
                    Evidence(
                        id="bal_c010",
                        kind="card_balance",
                        data={"balance_cents": 9200000, "limit_cents": 10000000},
                    ),
                ],
                could_not_determine=["Why the repayment failed"],
                suggested_next="Check the balance of the paying account on the due date.",
            ),
        )
    ],
}


def _canned(agent: str, request: InvestigateRequest) -> InvestigateResponse:
    question = request.question.lower()
    for keywords, response in _CANNED.get((agent, request.customer_id), []):
        if not keywords or any(keyword in question for keyword in keywords):
            # A copy, so a caller that edits the answer does not change the next one.
            return response.model_copy(deep=True)
    return InvestigateResponse(
        agent=agent,
        findings=[],
        evidence=[],
        could_not_determine=[f"There is no canned answer for {request.customer_id}."],
    )


def cash_flow(request: InvestigateRequest) -> InvestigateResponse:
    return _canned("cash_flow", request)


def credit_behavior(request: InvestigateRequest) -> InvestigateResponse:
    return _canned("credit_behavior", request)
