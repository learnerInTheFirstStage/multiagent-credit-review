"""A Bank or Card that gives nothing at all is never an empty answer, and never "no answer" either.

Seen live: a wrong JWT secret made every Bank and Card call answer 401, each agent returned a valid empty
answer, the case recorded "answered", and all 26 golden cases ended as a G-11 hand-over with nothing logged.
Then such a question was recorded as no answer, and the case still ended under G-11. Now the case stops before
that question (errors.CaseWaiting) and the runner makes it wait (tests/test_outage_wait.py). Each test fails on
the code before the fix. No model: the fixed plan and the code's gap rules ask.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app import guidelines as service_guidelines
from app.agents import mock
from app.agents.card_service import card_service
from app.agents.cash_flow import cash_flow
from app.agents import errors
from app.agents.errors import Unanswered
from app.graph import build_graph, facts, needs, plans
from contracts import InvestigateRequest
from tests._bank_standin import BankStandin
from tests._card_service_helpers import CardStub
from tests.test_graph import CONFIG, FROM_THE_FACTS, Run, report, scripted, signal_of

REFUSED = {"detail": {"error": "invalid_token", "message": "Signature verification failed"}}


def refusing(status: int = 401, body: dict | None = None) -> httpx.Client:
    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=REFUSED if body is None else body)
    return httpx.Client(transport=httpx.MockTransport(answer))


def unreachable() -> httpx.Client:
    def answer(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)
    return httpx.Client(transport=httpx.MockTransport(answer))


def timing_out() -> httpx.Client:
    def answer(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)
    return httpx.Client(transport=httpx.MockTransport(answer))


def ask(topic: str | None, customer_id: str = "c_010", **focus) -> InvestigateRequest:
    return InvestigateRequest(case_id="case_test", customer_id=customer_id, question="What does it show?",
                              window_start="2026-11-03", window_end="2026-11-10", topic=topic, **focus)


def run_case(customer_id: str, bank_http, card_http) -> Run:
    return Run([report(**FROM_THE_FACTS), report(**FROM_THE_FACTS)], plan=plans.FIXED_PLAN, follow_gaps=True,
               signal=signal_of(customer_id), guidelines=service_guidelines.load(),
               specialists={"cash_flow": lambda r: cash_flow(r, model=None, http=bank_http()),
                            "credit_behavior": lambda r: card_service(r, model=None, http=card_http())})


# --- the agents --------------------------------------------------------------------------------


@pytest.mark.parametrize(("client", "code"), [
    (lambda: refusing(401), "http_401"), (lambda: refusing(403), "http_403"), (lambda: refusing(503), "http_503"),
    (lambda: refusing(404, {"detail": {"error": "not_found", "message": "Unknown customer c_010"}}),
     "unknown_customer"),
    # A path the service does not have (a wrong BANK_URL or CARD_URL): FastAPI's own 404.
    (lambda: refusing(404, {"detail": "Not Found"}), "http_404"),
    (unreachable, "unreachable"), (timing_out, "timeout"),
])
@pytest.mark.parametrize("agent", ["cash_flow", "card_service"])
def test_an_agent_whose_every_call_failed_answers_unanswered_with_the_codes(agent, client, code):
    call = cash_flow if agent == "cash_flow" else card_service
    response = call(ask(None), model=None, http=client())
    assert isinstance(response, Unanswered)
    assert code in response.reason
    # Codes only: nothing the service wrote reaches the Orchestrator's model.
    assert "Signature" not in response.reason and "Unknown customer" not in response.reason
    # To any other caller it is still the plain answer, which says the service did not answer.
    assert response.findings == [] and response.could_not_determine
    assert "_reason" not in response.model_dump()


@pytest.mark.parametrize("agent", ["cash_flow", "card_service"])
def test_a_refused_token_is_logged_as_a_warning(agent, caplog):
    call = cash_flow if agent == "cash_flow" else card_service
    with caplog.at_level(logging.WARNING):
        call(ask(None), model=None, http=refusing(401))
    assert any(r.levelno == logging.WARNING and "http_401" in r.getMessage() and r.name.endswith(".tools")
               for r in caplog.records)


def test_an_empty_answer_bank_gave_is_still_an_answer():
    # c_010 has no record of the same weeks last year: Bank answers, with no evidence.
    response = cash_flow(ask("prior_year"), model=None, http=BankStandin().client())
    assert not isinstance(response, Unanswered) and response.evidence == []


def test_an_unknown_transaction_is_an_answer_about_the_request_not_an_outage():
    bank = BankStandin()
    response = cash_flow(ask("payee_history", record_id="txn_c_010_99"), model=None, http=bank.client())
    assert bank.called() == ["payee_history"]
    assert not isinstance(response, Unanswered)


def test_a_refused_argument_is_not_an_outage():
    bank = BankStandin().when("income_trend", {"detail": [{"msg": "bad"}]}, status_code=422)
    assert not isinstance(cash_flow(ask("income"), model=None, http=bank.client()), Unanswered)


def test_no_call_made_is_not_an_outage():
    # payee_history without record_id: nothing is asked, and the agent says why.
    response = cash_flow(ask("payee_history"), model=None, http=refusing(401))
    assert not isinstance(response, Unanswered)
    assert any("record_id" in reason for reason in response.could_not_determine)


def test_one_call_that_returned_data_is_enough():
    bank = BankStandin().when("transactions", REFUSED, status_code=401)
    response = cash_flow(ask("transactions"), model=None, http=bank.client())
    assert not isinstance(response, Unanswered) and "bal_c_010" in {e.id for e in response.evidence}


def test_card_one_call_that_returned_data_is_enough():
    stub = CardStub("c_010").when("spend_by_cycle", REFUSED, status_code=401)
    response = card_service(ask("card_use"), model=None, http=stub.client())
    assert not isinstance(response, Unanswered) and response.evidence


# --- through the case graph ---------------------------------------------------------------------


def stopped(customer_id: str, bank_http, card_http) -> tuple[dict, errors.CaseWaiting]:
    """The case as far as it got, and what stopped it: the fixed_rules flow on the real agents, which call these
    clients. The graph must not go on past a question whose service gave nothing."""
    graph = build_graph(
        model=scripted([report(**FROM_THE_FACTS), report(**FROM_THE_FACTS)]),
        specialists={"cash_flow": lambda r: cash_flow(r, model=None, http=bank_http()),
                     "credit_behavior": lambda r: card_service(r, model=None, http=card_http())},
        load_guidelines=service_guidelines.load,
        carry_out=lambda action, token: {"applied": True},
        checkpointer=InMemorySaver(),
        plan=plans.FIXED_PLAN,
        follow_gaps=True,
    )
    with pytest.raises(errors.CaseWaiting) as raised:
        graph.invoke({"case_id": "case_test", "signal": signal_of(customer_id)}, CONFIG)
    return graph.get_state(CONFIG).values, raised.value


@pytest.mark.parametrize(("bank", "card", "said", "kind"), [
    (lambda: refusing(401), lambda: refusing(401), "http_401", "configuration"),
    (unreachable, unreachable, "unreachable", "transient"),
    (lambda: refusing(503), lambda: CardStub("c_010").client(), "http_503", "transient"),
])
def test_through_the_graph_an_outage_stops_the_case_before_the_question_with_the_reason(bank, card, said, kind):
    state, outage = stopped("c_010", bank, card)

    # The plan asks Bank first: the case stops at its first question, which is recorded neither way.
    assert (outage.service, outage.codes, outage.kind) == ("Bank Service", [said], kind)
    assert state.get("asked", []) == [] and state.get("answers", []) == []


def test_through_the_graph_an_outage_of_card_stops_the_case_after_what_bank_answered():
    state, outage = stopped("c_010", lambda: BankStandin().client(), lambda: refusing(403))

    assert (outage.service, outage.codes, outage.kind) == ("Card Service", ["http_403"], "configuration")
    assert state["asked"] and {entry["agent"] for entry in state["asked"]} == {"cash_flow"}
    assert all(entry["outcome"] == "answered" for entry in state["asked"])


def test_through_the_graph_a_working_service_is_still_answered():
    run = run_case("c_010", lambda: BankStandin().client(), lambda: CardStub("c_010").client())
    assert all(entry["outcome"] == "answered" for entry in run.state["asked"] if entry["outcome"] != "refused")


def test_through_the_graph_an_outage_is_never_a_g11_hand_over_on_nothing_fits():
    # Before: every question was "no_answer", needs.state() counted each as asked, none_fits held, and the case was
    # handed over under G-11 as if the records showed that no guideline fits. Now the case stops before any report.
    state, _ = stopped("c_010", lambda: refusing(401), lambda: refusing(401))

    assert state.get("report") is None and state.get("check") is None
    assert not any(entry["outcome"] == "no_answer" for entry in state.get("asked", []))
    loaded = service_guidelines.load()
    assert "G-11" not in needs.hand_over(loaded, list(facts.ledger(state).values()), state.get("asked", []))


def test_no_question_carries_an_outage_mark():
    # The mark the evaluation used to read from the path page is gone: the case's own status says it now.
    state, _ = stopped("c_010", lambda: BankStandin().client(), lambda: refusing(401))

    assert state["asked"] and all("unavailable" not in entry for entry in state["asked"])
