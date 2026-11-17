import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from uuid import uuid4

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app import card_client, runner, runtime
from app.agents import mock
from app.card_client import CardError
from app.db import Base, SessionLocal, engine
from app.db.models import Case
from app.graph import build_graph
from app.main import app
from tests.conftest import needs_postgres
from tests.test_graph import GUIDELINES, INVESTIGATION, PAUSE, ask_bank, report, scripted
from tests.test_signals import SIGNAL  # c_010

SECRET = os.environ["JWT_SECRET"]


def bearer(sub="u_analyst", role="analyst", customers=("c_010",), expires_in=300, secret=SECRET, **claims) -> dict:
    """An Authorization header. A claim set to None is left out of the token."""
    payload = {"sub": sub, "role": role, "customers": customers, "exp": int(time.time()) + expires_in, **claims}
    payload = {
        name: list(value) if isinstance(value, tuple) else value
        for name, value in payload.items()
        if value is not None
    }
    return {"Authorization": f"Bearer {jwt.encode(payload, secret, algorithm='HS256')}"}


def analyst() -> dict:
    return bearer()


def manager() -> dict:
    return bearer(sub="u_manager", role="manager")


APPROVE = {"decision": "approve"}
REJECT = {"decision": "reject"}


class FakeCard:
    """Stands where the Card Service will be. Keeps what it was sent."""

    def __init__(self):
        self.calls = []
        self.is_down = False
        self.takes = 0.0

    def __call__(self, action: dict, token: str | None) -> dict:
        time.sleep(self.takes)
        if self.is_down:
            raise CardError("the Card Service did not answer")
        self.calls.append((dict(action), token))
        return {"applied": True}


def graph_on(script, fake_card, checkpointer=None):
    return build_graph(
        model=scripted(script),
        specialists={"cash_flow": mock.cash_flow, "credit_behavior": mock.credit_behavior},
        load_guidelines=lambda: GUIDELINES,
        carry_out=fake_card,
        checkpointer=checkpointer or InMemorySaver(),
    )


def serving(monkeypatch, graph):
    """Make the service start with this graph in place of the one it would build."""

    @contextmanager
    def case_graph():
        yield graph

    monkeypatch.setattr(runtime, "case_graph", case_graph)


class Service:
    def __init__(self, client, fake_card):
        self.client = client
        self.card = fake_card

    def open_case(self, **changes) -> str:
        """Send a signal. The investigation has run by the time this returns."""
        return self.client.post("/signals", json={**SIGNAL, **changes}).json()["case_id"]

    def case(self, case_id: str) -> dict:
        return self.client.get(f"/cases/{case_id}", headers=analyst()).json()

    def decide(self, case_id: str, decision: dict, headers: dict):
        return self.client.post(f"/cases/{case_id}/decision", json=decision, headers=headers)


@pytest.fixture
def start(monkeypatch):
    """Start the service with a scripted model behind it."""
    with ExitStack() as stack:

        def _start(script) -> Service:
            fake_card = FakeCard()
            serving(monkeypatch, graph_on(script, fake_card))
            Base.metadata.drop_all(engine)
            return Service(stack.enter_context(TestClient(app)), fake_card)

        yield _start


def status_in_table(case_id: str) -> str:
    with SessionLocal() as session:
        return session.get(Case, case_id).status


# Running a case


def test_a_new_signal_is_investigated_and_then_waits_for_the_analyst(start):
    service = start([*INVESTIGATION, report()])

    accepted = service.client.post("/signals", json=SIGNAL).json()

    assert accepted["status"] == "queued"  # the answer goes out before the investigation
    case = service.case(accepted["case_id"])
    assert case["status"] == "needs_approval"
    assert case["waiting_for"] == "analyst"
    assert case["report"]["recommendation"] == "reduce_limit"
    assert case["decisions"] == []
    assert case["action"] is None
    assert service.card.calls == []


def test_a_signal_that_joins_or_repeats_starts_no_second_investigation(start, monkeypatch):
    # Counted, because a second run on a case that already waits does nothing you can see.
    # On a case still being investigated it would run the same steps twice.
    runs = []
    run_case = runner.run_case
    monkeypatch.setattr(runner, "run_case", lambda graph, case_id: (runs.append(case_id), run_case(graph, case_id)))
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()

    joined = service.client.post("/signals", json={**SIGNAL, "signal_id": "sig_552"}).json()
    repeated = service.client.post("/signals", json=SIGNAL).json()

    assert (joined["result"], repeated["result"]) == ("joined", "duplicate")
    assert joined["case_id"] == repeated["case_id"] == case_id
    assert runs == [case_id]
    assert service.case(case_id)["status"] == "needs_approval"


def test_a_case_that_breaks_is_marked_failed_and_shows_no_unchecked_report(start):
    # The first draft fails the check, and the model has nothing left for the redraft.
    service = start([*INVESTIGATION, report(guideline_ids=[])])

    case_id = service.open_case()

    case = service.case(case_id)
    assert case["status"] == "failed"
    assert case["report"] is None
    assert case["waiting_for"] is None
    # A failed case is closed, so the customer's next signal opens a new one.
    again = service.client.post("/signals", json={**SIGNAL, "signal_id": "sig_552"}).json()
    assert again["result"] == "opened"
    assert again["case_id"] != case_id


def test_with_no_model_a_signal_is_kept_and_its_case_waits(client):
    accepted = client.post("/signals", json=SIGNAL).json()

    case = client.get(f"/cases/{accepted['case_id']}", headers=analyst())
    assert case.json() == {
        "case_id": accepted["case_id"],
        "customer_id": "c_010",
        "status": "queued",
        "waiting_for": None,
        "report": None,
        "decisions": [],
        "action": None,
    }
    decision = client.post(f"/cases/{accepted['case_id']}/decision", json=APPROVE, headers=analyst())
    assert decision.status_code == 503


# Deciding


def test_an_approval_sends_the_action_to_card_with_the_approvers_own_token(start):
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()
    headers = analyst()

    answer = service.decide(case_id, APPROVE, headers)

    assert answer.status_code == 200
    case = answer.json()
    assert case["status"] == "done"
    assert case["waiting_for"] is None
    assert case["decisions"] == [{"step": "analyst", "decision": "approve", "by": "u_analyst"}]
    assert case["action"]["result"] == {"applied": True}
    [(action, token)] = service.card.calls
    assert action == {
        "type": "reduce_limit",
        "limit_cents": 5800000,
        "case_id": case_id,
        "customer_id": "c_010",
        "approved_by": "u_analyst",
        "approval_id": f"{case_id}:analyst",
    }
    assert f"Bearer {token}" == headers["Authorization"]
    assert status_in_table(case_id) == "done"


def test_a_rejection_closes_the_case_and_sends_nothing(start):
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()

    answer = service.decide(case_id, REJECT, analyst())

    assert answer.json()["status"] == "rejected"
    assert service.card.calls == []
    assert status_in_table(case_id) == "rejected"


def test_a_card_pause_needs_the_manager_and_goes_out_with_the_managers_token(start):
    service = start([*INVESTIGATION, report(**PAUSE)])
    case_id = service.open_case()

    first = service.decide(case_id, APPROVE, analyst())

    assert first.json()["status"] == "needs_manager"
    assert first.json()["waiting_for"] == "manager"
    assert status_in_table(case_id) == "needs_manager"
    assert service.card.calls == []

    # The analyst cannot also give the manager's approval.
    assert service.decide(case_id, APPROVE, analyst()).status_code == 403
    assert service.card.calls == []

    headers = manager()
    second = service.decide(case_id, APPROVE, headers)

    assert second.json()["status"] == "done"
    [(action, token)] = service.card.calls
    assert (action["type"], action["approved_by"], action["approval_id"]) == (
        "pause_card",
        "u_manager",
        f"{case_id}:manager",
    )
    assert f"Bearer {token}" == headers["Authorization"]


def test_a_manager_may_decide_the_analyst_step(start):
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()

    assert service.decide(case_id, APPROVE, manager()).json()["status"] == "done"


def test_one_person_cannot_give_both_approvals(start):
    service = start([*INVESTIGATION, report(**PAUSE)])
    case_id = service.open_case()
    assert service.decide(case_id, APPROVE, manager()).json()["waiting_for"] == "manager"

    for decision in (APPROVE, REJECT):
        assert service.decide(case_id, decision, manager()).status_code == 403
    assert service.card.calls == []
    assert service.case(case_id)["waiting_for"] == "manager"

    second = service.decide(case_id, APPROVE, bearer(sub="u_manager_2", role="manager"))

    assert second.json()["status"] == "done"
    assert [d["by"] for d in second.json()["decisions"]] == ["u_manager", "u_manager_2"]


@pytest.mark.parametrize("body", [{"decision": "maybe"}, {}, {"decision": "approve", "by": "u_someone_else"}])
def test_a_decision_is_approve_or_reject_and_cannot_name_who_decided(start, body):
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()

    assert service.decide(case_id, body, analyst()).status_code == 422
    assert service.case(case_id)["waiting_for"] == "analyst"
    assert service.card.calls == []


def test_a_case_that_is_not_waiting_cannot_be_decided(start):
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()
    service.decide(case_id, APPROVE, analyst())

    again = service.decide(case_id, APPROVE, analyst())

    assert again.status_code == 409
    assert len(service.card.calls) == 1
    assert service.decide("case_nothere", APPROVE, analyst()).status_code == 404


# Who may do it


def unsigned() -> dict:
    payload = {"sub": "u_analyst", "role": "manager", "customers": ["c_010"], "exp": int(time.time()) + 300}
    return {"Authorization": f"Bearer {jwt.encode(payload, None, algorithm='none')}"}


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param({}, 401, id="no token"),
        pytest.param({"Authorization": "Basic dV9hbmFseXN0OnB3"}, 401, id="not a bearer token"),
        pytest.param({"Authorization": "Bearer not.a.token"}, 401, id="not a token"),
        pytest.param(bearer(secret="another-secret-that-is-also-long-enough"), 401, id="signed with another secret"),
        pytest.param(unsigned(), 401, id="not signed at all"),
        pytest.param(bearer(expires_in=-60), 401, id="expired"),
        pytest.param(bearer(exp=None), 401, id="never expires"),
        pytest.param(bearer(sub=None), 401, id="names nobody"),
        pytest.param(bearer(role=None), 401, id="no role"),
        pytest.param(bearer(role="intern"), 401, id="a role that does not exist"),
        pytest.param(bearer(customers=("c_002",)), 403, id="another customer's analyst"),
        pytest.param(bearer(customers=()), 403, id="no customers"),
        pytest.param(bearer(sub="svc", role="service", customers=None, customer_id="c_010"), 403, id="a service token"),
        pytest.param(bearer(role="admin"), 403, id="an admin deciding"),
    ],
)
def test_who_may_not_decide_a_case(start, headers, expected):
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()

    assert service.decide(case_id, APPROVE, headers).status_code == expected
    assert service.card.calls == []
    assert service.case(case_id)["waiting_for"] == "analyst"


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param({}, 401, id="no token"),
        pytest.param(bearer(expires_in=-60), 401, id="expired"),
        pytest.param(bearer(customers=("c_002",)), 403, id="another customer's analyst"),
        pytest.param(bearer(role="admin"), 200, id="an admin with the customer may read"),
    ],
)
def test_who_may_read_a_case(start, headers, expected):
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()

    assert service.client.get(f"/cases/{case_id}", headers=headers).status_code == expected
    assert service.client.get("/cases/case_nothere", headers=analyst()).status_code == 404


def test_without_a_signing_secret_everyone_is_refused(start, monkeypatch):
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()
    headers = analyst()
    monkeypatch.delenv("JWT_SECRET")

    assert service.client.get(f"/cases/{case_id}", headers=headers).status_code == 503
    assert service.decide(case_id, APPROVE, headers).status_code == 503
    assert service.card.calls == []


# When Card does not answer


def test_an_approval_card_did_not_carry_out_is_kept_and_only_its_approver_can_retry(start):
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()
    service.card.is_down = True

    failed = service.decide(case_id, APPROVE, analyst())

    assert failed.status_code == 502
    case = service.case(case_id)
    assert case["status"] == "needs_approval"
    assert case["waiting_for"] == "retry"
    assert case["decisions"] == [{"step": "analyst", "decision": "approve", "by": "u_analyst"}]
    assert case["action"] is None

    service.card.is_down = False
    # Nobody else can ride on that approval, and it cannot be turned into a rejection.
    assert service.decide(case_id, APPROVE, bearer(sub="u_other")).status_code == 409
    assert service.decide(case_id, APPROVE, manager()).status_code == 409
    assert service.decide(case_id, REJECT, analyst()).status_code == 409
    assert service.card.calls == []

    retried = service.decide(case_id, APPROVE, analyst())

    assert retried.json()["status"] == "done"
    assert [action["approval_id"] for action, _ in service.card.calls] == [f"{case_id}:analyst"]
    assert len(retried.json()["decisions"]) == 1


def test_a_manager_can_close_a_case_card_never_confirmed_so_it_does_not_block_the_customer(start):
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()
    service.card.is_down = True
    service.decide(case_id, APPROVE, analyst())
    service.card.is_down = False

    closed = service.decide(case_id, REJECT, manager())

    assert closed.status_code == 200
    case = closed.json()
    # Failed, not rejected: Card may have applied the action and lost its answer.
    assert case["status"] == "failed"
    assert case["waiting_for"] is None
    assert case["action"] is None
    assert case["decisions"] == [
        {"step": "analyst", "decision": "approve", "by": "u_analyst"},
        {"step": "retry", "decision": "reject", "by": "u_manager"},
    ]
    assert case["report"]["recommendation"] == "reduce_limit"  # the record of what was approved stays
    assert status_in_table(case_id) == "failed"
    # Closed for good: the approval cannot be sent after all.
    assert service.decide(case_id, APPROVE, analyst()).status_code == 409
    assert service.card.calls == []
    again = service.client.post("/signals", json={**SIGNAL, "signal_id": "sig_552"}).json()
    assert again["result"] == "opened"


def card_answers(monkeypatch, answer):
    sent = []

    def post(url, **kwargs):
        sent.append({"url": url, **kwargs})
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(*answer[:1], **answer[1], request=httpx.Request("POST", url))

    monkeypatch.setattr(card_client.httpx, "post", post)
    return sent


ACTION = {"type": "pause_card", "customer_id": "c_010", "case_id": "case_1", "approval_id": "case_1:manager"}


def test_the_card_call_carries_the_action_and_the_token(monkeypatch):
    monkeypatch.setenv("CARD_URL", "http://card.test")
    sent = card_answers(monkeypatch, (200, {"json": {"applied": True}}))

    assert card_client.carry_out(ACTION, "the-token") == {"applied": True}
    assert sent[0]["url"] == "http://card.test/customers/c_010/actions"
    assert sent[0]["json"] == ACTION
    assert sent[0]["headers"] == {"Authorization": "Bearer the-token"}


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param((500, {"json": {"error": "boom"}}), id="Card fails"),
        pytest.param((403, {"json": {"error": "forbidden"}}), id="Card refuses"),
        pytest.param((200, {"text": "<html>gateway</html>"}), id="the answer is not JSON"),
        pytest.param(httpx.ConnectError("no route to card"), id="Card cannot be reached"),
        pytest.param(httpx.ReadTimeout("card took too long"), id="Card is too slow"),
    ],
)
def test_any_answer_but_a_clear_yes_from_card_is_a_card_error(monkeypatch, answer):
    card_answers(monkeypatch, answer)

    with pytest.raises(CardError):
        card_client.carry_out(ACTION, "the-token")


@pytest.mark.parametrize("token", [None, ""])
def test_no_action_goes_to_card_without_an_approver_token(monkeypatch, token):
    sent = card_answers(monkeypatch, (200, {"json": {"applied": True}}))

    with pytest.raises(CardError):
        card_client.carry_out(ACTION, token)
    assert sent == []


# After a restart


def queued_case(client) -> str:
    """A case nobody has started: its signal arrived while no model was configured."""
    return client.post("/signals", json=SIGNAL).json()["case_id"]


def test_a_queued_case_is_run_when_it_is_picked_up(client):
    case_id = queued_case(client)
    graph = graph_on([*INVESTIGATION, report()], FakeCard())
    assert runner.unfinished() == [case_id]

    runner.run_case(graph, case_id)

    assert status_in_table(case_id) == "needs_approval"
    assert graph.get_state(runner.thread(case_id)).values["signal"] == SIGNAL


def test_a_case_stopped_part_way_goes_on_from_where_it_stopped(client):
    case_id = queued_case(client)
    first_question, *rest = INVESTIGATION
    # The first process asked one question and died. The second has only the rest of the script.
    before = graph_on([first_question], FakeCard(), checkpointer := InMemorySaver())
    before.invoke({"case_id": case_id, "signal": SIGNAL}, runner.thread(case_id), interrupt_after=["ask_specialist"])
    runner._set_status(case_id, "investigating")
    after = graph_on([*rest, report()], FakeCard(), checkpointer)

    runner.run_case(after, case_id)

    state = after.get_state(runner.thread(case_id)).values
    assert status_in_table(case_id) == "needs_approval"
    assert [answer["question"] for answer in state["answers"]] == [
        "Where did the money go?",
        "Did any repayment fail?",
        "What was the paying account balance on the due date?",
    ]


def test_a_case_the_table_is_behind_on_is_caught_up_without_running_anything(client):
    case_id = queued_case(client)
    graph = graph_on([*INVESTIGATION, report()], FakeCard())
    graph.invoke({"case_id": case_id, "signal": SIGNAL}, runner.thread(case_id))
    assert status_in_table(case_id) == "queued"  # the process died before it could write the status

    runner.run_case(graph, case_id)  # the script is used up: a second run would fail the case

    assert status_in_table(case_id) == "needs_approval"


def test_a_decision_the_table_missed_is_caught_up_at_the_next_start(client):
    # The decision went through in the graph, then writing the status failed.
    case_id = queued_case(client)
    graph = graph_on([*INVESTIGATION, report()], FakeCard())
    runner.run_case(graph, case_id)
    graph.invoke(Command(resume={"decision": "reject", "by": "u_analyst"}), runner.thread(case_id))
    assert status_in_table(case_id) == "needs_approval"

    assert runner.unfinished() == [case_id]
    runner.run_case(graph, case_id)

    assert status_in_table(case_id) == "rejected"
    assert runner.unfinished() == []


def test_a_decision_made_the_moment_a_run_pauses_is_not_overwritten_by_the_run(client):
    case_id = queued_case(client)
    graph = graph_on([*INVESTIGATION, report()], FakeCard())

    class DecidedInTheGap:
        """The graph, with a rejection landing after the run returns and before its status is written."""

        get_state = graph.get_state

        def invoke(self, graph_input, config):
            paused = graph.invoke(graph_input, config)
            graph.invoke(Command(resume={"decision": "reject", "by": "u_analyst"}), config)
            runner._set_status(case_id, "rejected")
            return paused  # still says needs_approval

    runner.run_case(DecidedInTheGap(), case_id)

    assert status_in_table(case_id) == "rejected"


def test_an_approved_action_is_not_sent_by_a_pick_up(client):
    # Approved, Card was down, then a restart. Only the approver's own request may send the action.
    case_id = queued_case(client)
    fake_card = FakeCard()
    graph = graph_on([*INVESTIGATION, report()], fake_card)
    runner.run_case(graph, case_id)
    fake_card.is_down = True
    with pytest.raises(CardError):
        graph.invoke(Command(resume={"decision": "approve", "by": "u_analyst"}), runner.thread(case_id))
    fake_card.is_down = False

    runner.run_case(graph, case_id)

    assert fake_card.calls == []
    assert status_in_table(case_id) == "needs_approval"


def test_starting_the_service_picks_up_what_the_last_run_left(monkeypatch):
    Base.metadata.drop_all(engine)
    with TestClient(app) as before:  # no model yet, so the service stops with the case still queued
        case_id = queued_case(before)
    serving(monkeypatch, graph_on([*INVESTIGATION, report()], FakeCard()))

    with TestClient(app):
        pass  # stopping waits for the cases that were picked up

    assert status_in_table(case_id) == "needs_approval"


# Only on Postgres


@needs_postgres
def test_a_paused_case_survives_a_restart_and_resumes_on_a_new_connection():
    case_id = f"case_{uuid4().hex[:8]}"
    fake_card = FakeCard()
    with runtime.postgres_checkpointer() as checkpointer:
        graph = graph_on([*INVESTIGATION, report()], fake_card, checkpointer)
        graph.invoke({"case_id": case_id, "signal": SIGNAL}, runner.thread(case_id))

    with runtime.postgres_checkpointer() as checkpointer:  # a new pool, as after a restart
        graph = graph_on([], fake_card, checkpointer)  # the model must not be needed again
        snapshot = graph.get_state(runner.thread(case_id))
        assert runner.waiting_for(snapshot) == "analyst"
        assert snapshot.values["report"]["recommendation"] == "reduce_limit"

        state = graph.invoke(
            Command(resume={"decision": "approve", "by": "u_analyst"}),
            runner.thread(case_id),
            context={"approver_token": "the-token"},
        )

    assert state["status"] == "done"
    assert [token for _, token in fake_card.calls] == ["the-token"]


@needs_postgres
def test_an_approval_sent_twice_at_the_same_moment_sends_one_action(start):
    # The same analyst, twice: a double click. Without the lock the second request finds an
    # approval of its own that Card has not answered yet, and sends the action again.
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()
    service.card.takes = 0.5  # long enough for the second request to arrive while the first holds the case

    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda headers: service.decide(case_id, APPROVE, headers), [analyst(), analyst()]))

    assert sorted(answer.status_code for answer in answers) == [200, 409]
    assert len(service.card.calls) == 1
    assert status_in_table(case_id) == "done"
