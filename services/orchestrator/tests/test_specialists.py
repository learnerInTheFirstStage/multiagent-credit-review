import copy

import pytest
from pydantic import ValidationError

from app.agents import mock
from contracts import InvestigateRequest, InvestigateResponse

# The examples in API doc section 2.
REQUEST = {
    "case_id": "case_102",
    "customer_id": "c_002",
    "question": "Where did the money go?",
    "window_start": "2026-11-03",
    "window_end": "2026-11-10",
    "context": "Linked cash fell from $1.2M to $480K in 7 days.",
}
RESPONSE = {
    "agent": "cash_flow",
    "findings": [
        {
            "claim": "$400,000 was transferred on Nov 7 to an unlinked account.",
            "confidence": "high",
            "evidence_ids": ["txn_8841"],
        }
    ],
    "evidence": [
        {
            "id": "txn_8841",
            "kind": "bank_transaction",
            "data": {"date": "2026-11-07", "amount_cents": -40000000, "type": "transfer"},
        }
    ],
    "could_not_determine": ["The balance of the receiving account"],
    "suggested_next": "Check whether inflows changed.",
}


def response_with(**changes) -> dict:
    return {**copy.deepcopy(RESPONSE), **changes}


def test_the_api_doc_examples_are_valid():
    assert InvestigateRequest.model_validate(REQUEST).question == "Where did the money go?"
    assert InvestigateResponse.model_validate(RESPONSE).findings[0].evidence_ids == ["txn_8841"]


def test_a_specialist_that_cannot_answer_says_why():
    cannot = response_with(findings=[], evidence=[], could_not_determine=["No transactions in the window"])

    assert InvestigateResponse.model_validate(cannot).findings == []


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(
            response_with(evidence=[]), id="finding cites evidence that is not in the response"
        ),
        pytest.param(
            response_with(
                findings=[{"claim": "Cash fell.", "confidence": "high", "evidence_ids": []}]
            ),
            id="finding with no evidence",
        ),
        pytest.param(
            response_with(evidence=RESPONSE["evidence"] * 2), id="the same evidence id twice"
        ),
        pytest.param(
            response_with(findings=[], evidence=[], could_not_determine=[]),
            id="no findings and no reason",
        ),
        pytest.param(
            response_with(findings=[], evidence=[], could_not_determine=[""]),
            id="no findings and a blank reason",
        ),
        pytest.param(response_with(agent="orchestrator"), id="unknown agent"),
        pytest.param(
            response_with(
                findings=[{"claim": "Cash fell.", "confidence": 0.9, "evidence_ids": ["txn_8841"]}]
            ),
            id="confidence as a number",
        ),
        pytest.param(response_with(recommendation="reduce_limit"), id="a limit action"),
    ],
)
def test_a_response_that_breaks_the_rules_is_refused(response):
    with pytest.raises(ValidationError):
        InvestigateResponse.model_validate(response)


@pytest.mark.parametrize(
    "request_",
    [
        pytest.param({**REQUEST, "question": ""}, id="empty question"),
        pytest.param({**REQUEST, "customer_id": "002"}, id="customer_id not c_NNN"),
        pytest.param(
            {**REQUEST, "window_start": "2026-11-10", "window_end": "2026-11-03"},
            id="window runs backwards",
        ),
        pytest.param({**REQUEST, "window_end": 1731196800}, id="date as a timestamp"),
        pytest.param({**REQUEST, "focus": "acct_1"}, id="unknown field"),
    ],
)
def test_a_malformed_request_is_refused(request_):
    with pytest.raises(ValidationError):
        InvestigateRequest.model_validate(request_)


def ask(specialist, customer_id: str, question: str) -> InvestigateResponse:
    return specialist(InvestigateRequest.model_validate({**REQUEST, "customer_id": customer_id, "question": question}))


def test_the_stand_ins_answer_as_the_right_agent():
    assert ask(mock.cash_flow, "c_002", "Where did the money go?").agent == "cash_flow"
    assert ask(mock.credit_behavior, "c_002", "Are repayments on time?").agent == "credit_behavior"


def test_a_follow_up_question_gets_a_different_answer():
    first = ask(mock.cash_flow, "c_010", "Where did the money go?")
    follow_up = ask(mock.cash_flow, "c_010", "What was the paying account balance on the due date?")

    assert first.findings != follow_up.findings
    assert follow_up.evidence[0].kind == "daily_balance"


def test_where_the_money_went_is_not_mistaken_for_the_follow_up():
    answer = ask(mock.cash_flow, "c_010", "Why did cash fall before the repayment?")

    assert answer.evidence[0].kind == "bank_transaction"


def test_editing_an_answer_does_not_change_the_next_one():
    first = ask(mock.cash_flow, "c_002", "Where did the money go?")
    first.findings.clear()
    first.evidence[0].data["amount_cents"] = 0

    again = ask(mock.cash_flow, "c_002", "Where did the money go?")

    assert len(again.findings) == 3
    assert again.evidence[0].data["amount_cents"] == -40000000


def test_a_customer_with_no_canned_answer_gets_an_honest_no():
    answer = ask(mock.cash_flow, "c_007", "Where did the money go?")

    assert answer.findings == []
    assert answer.could_not_determine


# Agreed on PR 4: each service owns its id prefixes, so two services never make the same id.
# An aggregate is agg_ plus a short id worked out from the tool's inputs.
ID_PREFIXES = {
    "cash_flow": ("txn_", "acct_", "bal_", "agg_"),
    "credit_behavior": ("rep_", "bal_card_", "agg_"),
}


def test_the_stand_ins_use_the_id_prefixes_of_their_own_service():
    ids = {
        (agent, evidence.id)
        for (agent, _customer), answers in mock._CANNED.items()
        for _keywords, response in answers
        for evidence in response.evidence
    }

    assert len(ids) >= 10  # the stand-ins do carry evidence
    for agent, evidence_id in ids:
        assert evidence_id.startswith(ID_PREFIXES[agent]), f"{agent} made {evidence_id}"
        # bal_card_ also starts with bal_, so the Bank side needs this said separately.
        assert agent == "credit_behavior" or not evidence_id.startswith("bal_card_"), f"{agent} made {evidence_id}"
