import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import jwt
import pytest
import yaml
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import delete, select
from sqlalchemy.orm import object_session

from app import card_client, guidelines, history, policy, runner, runtime
from app.agents import mock
from app.card_client import CardError
from app.db import Base, SessionLocal, engine
from app.db.models import Case, Review
from app.graph import build_graph
from app.graph.build import MAX_REFUSALS
from app.main import app
from tests.conftest import bearer, needs_postgres, sender
from tests.test_graph import DONE, GUIDELINES, INVESTIGATION, PAUSE, ask_bank, ask_card, gaps, report, scripted
from tests.test_signals import SIGNAL  # c_010

SEED_FILE = Path(__file__).resolve().parents[3] / "eval" / "history.yaml"


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

    def send(self, signal: dict):
        return self.client.post("/signals", json=signal, headers=sender())

    def open_case(self, **changes) -> str:
        """Send a signal. The investigation has run by the time this returns."""
        return self.send({**SIGNAL, **changes}).json()["case_id"]

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


@pytest.fixture(autouse=True)
def no_reviews_left():
    # A closed case keeps a review now. One left behind would be an earlier review for a later test's case.
    yield
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def status_in_table(case_id: str) -> str:
    with SessionLocal() as session:
        return session.get(Case, case_id).status


def reviews_of(case_id: str) -> list[Review]:
    with SessionLocal() as session:
        return list(session.scalars(select(Review).where(Review.case_id == case_id)))


def state_of(case_id: str) -> dict:
    """What the graph the service runs on has saved for a case."""
    return app.state.graph.get_state(runner.thread(case_id)).values


def cited(report: dict) -> set[str]:
    parts = ("evidence", "ruled_out", "other_findings", "review_reasons")
    return {evidence_id for part in parts for item in report[part] for evidence_id in item["evidence_ids"]}


# A report that hands the case to a person, as the fixture's guidelines allow.
HANDS_OVER = {
    "likely_cause": "no_clear_cause",
    "recommendation": "needs_human",
    "guideline_ids": ["G-12"],
    "evidence": [],
}
HAND_OVER = report(**HANDS_OVER)


def seed_entry(case_id: str, as_of: str, customer_id: str = "c_010") -> dict:
    """One flat seed entry, as in eval/history.yaml: a review a person approved."""
    return {
        "case_id": case_id,
        "customer_id": customer_id,
        "as_of": as_of,
        "outcome": "done",
        "current_limit_cents": 10000000,
        "engine_limit_cents": 5800000,
        "cash_7d_ago_cents": 52000000,
        "cash_now_cents": 29000000,
        "review_reasons": ["large_cut"],
        "likely_cause": "payroll_or_tax",
        "recommendation": "keep_limit",
        "confidence": "medium",
        "guideline_ids": ["G-01"],
        "claims": ["A payroll run went out in the week."],
        "decisions": [{"step": "analyst", "decision": "approve", "by": "u_seed_analyst"}],
    }


def seed_file(path: Path, entries: list[dict]) -> Path:
    path.write_text(yaml.safe_dump({"schema_version": 1, "reviews": entries}))
    return path


@pytest.fixture
def wired(monkeypatch):
    """Start the service on the graph runtime.case_graph builds, earlier reviews wired in as in production.

    The model is scripted, the checkpointer is in memory and Card is a fake. The guidelines are the
    fixture's: they declare no needs, so the run after the case opens follows the script.
    """

    @contextmanager
    def in_memory():
        yield InMemorySaver()

    monkeypatch.setattr(runtime, "postgres_checkpointer", in_memory)
    monkeypatch.setattr(guidelines, "load", lambda: GUIDELINES)
    with ExitStack() as stack:

        def _start(script) -> Service:
            fake_card = FakeCard()
            monkeypatch.setattr(runtime, "_chat_model", lambda: scripted(script))
            monkeypatch.setattr(card_client, "carry_out", fake_card)
            Base.metadata.drop_all(engine)
            return Service(stack.enter_context(TestClient(app)), fake_card)

        yield _start


# Running a case


def test_a_new_signal_is_investigated_and_then_waits_for_the_analyst(start):
    service = start([*INVESTIGATION, report()])

    accepted = service.send(SIGNAL).json()

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

    joined = service.send({**SIGNAL, "signal_id": "sig_552"}).json()
    repeated = service.send(SIGNAL).json()

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
    again = service.send({**SIGNAL, "signal_id": "sig_552"}).json()
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
    assert decision.json()["error"] == "unavailable"


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
    assert set(failed.json()) == {"error", "message"}
    assert failed.json()["error"] == "card_unavailable"
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
    again = service.send({**SIGNAL, "signal_id": "sig_552"}).json()
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
    return client.post("/signals", json=SIGNAL, headers=sender()).json()["case_id"]


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
        "Is income steady?",
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


# A review kept for every closed case


def _approve(service, case_id):
    assert service.decide(case_id, APPROVE, analyst()).status_code == 200


def _reject(service, case_id):
    assert service.decide(case_id, REJECT, analyst()).status_code == 200


def _manager_rejects(service, case_id):
    assert service.decide(case_id, APPROVE, analyst()).json()["waiting_for"] == "manager"
    assert service.decide(case_id, REJECT, manager()).status_code == 200


def _approve_hand_over(service, case_id):
    assert service.decide(case_id, {**APPROVE, "decided": "request_documents"}, analyst()).status_code == 200


def _close_unconfirmed(service, case_id):
    service.card.is_down = True
    assert service.decide(case_id, APPROVE, analyst()).status_code == 502
    service.card.is_down = False
    assert service.decide(case_id, REJECT, manager()).status_code == 200


@pytest.mark.parametrize(
    ("draft", "close", "closed_as"),
    [
        pytest.param(report(), _approve, "done", id="approved"),
        pytest.param(report(), _reject, "rejected", id="rejected"),
        pytest.param(report(**PAUSE), _manager_rejects, "rejected", id="the manager rejects"),
        pytest.param(HAND_OVER, _approve_hand_over, "done", id="a hand-over approved"),
        pytest.param(report(), _close_unconfirmed, "failed", id="a manager closes what Card never confirmed"),
    ],
)
def test_closing_a_case_keeps_one_review_whichever_path_closes_it(start, draft, close, closed_as):
    service = start([*INVESTIGATION, draft])
    case_id = service.open_case()
    assert reviews_of(case_id) == []  # waiting for a person is not closed

    close(service, case_id)

    case = service.case(case_id)
    [review] = reviews_of(case_id)
    assert status_in_table(case_id) == case["status"] == review.outcome == closed_as
    assert (review.customer_id, review.source, review.as_of) == ("c_010", "case", date(2026, 11, 10))
    assert review.check_passed is True
    assert review.report == case["report"]
    assert review.decisions == case["decisions"]
    assert review.action == case["action"]
    # The cited records are copied from the case's ledger: their ids mean nothing outside it.
    assert {record["id"] for record in review.evidence} == cited(case["report"])
    assert [entry["topic"] for entry in review.asked] == ["transactions", "repayments", "income"]
    # A closed case cannot be decided again, so nothing can write a second review.
    assert service.decide(case_id, APPROVE, manager()).status_code == 409
    assert len(reviews_of(case_id)) == 1


def test_no_review_is_written_while_an_approved_action_waits_for_card(start):
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()
    service.card.is_down = True

    assert service.decide(case_id, APPROVE, analyst()).status_code == 502
    runner.run_case(app.state.graph, case_id)  # a pick-up after a restart
    assert runner.keep_missing_reviews(app.state.graph) == 0

    assert reviews_of(case_id) == []
    assert status_in_table(case_id) == "needs_approval"
    service.card.is_down = False
    assert service.decide(case_id, APPROVE, analyst()).json()["status"] == "done"
    [review] = reviews_of(case_id)
    assert review.action["result"] == {"applied": True}


def test_a_review_the_table_missed_is_written_by_the_catch_up_at_the_next_start(monkeypatch):
    Base.metadata.drop_all(engine)
    graph = graph_on([*INVESTIGATION, report()], FakeCard())
    serving(monkeypatch, graph)
    with TestClient(app) as client:
        case_id = queued_case(client)
        # The rejection went through in the graph. The process died before the table was written.
        graph.invoke(Command(resume={"decision": "reject", "by": "u_analyst"}), runner.thread(case_id))
    assert status_in_table(case_id) == "needs_approval"
    assert reviews_of(case_id) == []

    with TestClient(app):
        pass

    assert status_in_table(case_id) == "rejected"
    [review] = reviews_of(case_id)
    assert review.outcome == "rejected"
    assert review.decisions == [{"step": "analyst", "decision": "reject", "by": "u_analyst"}]


def _caught_up(service, case_id):
    # The rejection went through in the graph. The process died before the table was written.
    app.state.graph.invoke(Command(resume={"decision": "reject", "by": "u_analyst"}), runner.thread(case_id))
    runner.run_case(app.state.graph, case_id)  # what the next start does with it


@pytest.mark.parametrize(
    ("close", "closed_as"),
    [
        pytest.param(_reject, "rejected", id="a decision"),
        pytest.param(_close_unconfirmed, "failed", id="a manager closes what Card never confirmed"),
        pytest.param(_caught_up, "rejected", id="the catch-up of a decision the table missed"),
    ],
)
def test_a_closed_case_gets_its_review_in_the_transaction_that_writes_its_status(start, monkeypatch, close, closed_as):
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()
    record, seen, same = history.record, [], []

    def recording(session, case, state, *, ledger):
        # Read on a connection of its own. Kept, not asserted here: run_case would swallow the assertion.
        seen.append(status_in_table(case.case_id))
        same.append(object_session(case) is session)  # the session that writes the status, not one of its own
        record(session, case, state, ledger=ledger)

    monkeypatch.setattr(history, "record", recording)

    close(service, case_id)

    # Once, and before the closing status was committed: the two commit together, or neither does.
    assert seen == ["needs_approval"]
    assert same == [True]
    assert status_in_table(case_id) == closed_as
    assert [review.outcome for review in reviews_of(case_id)] == [closed_as]


def written_at_start(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "app.runner" and record.getMessage().startswith("Reviews written")
    ]


def test_a_review_that_went_missing_from_a_closed_case_is_written_again_at_the_next_start(monkeypatch, caplog):
    Base.metadata.drop_all(engine)
    serving(monkeypatch, graph_on([*INVESTIGATION, report()], FakeCard()))
    with TestClient(app) as client:
        case_id = queued_case(client)
        note = {**APPROVE, "note": "Cut as the engine says."}
        assert client.post(f"/cases/{case_id}/decision", json=note, headers=analyst()).json()["status"] == "done"
    [kept] = reviews_of(case_id)
    # An insert that failed, or a case closed before reviews were kept.
    with SessionLocal() as session:
        session.execute(delete(Review).where(Review.case_id == case_id))
        session.commit()
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="app.runner"), TestClient(app):
        pass

    [again] = reviews_of(case_id)
    assert written_at_start(caplog) == ["Reviews written for closed cases that had none: 1 of 1"]
    columns = ("outcome", "check_passed", "report", "decisions", "evidence", "asked", "text")
    assert [getattr(again, column) for column in columns] == [getattr(kept, column) for column in columns]
    assert "Cut as the engine says." in again.text
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.runner"), TestClient(app):
        pass
    assert written_at_start(caplog) == ["Reviews written for closed cases that had none: 0 of 0"]


def test_a_case_that_failed_before_its_report_was_checked_gets_no_review_at_start(monkeypatch, caplog):
    Base.metadata.drop_all(engine)
    # The first draft fails the check, and the model has nothing left for the redraft: the run breaks.
    serving(monkeypatch, graph_on([*INVESTIGATION, report(guideline_ids=[])], FakeCard()))
    with TestClient(app) as client:
        case_id = queued_case(client)
    assert status_in_table(case_id) == "failed"
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="app.runner"), TestClient(app):
        pass

    assert reviews_of(case_id) == []
    # It is looked at, and left: its graph still says investigating.
    assert written_at_start(caplog) == ["Reviews written for closed cases that had none: 0 of 1"]
    assert status_in_table(case_id) == "failed"


class Unreadable:
    """The graph, except that the saved state of one case cannot be read."""

    def __init__(self, graph, case_id: str):
        self.graph, self.case_id = graph, case_id

    def get_state(self, config):
        if config["configurable"]["thread_id"] == self.case_id:
            raise ValueError(f"the checkpoint of {self.case_id} cannot be read")
        return self.graph.get_state(config)

    def __getattr__(self, name):
        return getattr(self.graph, name)


def test_a_closed_case_whose_state_cannot_be_read_stops_neither_the_start_nor_the_other_repairs(monkeypatch, caplog):
    Base.metadata.drop_all(engine)
    graph = graph_on([*INVESTIGATION, report()] * 3, FakeCard())
    serving(monkeypatch, graph)
    with TestClient(app) as client:
        service = Service(client, FakeCard())
        closed = []
        for signal_id in ("sig_551", "sig_552"):
            closed.append(service.open_case(signal_id=signal_id))
            _approve(service, closed[-1])
        still_open = service.open_case(signal_id="sig_553")
    with SessionLocal() as session:
        session.execute(delete(Review))
        session.commit()
    unreadable, readable = closed
    serving(monkeypatch, Unreadable(graph, unreadable))
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="app.runner"), TestClient(app) as client:
        assert client.get("/health").status_code == 200

    assert reviews_of(unreadable) == []
    assert [review.outcome for review in reviews_of(readable)] == ["done"]
    # Only the closed cases are looked at: the open one has no review to miss.
    assert written_at_start(caplog) == ["Reviews written for closed cases that had none: 1 of 2"]
    failed = [record for record in caplog.records if record.name == "app.runner" and record.levelno == logging.ERROR]
    assert [record.getMessage() for record in failed] == [f"case {unreadable}: its missing review could not be written"]
    assert status_in_table(still_open) == "needs_approval"


# History when a case opens


def test_a_case_opened_through_the_service_counts_a_twelfth_earlier_review(wired, monkeypatch, tmp_path):
    first = date(2026, 3, 2)
    entries = [seed_entry(f"seed_c_010_{n:02d}", (first + timedelta(weeks=2 * n)).isoformat()) for n in range(12)]
    monkeypatch.setenv("HISTORY_SEED_FILE", str(seed_file(tmp_path / "history.yaml", entries)))
    service = wired([*INVESTIGATION, report()])

    case_id = service.open_case()

    # Only the case's facts: what follows the opening is not this test's business.
    records = {record["id"]: record for record in state_of(case_id)["facts"]}
    summary = records["hist_c_010_summary"]["data"]
    assert (summary["earlier_reviews"], summary["approved_reviews"]) == (12, 12)
    assert summary["last_review_as_of"] == entries[-1]["as_of"]
    # The three newest get a record of their own.
    assert [record_id for record_id in records if record_id.startswith("hist_seed_")] == [
        "hist_seed_c_010_11",
        "hist_seed_c_010_10",
        "hist_seed_c_010_09",
    ]


def test_the_second_run_of_a_customer_on_the_same_day_sees_no_history(wired, monkeypatch, tmp_path):
    # One review from the day before, which counts, beside the first run of the day, which must not.
    seeds = seed_file(tmp_path / "history.yaml", [seed_entry("seed_c_010_a", "2026-11-09")])
    monkeypatch.setenv("HISTORY_SEED_FILE", str(seeds))
    service = wired([*INVESTIGATION, report(), *INVESTIGATION, report()])
    first = service.open_case()
    assert service.decide(first, APPROVE, analyst()).json()["status"] == "done"
    [review] = reviews_of(first)
    assert (review.as_of, review.check_passed, review.outcome) == (date(2026, 11, 10), True, "done")

    second = service.open_case(signal_id="sig_552")

    assert second != first
    records = {record["id"]: record for record in state_of(second)["facts"]}
    assert records["hist_c_010_summary"]["data"]["earlier_reviews"] == 1
    assert "hist_seed_c_010_a" in records
    assert f"hist_{first}" not in records


def test_a_missing_reviews_table_is_an_error_and_not_an_empty_history(wired, caplog):
    service = wired([*INVESTIGATION, report()])
    Review.__table__.drop(engine)

    case_id = service.open_case()

    # Not a first review: the case fails, and the log says why.
    assert status_in_table(case_id) == "failed"
    assert service.case(case_id)["report"] is None
    assert "facts" not in state_of(case_id)
    [failed] = [record for record in caplog.records if record.name == "app.runner" and record.levelno == logging.ERROR]
    assert "reviews" in str(failed.exc_info[1])


# Seeds at start


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        pytest.param(None, ["/eval/history.yaml"], id="unset: the file the image holds"),
        pytest.param("", ["/eval/history.yaml"], id="empty, as compose passes it"),
        pytest.param(":", ["/eval/history.yaml"], id="naming no file"),
        pytest.param("/eval/history.yaml", ["/eval/history.yaml"], id="one file"),
        pytest.param(
            "/eval/history.yaml::/eval/history_eval.yaml:",
            ["/eval/history.yaml", "/eval/history_eval.yaml"],
            id="two files, empty names ignored",
        ),
    ],
)
def test_the_seed_file_setting_lists_its_paths_in_order(monkeypatch, setting, expected):
    monkeypatch.delenv("HISTORY_SEED_FILE", raising=False)
    if setting is not None:
        monkeypatch.setenv("HISTORY_SEED_FILE", setting)

    assert runtime.history_seed_files() == expected


def stored() -> list[tuple]:
    """Every case row, then every review, in id order."""
    with SessionLocal() as session:
        cases = session.scalars(select(Case).order_by(Case.case_id))
        reviews = session.scalars(select(Review).order_by(Review.case_id))
        return [
            *((case.case_id, case.customer_id, case.status) for case in cases),
            *((review.case_id, review.source, review.closed_at) for review in reviews),
        ]


def test_starting_the_service_twice_leaves_one_set_of_seeded_reviews(monkeypatch, tmp_path):
    other = seed_file(tmp_path / "history_eval.yaml", [seed_entry("seed_c_010_2025", "2025-11-10")])
    monkeypatch.setenv("HISTORY_SEED_FILE", f"{SEED_FILE}:{other}")
    Base.metadata.drop_all(engine)

    with TestClient(app):  # no model: seeding needs none
        pass
    first = stored()
    with TestClient(app):
        pass

    assert stored() == first
    assert first[:2] == [("seed_c_007_2025", "c_007", "done"), ("seed_c_010_2025", "c_010", "done")]
    assert [row[:2] for row in first[2:]] == [("seed_c_007_2025", "seed"), ("seed_c_010_2025", "seed")]


@pytest.mark.parametrize(
    ("files", "seeded", "logged_by"),
    [
        pytest.param(["nothere.yaml"], [], "app.history", id="a file that is not there"),
        pytest.param(["."], [], "app.history", id="a folder"),
        pytest.param(["broken.yaml"], [], "app.main", id="not YAML"),
        pytest.param(["good.yaml", "good.yaml"], [], "app.main", id="the same seed twice"),
        pytest.param(["slip.yaml"], [], "app.main", id="a seed that is not a closed case"),
        pytest.param(
            ["nothere.yaml", "good.yaml"], ["seed_c_010_2025"], "app.history", id="a good file beside a missing one"
        ),
    ],
)
def test_a_seed_file_that_is_missing_or_broken_does_not_stop_the_start(
    monkeypatch, tmp_path, caplog, files, seeded, logged_by
):
    seed_file(tmp_path / "good.yaml", [seed_entry("seed_c_010_2025", "2025-11-10")])
    seed_file(tmp_path / "slip.yaml", [{**seed_entry("seed_c_010_2025", "2025-11-10"), "outcome": "needs_approval"}])
    (tmp_path / "broken.yaml").write_text("reviews: [\n")
    monkeypatch.setenv("HISTORY_SEED_FILE", ":".join(str(tmp_path / name) for name in files))
    Base.metadata.drop_all(engine)

    with caplog.at_level(logging.WARNING), TestClient(app) as client:
        assert client.get("/health").status_code == 200
        case_id = queued_case(client)  # signals are still taken

    assert status_in_table(case_id) == "queued"
    with SessionLocal() as session:
        assert list(session.scalars(select(Review.case_id))) == seeded
    assert [record.name for record in caplog.records if record.name in ("app.main", "app.history")] == [logged_by]


def test_seeded_cases_are_not_in_the_queue_and_have_a_case_page_with_no_report(monkeypatch, tmp_path):
    seeds = seed_file(tmp_path / "history.yaml", [seed_entry("seed_c_010_2025", "2025-11-10")])
    monkeypatch.setenv("HISTORY_SEED_FILE", str(seeds))
    Base.metadata.drop_all(engine)
    serving(monkeypatch, graph_on([*INVESTIGATION, report()], FakeCard()))

    with TestClient(app) as client:
        case_id = queued_case(client)  # a closed seed of the customer does not stop a new case
        queue = client.get("/cases", headers=analyst()).json()
        done = client.get("/cases", params={"status": "done"}, headers=analyst()).json()
        page = client.get("/cases/seed_c_010_2025", headers=analyst())
        path = client.get("/cases/seed_c_010_2025/path", headers=analyst())
        decided = client.post("/cases/seed_c_010_2025/decision", json=APPROVE, headers=analyst())

    assert [row["case_id"] for row in queue["cases"]] == [case_id]
    assert [row["case_id"] for row in done["cases"]] == ["seed_c_010_2025"]
    assert page.json() == {
        "case_id": "seed_c_010_2025",
        "customer_id": "c_010",
        "status": "done",
        "waiting_for": None,
        "report": None,  # a seed has no graph state
        "decisions": [],
        "action": None,
    }
    assert path.json() == {"case_id": "seed_c_010_2025", **EMPTY_PATH}
    assert decided.status_code == 409
    assert [review.case_id for review in reviews_of("seed_c_010_2025")] == ["seed_c_010_2025"]


# A note, and what the person decided on a hand-over


def test_a_note_longer_than_the_limit_is_refused(start):
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()

    refused = service.decide(case_id, {**APPROVE, "note": "x" * 501}, analyst())

    assert refused.status_code == 422
    assert refused.json()["error"] == "invalid_request"
    assert service.case(case_id)["waiting_for"] == "analyst"
    assert service.card.calls == []

    kept = service.decide(case_id, {**APPROVE, "note": "x" * 500}, analyst())

    assert kept.json()["decisions"] == [
        {"step": "analyst", "decision": "approve", "by": "u_analyst", "note": "x" * 500}
    ]
    [review] = reviews_of(case_id)
    assert review.decisions[0]["note"] == "x" * 500


def test_a_manager_closing_what_card_never_confirmed_may_say_why(start):
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()
    service.card.is_down = True
    service.decide(case_id, APPROVE, analyst())

    closed = service.decide(case_id, {**REJECT, "note": "  Card applied it, checked by phone.\x00 "}, manager())

    assert closed.json()["decisions"][-1] == {
        "step": "retry",
        "decision": "reject",
        "by": "u_manager",
        "note": "Card applied it, checked by phone.",
    }
    [review] = reviews_of(case_id)
    assert review.decisions == closed.json()["decisions"]


def test_an_approval_retried_with_its_note_keeps_the_note_once(start):
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()
    service.card.is_down = True
    approval = {**APPROVE, "note": "Cut as the engine says."}
    assert service.decide(case_id, approval, analyst()).status_code == 502
    service.card.is_down = False

    # The approver sends the same approval again: a retry records no new decision.
    retried = service.decide(case_id, approval, analyst())

    assert retried.status_code == 200
    assert retried.json()["status"] == "done"
    decisions = [{"step": "analyst", "decision": "approve", "by": "u_analyst", "note": "Cut as the engine says."}]
    assert retried.json()["decisions"] == decisions
    assert len(service.card.calls) == 1
    [review] = reviews_of(case_id)
    assert review.decisions == decisions


def _nothing_first(service, case_id):
    pass


def _analyst_approves(service, case_id):
    assert service.decide(case_id, APPROVE, analyst()).json()["waiting_for"] == "manager"


def _card_is_down(service, case_id):
    service.card.is_down = True
    assert service.decide(case_id, APPROVE, analyst()).status_code == 502
    service.card.is_down = False


@pytest.mark.parametrize(
    ("draft", "before", "body", "who"),
    [
        pytest.param(HAND_OVER, _nothing_first, REJECT, analyst, id="rejecting a hand-over"),
        pytest.param(report(), _nothing_first, APPROVE, analyst, id="approving a report that acts"),
        pytest.param(report(**PAUSE), _analyst_approves, APPROVE, manager, id="the manager step"),
        pytest.param(report(), _card_is_down, APPROVE, analyst, id="the retry step, approving"),
        pytest.param(report(), _card_is_down, REJECT, manager, id="the retry step, a manager closing"),
    ],
)
def test_what_the_person_decided_is_refused_anywhere_but_approving_a_hand_over(start, draft, before, body, who):
    service = start([*INVESTIGATION, draft])
    case_id = service.open_case()
    before(service, case_id)
    page = service.case(case_id)

    answer = service.decide(case_id, {**body, "decided": "request_documents"}, who())

    assert answer.status_code == 409
    assert answer.json()["error"] == "conflict"
    assert service.case(case_id) == page  # nothing was decided, nothing was closed
    assert service.card.calls == []
    assert reviews_of(case_id) == []


@pytest.mark.parametrize(
    "draft",
    [
        pytest.param(HAND_OVER, id="a report that hands the case over"),
        pytest.param(report(confidence="low"), id="a report code handed over for its low confidence"),
    ],
)
def test_what_the_person_decided_is_accepted_only_when_approving_a_hand_over(start, draft):
    service = start([*INVESTIGATION, draft])
    case_id = service.open_case()
    assert service.case(case_id)["report"]["recommendation"] == "needs_human"
    assert service.decide(case_id, {**APPROVE, "decided": "close_the_account"}, analyst()).status_code == 422

    answer = service.decide(case_id, {**APPROVE, "decided": "pause_card"}, analyst())

    case = answer.json()
    assert case["status"] == "done"
    assert case["decisions"] == [{"step": "analyst", "decision": "approve", "by": "u_analyst", "decided": "pause_card"}]
    assert case["action"] is None
    assert service.card.calls == []  # a person decided it: nothing goes to Card for it
    [review] = reviews_of(case_id)
    assert review.human_decision == "pause_card"


# The path page


def test_the_path_page_lists_each_question_with_its_topic_and_focus_and_obeys_the_case_pages_access_rule(start):
    service = start(
        [
            ask_bank("Who was paid?", topic="payee_history"),  # refused: the payee's history needs a record
            ask_bank("Where did the money go?"),
            ask_bank("Is the account reporting?", topic="accounts", account_id="acct_c010_a"),
            ask_card("Did any repayment fail?"),
            ask_bank("Is income steady?", topic="income"),
            DONE,
            gaps(),
            report(),
        ]
    )
    case_id = service.open_case()

    path = service.client.get(f"/cases/{case_id}/path", headers=analyst())

    assert path.status_code == 200
    page = path.json()
    assert set(page) == {
        "case_id", "flow", "policy_version", "rounds", "refusals", "out_of_rounds", "questions",
        "searches", "gaps", "drafts", "history", "handed_over_by",
    }  # fmt: skip
    assert (page["case_id"], page["flow"], page["policy_version"]) == (case_id, "agent", policy.POLICY_VERSION)
    assert (page["rounds"], page["refusals"], page["out_of_rounds"]) == (4, 1, False)
    # A refused call was never sent, so it carries no topic.
    assert [(question["tool"], question.get("topic"), question["outcome"]) for question in page["questions"]] == [
        ("AskCashFlow", None, "refused"),
        ("AskCashFlow", "transactions", "answered"),
        ("AskCashFlow", "accounts", "answered"),
        ("AskCreditBehavior", "repayments", "answered"),
        ("AskCashFlow", "income", "answered"),
    ]
    assert page["questions"][2]["account_id"] == "acct_c010_a"
    assert "acct_c010_a" in page["questions"][2]["returned"]
    assert page["questions"] == state_of(case_id)["asked"]
    assert page["searches"] == []
    assert [draft["result"] for draft in page["drafts"]] == ["passed"]
    assert page["history"] == ["hist_c_010_summary"]
    assert page["handed_over_by"] is None
    # Who may read it: the case page's rule.
    for headers, expected in [
        ({}, 401),
        (bearer(expires_in=-60), 401),
        (bearer(customers=("c_002",)), 403),
        (bearer(role="admin"), 200),
    ]:
        assert service.client.get(f"/cases/{case_id}/path", headers=headers).status_code == expected
    assert service.client.get("/cases/case_nothere/path", headers=analyst()).status_code == 404


def test_the_path_page_counts_every_refused_call_though_only_the_first_ones_are_free(start):
    refused = [ask_bank(f"Who was paid, try {n}?", topic="payee_history") for n in range(MAX_REFUSALS + 1)]
    service = start([*refused, *INVESTIGATION, report()])
    case_id = service.open_case()

    page = service.client.get(f"/cases/{case_id}/path", headers=analyst()).json()

    # The case's own counter stops where the free refusals end. The path counts every refused call.
    assert state_of(case_id)["refusals"] == MAX_REFUSALS
    assert page["refusals"] == MAX_REFUSALS + 1
    assert page["rounds"] == 1 + 3  # the refusal past the free ones used a round
    assert [question["outcome"] for question in page["questions"]].count("refused") == MAX_REFUSALS + 1


def test_the_path_page_says_when_the_questions_ended_at_the_round_limit(start):
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()
    before = service.client.get(f"/cases/{case_id}/path", headers=analyst()).json()
    # What the graph logs for a gap pass the round limit ended, and a search. Written here, so this test
    # reads the path page's own rules and nothing else of the gap log. The search returned nothing.
    search = {"query": "stocking payment", "returned": []}
    app.state.graph.update_state(
        runner.thread(case_id), {"gap_log": [{"ended_by": "round_limit"}], "searches": [search]}
    )

    after = service.client.get(f"/cases/{case_id}/path", headers=analyst()).json()

    assert (before["out_of_rounds"], after["out_of_rounds"]) == (False, True)
    # Both logs are passed on whole.
    assert after["gaps"] == state_of(case_id)["gap_log"]
    assert after["gaps"][-1] == {"ended_by": "round_limit"}
    assert after["searches"] == [search]


@pytest.mark.parametrize(
    ("drafts", "handed_over_by"),
    [
        pytest.param([report()], None, id="a report that acts"),
        pytest.param([HAND_OVER], "report", id="a report that hands the case over"),
        pytest.param([report(confidence="low")], "low_confidence", id="a draft code handed over for low confidence"),
        # Low confidence on a draft that already hands over: code changed nothing, the report did it.
        pytest.param([report(**HANDS_OVER, confidence="low")], "report", id="a hand-over at low confidence"),
        pytest.param([report(guideline_ids=[])] * 2, "gave_up", id="two drafts refused"),
    ],
)
def test_the_path_page_says_how_a_case_was_handed_over(start, drafts, handed_over_by):
    service = start([*INVESTIGATION, *drafts])
    case_id = service.open_case()

    page = service.client.get(f"/cases/{case_id}/path", headers=analyst()).json()

    assert page["handed_over_by"] == handed_over_by
    recommendation = service.case(case_id)["report"]["recommendation"]
    assert recommendation == ("reduce_limit" if handed_over_by is None else "needs_human")


EMPTY_PATH = {
    "flow": None,
    "policy_version": None,
    "rounds": 0,
    "refusals": 0,
    "out_of_rounds": False,
    "questions": [],
    "searches": [],
    "gaps": [],
    "drafts": [],
    "history": [],
    "handed_over_by": None,
}


def test_the_path_page_of_a_case_nobody_ran_is_empty_and_not_an_error(monkeypatch):
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:  # no model is configured
        case_id = queued_case(client)
        no_model = client.get(f"/cases/{case_id}/path", headers=analyst())
    # A model is there now, and the case has not run yet.
    serving(monkeypatch, graph_on([], FakeCard()))
    monkeypatch.setattr(runner, "run_case", lambda graph, case_id: None)
    with TestClient(app) as client:
        not_run = client.get(f"/cases/{case_id}/path", headers=analyst())

    assert no_model.status_code == not_run.status_code == 200
    assert no_model.json() == not_run.json() == {"case_id": case_id, **EMPTY_PATH}


def test_the_case_page_is_what_it_was(start):
    service = start([*INVESTIGATION, report(**PAUSE)])
    case_id = service.open_case()

    first = service.decide(case_id, APPROVE, analyst()).json()
    second = service.decide(case_id, {**APPROVE, "note": "Seen."}, bearer(sub="u_manager", role="manager")).json()

    keys = {"case_id", "customer_id", "status", "waiting_for", "report", "decisions", "action"}
    assert set(first) == set(second) == keys
    # A decision gains a note only when one was given.
    assert second["decisions"] == [
        {"step": "analyst", "decision": "approve", "by": "u_analyst"},
        {"step": "manager", "decision": "approve", "by": "u_manager", "note": "Seen."},
    ]
    # The path is a page of its own, not part of this one.
    assert service.client.get(f"/cases/{case_id}/path", headers=analyst()).json()["drafts"] != []


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


@needs_postgres
def test_an_approval_sent_twice_at_the_same_moment_leaves_one_review(start):
    # An analyst and a manager at once. SQLite ignores FOR UPDATE, so there only the primary key guards this.
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()
    service.card.takes = 0.5

    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda headers: service.decide(case_id, APPROVE, headers), [analyst(), manager()]))

    assert sorted(answer.status_code for answer in answers) == [200, 409]
    [review] = reviews_of(case_id)
    assert review.outcome == status_in_table(case_id) == "done"
    assert len(review.decisions) == 1
