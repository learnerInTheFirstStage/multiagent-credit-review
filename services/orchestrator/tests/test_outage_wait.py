"""When Bank or Card gives nothing at all, the case stops before that question, waits, and asks it again later.

Only once the retries are used up, or when retrying cannot help, does it go to a person: failed, with the real
reason, and never as the G-11 hand-over on "no guideline fits". No model: a scripted one, and the stand-ins behind a
switch that makes a side answer as its real agent does when every call to its service failed (errors.Unanswered).
"""

import logging
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import update

from app import policy, runner, runtime
from app.agents import errors, mock
from app.agents.errors import Unanswered
from app.db import Base, SessionLocal, engine, models
from app.graph import build_graph, plans
from app.main import app
from tests.conftest import bearer, sender
from tests.test_cases import FakeCard, analyst, manager, queued_case, serving, status_in_table
from tests.test_graph import CONFIG, DONE, GUIDELINES, INVESTIGATION, Run, ask_card, gaps, report, scripted, signal_of
from tests.test_signals import SIGNAL  # c_010

T0 = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)
SERVICE = {"cash_flow": "Bank", "credit_behavior": "Card"}


class Down:
    """The stand-ins, with a switch per side. While a side is down (for one topic, or for all), its answer is the
    Unanswered its real agent gives when every call to its service failed. Keeps every question it was sent."""

    def __init__(self):
        self.codes: dict[str, tuple[str, str | None]] = {}
        self.asked: list[tuple[str, str]] = []

    def fail(self, agent: str, codes: str, topic: str | None = None) -> "Down":
        self.codes[agent] = (codes, topic)
        return self

    def up(self) -> None:
        self.codes.clear()

    def specialists(self) -> dict:
        return {
            "cash_flow": lambda request: self._ask("cash_flow", mock.cash_flow, request),
            "credit_behavior": lambda request: self._ask("credit_behavior", mock.credit_behavior, request),
        }

    def _ask(self, agent, stand_in, request):
        self.asked.append((agent, request.topic))
        answer = stand_in(request)
        codes, topic = self.codes.get(agent, (None, None))
        if codes and topic in (None, request.topic):
            return Unanswered.of(answer, f"every call to the {SERVICE[agent]} Service failed ({codes})")
        return answer


def graph_with(down: Down, script, **graph_args):
    return build_graph(
        model=scripted(script),
        specialists=down.specialists(),
        load_guidelines=lambda: GUIDELINES,
        carry_out=FakeCard(),
        checkpointer=InMemorySaver(),
        **graph_args,
    )


def utc(moment: datetime | None) -> datetime | None:
    """SQLite gives back the UTC wall time without its zone."""
    return moment if moment is None or moment.tzinfo else moment.replace(tzinfo=UTC)


def wait_of(case_id: str) -> dict | None:
    """The case's row in the outage table, or None."""
    with SessionLocal() as session:
        row = session.get(models.OutageWait, case_id)
        if row is None:
            return None
        return {
            "service": row.service,
            "codes": row.codes,
            "kind": row.kind,
            "attempts": row.attempts,
            "first_seen": utc(row.first_seen),
            "next_retry_at": utc(row.next_retry_at),
        }


def state_on(graph, case_id: str) -> dict:
    return graph.get_state(runner.thread(case_id)).values


@pytest.fixture(autouse=True)
def no_cases_left():
    # A waiting case is open: one left behind would be joined by a later test's signal for its customer.
    yield
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


# What an outage is


@pytest.mark.parametrize(("codes", "kind"), [
    ("unreachable", "transient"), ("timeout", "transient"), ("http_500", "transient"), ("http_503", "transient"),
    ("http_401", "configuration"), ("http_403", "configuration"), ("http_404", "configuration"),
    ("unknown_customer", "data"),
    # Several codes: the one retrying helps least decides.
    ("http_503, timeout", "transient"), ("http_503, http_401", "configuration"), ("timeout, unknown_customer", "data"),
    ("http_401, unknown_customer", "data"),
])
def test_an_outage_is_transient_configuration_or_data_by_its_codes(codes, kind):
    outage = errors.CaseWaiting("Bank Service", f"every call to the Bank Service failed ({codes})")

    assert outage.kind == kind
    assert outage.codes == codes.split(", ")
    assert str(outage) == f"Bank Service unavailable ({codes})"


def test_a_code_nobody_classified_is_a_configuration_outage_so_a_person_is_told():
    assert errors.CaseWaiting("Card Service", "every call to the Card Service failed (something_new)").kind == "configuration"


# Through the graph


def test_the_graph_stops_before_the_question_whose_service_gave_nothing_and_asks_only_it_again():
    down = Down().fail("cash_flow", "http_503", topic="income")
    graph = graph_with(down, [*INVESTIGATION, report()])

    with pytest.raises(errors.CaseWaiting) as raised:
        graph.invoke({"case_id": "case_test", "signal": SIGNAL}, CONFIG)

    assert (raised.value.service, raised.value.codes, raised.value.kind) == ("Bank Service", ["http_503"], "transient")
    stopped = graph.get_state(CONFIG)
    assert stopped.next == ("ask_specialist",)
    # The question that met the outage is recorded neither as answered nor as no answer. What came before is kept.
    assert [(entry["topic"], entry["outcome"]) for entry in stopped.values["asked"]] == [
        ("transactions", "answered"), ("repayments", "answered"),
    ]
    assert len(stopped.values["answers"]) == 2

    down.up()
    finished = graph.invoke(None, CONFIG)

    assert finished["status"] == "needs_approval"
    assert [(entry["topic"], entry["outcome"]) for entry in finished["asked"]] == [
        ("transactions", "answered"), ("repayments", "answered"), ("income", "answered"),
    ]
    assert down.asked == [
        ("cash_flow", "transactions"), ("credit_behavior", "repayments"), ("cash_flow", "income"), ("cash_flow", "income"),
    ]


def test_a_specialist_that_raises_is_still_no_answer_and_the_case_goes_on():
    # Only a service that gave nothing stops the case. Any other failure of a specialist is no answer, as before.
    def broken(request):
        raise RuntimeError("the agent broke")

    # The report cites what Bank would have returned, so both drafts fail the check and code hands the case over.
    run = Run([*INVESTIGATION, report(), report()],
              specialists={"cash_flow": broken, "credit_behavior": mock.credit_behavior})

    outcomes = [entry["outcome"] for entry in run.state["asked"]]
    assert outcomes == ["no_answer", "answered", "no_answer"]
    assert all("unavailable" not in entry for entry in run.state["asked"])


# Through the runner


def test_a_transient_outage_waits_and_its_retry_ends_as_a_run_with_no_outage_would(client):
    reference = Run([*INVESTIGATION, report()])
    down = Down().fail("cash_flow", "http_503", topic="income")
    graph = graph_with(down, [*INVESTIGATION, report()])
    case_id = queued_case(client)

    runner.run_case(graph, case_id, now=T0)

    assert status_in_table(case_id) == "waiting_for_data"
    first = policy.OUTAGE_RETRY_SECONDS[0]
    assert wait_of(case_id) == {
        "service": "Bank Service", "codes": "http_503", "kind": "transient", "attempts": 1,
        "first_seen": T0, "next_retry_at": T0 + timedelta(seconds=first),
    }
    # Not before its time.
    assert runner.retry_due(graph, now=T0 + timedelta(seconds=first - 1)) == []
    assert status_in_table(case_id) == "waiting_for_data"

    down.up()
    assert runner.retry_due(graph, now=T0 + timedelta(seconds=first)) == [case_id]

    state = state_on(graph, case_id)
    assert status_in_table(case_id) == "needs_approval"
    assert wait_of(case_id) is None
    # The same questions, answered the same way, and the same report: only the failed question was asked twice.
    assert state["asked"] == reference.state["asked"]
    assert {**state["report"], "case_id": None} == {**reference.state["report"], "case_id": None}
    assert down.asked == [
        ("cash_flow", "transactions"), ("credit_behavior", "repayments"), ("cash_flow", "income"), ("cash_flow", "income"),
    ]


@pytest.mark.parametrize(("codes", "level"), [
    ("http_401", logging.ERROR), ("http_403", logging.ERROR), ("http_404", logging.ERROR),
    ("http_503", logging.WARNING), ("timeout", logging.WARNING), ("unreachable", logging.WARNING),
])
def test_a_configuration_outage_is_logged_as_an_error_and_a_transient_one_as_a_warning_and_both_wait(
    client, caplog, codes, level
):
    graph = graph_with(Down().fail("cash_flow", codes).fail("credit_behavior", codes), [*INVESTIGATION, report()])
    case_id = queued_case(client)

    with caplog.at_level(logging.INFO, logger="app.runner"):
        runner.run_case(graph, case_id, now=T0)

    assert status_in_table(case_id) == "waiting_for_data"
    said = [r for r in caplog.records if r.name == "app.runner" and codes in r.getMessage()]
    assert said and {r.levelno for r in said} == {level}
    assert case_id in said[0].getMessage()
    # Not the catch-all: no traceback, and not "failed".
    assert not any(r.exc_info for r in caplog.records if r.name == "app.runner")


def test_a_data_outage_fails_at_once_with_the_reason_and_no_report(client, caplog):
    graph = graph_with(Down().fail("cash_flow", "unknown_customer"), [*INVESTIGATION, report()])
    case_id = queued_case(client)

    with caplog.at_level(logging.INFO, logger="app.runner"):
        runner.run_case(graph, case_id, now=T0)

    assert status_in_table(case_id) == "failed"
    assert wait_of(case_id) == {
        "service": "Bank Service", "codes": "unknown_customer", "kind": "data", "attempts": 1,
        "first_seen": T0, "next_retry_at": None,
    }
    assert runner.describe(wait_of(case_id)) == (
        "Bank Service unavailable (unknown_customer) since 2026-11-18T10:00Z, not retried: retrying cannot help"
    )
    assert any(r.levelno == logging.ERROR and "unknown_customer" in r.getMessage() for r in caplog.records)
    state = state_on(graph, case_id)
    assert state.get("report") is None and state.get("check") is None
    # A failed case is not retried by the service on its own.
    assert runner.retry_due(graph, now=T0 + timedelta(days=1)) == []


def test_when_the_retries_are_used_up_the_case_fails_with_the_reason_and_is_never_a_g11_hand_over(client):
    down = Down().fail("cash_flow", "http_503")
    graph = graph_with(down, [*INVESTIGATION, report()])
    case_id = queued_case(client)
    runner.run_case(graph, case_id, now=T0)

    at = T0
    for attempt, delay in enumerate(policy.OUTAGE_RETRY_SECONDS, start=1):
        assert status_in_table(case_id) == "waiting_for_data"
        assert wait_of(case_id)["attempts"] == attempt
        assert wait_of(case_id)["next_retry_at"] == at + timedelta(seconds=delay)
        at += timedelta(seconds=delay)
        assert runner.retry_due(graph, now=at) == [case_id]

    retries = len(policy.OUTAGE_RETRY_SECONDS)
    assert status_in_table(case_id) == "failed"
    wait = wait_of(case_id)
    assert (wait["attempts"], wait["first_seen"], wait["next_retry_at"]) == (retries + 1, T0, None)
    assert runner.describe(wait) == f"Bank Service unavailable (http_503) since 2026-11-18T10:00Z, retried {retries} times"
    # The same question each time, and nothing after it: no report, so no hand-over on G-11 or on anything else.
    assert down.asked == [("cash_flow", "transactions")] * (retries + 1)
    state = state_on(graph, case_id)
    assert state.get("report") is None and state.get("asked", []) == []
    assert runner.retry_due(graph, now=at + timedelta(days=1)) == []


def test_an_outage_at_a_later_question_is_a_new_outage_with_its_own_attempts(client):
    # Bank fails the third question; on the retry Bank answers, and Card fails the fourth.
    script = [*INVESTIGATION[:3], ask_card("How is the card used?", topic="card_use"), DONE, gaps(), report()]
    down = Down().fail("cash_flow", "http_503", topic="income")
    graph = graph_with(down, script)
    case_id = queued_case(client)
    runner.run_case(graph, case_id, now=T0)
    first = T0 + timedelta(seconds=policy.OUTAGE_RETRY_SECONDS[0])

    down.up()
    down.fail("credit_behavior", "http_503", topic="card_use")
    assert runner.retry_due(graph, now=first) == [case_id]

    assert status_in_table(case_id) == "waiting_for_data"
    assert wait_of(case_id) == {
        "service": "Card Service", "codes": "http_503", "kind": "transient", "attempts": 1,
        "first_seen": first, "next_retry_at": first + timedelta(seconds=policy.OUTAGE_RETRY_SECONDS[0]),
    }


def test_a_case_another_run_took_is_not_taken_again():
    with SessionLocal() as session:
        session.add(models.Case(case_id="case_taken", customer_id="c_010", status="waiting_for_data"))
        session.commit()

    assert runner.claim("case_taken", "waiting_for_data") is True
    assert runner.claim("case_taken", "waiting_for_data") is False
    assert status_in_table("case_taken") == "investigating"


def test_the_retry_tick_starts_no_case_once_the_service_is_stopping(client):
    graph = graph_with(Down().fail("cash_flow", "http_503"), [], plan=plans.FIXED_PLAN)
    case_id = queued_case(client)
    runner.run_case(graph, case_id, now=T0)

    assert runner.retry_due(graph, now=T0 + timedelta(days=1), stopping=lambda: True) == []
    assert status_in_table(case_id) == "waiting_for_data"


def test_an_outage_whose_wait_cannot_be_recorded_is_logged_and_the_case_is_left_for_the_next_start(
    client, monkeypatch, caplog
):
    graph = graph_with(Down().fail("cash_flow", "http_503"), [], plan=plans.FIXED_PLAN)
    case_id = queued_case(client)

    def database_gone(*args):
        raise ConnectionError("the database did not answer")

    monkeypatch.setattr(runner, "_wait_or_fail", database_gone)
    with caplog.at_level(logging.ERROR, logger="app.runner"):
        runner.run_case(graph, case_id, now=T0)  # does not raise

    assert status_in_table(case_id) == "investigating"
    assert case_id in runner.unfinished()
    assert any(r.exc_info and "Bank Service unavailable (http_503)" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(("setting", "seconds", "warned"), [
    (None, 15.0, False), ("", 15.0, False), ("2.5", 2.5, False),
    ("15s", 15.0, True), ("0", 15.0, True), ("-3", 15.0, True), ("inf", 15.0, True), ("nan", 15.0, True),
])
def test_the_retry_loop_runs_every_so_often_and_a_bad_setting_neither_ends_it_nor_makes_it_spin(
    monkeypatch, caplog, setting, seconds, warned
):
    if setting is None:
        monkeypatch.delenv("OUTAGE_CHECK_SECONDS", raising=False)
    else:
        monkeypatch.setenv("OUTAGE_CHECK_SECONDS", setting)

    with caplog.at_level(logging.WARNING, logger="app.runtime"):
        assert runtime.retry_check_seconds() == seconds
    assert any("OUTAGE_CHECK_SECONDS" in r.getMessage() for r in caplog.records) is warned


def test_the_retry_delays_are_the_policys_and_a_case_waits_at_least_once():
    assert policy.OUTAGE_RETRY_SECONDS == (60, 300, 900)
    # type() and not isinstance(): True is an int. Empty, a transient outage would fail a case at once.
    assert type(policy.OUTAGE_RETRY_SECONDS) is tuple and policy.OUTAGE_RETRY_SECONDS
    assert all(type(seconds) is int and seconds > 0 for seconds in policy.OUTAGE_RETRY_SECONDS)


def test_the_retry_tick_runs_only_the_waiting_cases_whose_time_has_come(client):
    down = Down().fail("cash_flow", "http_503")
    graph = graph_with(down, [], plan=plans.FIXED_PLAN)  # code asks: no model before the report
    early = queued_case(client)
    late = client.post("/signals", json=signal_of("c_009"), headers=sender()).json()["case_id"]
    taken = client.post("/signals", json=signal_of("c_008"), headers=sender()).json()["case_id"]
    runner.run_case(graph, early, now=T0)
    runner.run_case(graph, late, now=T0 + timedelta(seconds=120))
    runner.run_case(graph, taken, now=T0)
    # Another run has this one already (the retry endpoint, or a tick before): its row says due, its status does not.
    runner._set_status(taken, "investigating")
    asked = len(down.asked)

    ran = runner.retry_due(graph, now=T0 + timedelta(seconds=150))

    assert ran == [early]
    assert len(down.asked) == asked + 1
    assert (wait_of(early)["attempts"], wait_of(late)["attempts"], wait_of(taken)["attempts"]) == (2, 1, 1)
    assert (status_in_table(late), status_in_table(taken)) == ("waiting_for_data", "investigating")


def test_a_start_does_not_pick_up_a_waiting_case_before_its_time(client):
    graph = graph_with(Down().fail("cash_flow", "http_503"), [*INVESTIGATION, report()])
    case_id = queued_case(client)
    runner.run_case(graph, case_id, now=T0)

    assert status_in_table(case_id) == "waiting_for_data"
    assert case_id not in runner.unfinished()


def test_a_restart_leaves_a_waiting_case_to_its_time_and_then_retries_it(monkeypatch):
    Base.metadata.drop_all(engine)
    with TestClient(app) as before:  # no model yet: the case stays queued
        case_id = queued_case(before)
    down = Down().fail("cash_flow", "http_503", topic="income")
    serving(monkeypatch, graph_with(down, [*INVESTIGATION, report()]))

    with TestClient(app):
        pass  # the start picks the queued case up, and it meets the outage
    assert status_in_table(case_id) == "waiting_for_data"
    asked = list(down.asked)

    down.up()
    with TestClient(app):
        pass  # its retry time has not come: neither the pick-up nor the retry loop runs it
    assert status_in_table(case_id) == "waiting_for_data" and down.asked == asked

    with SessionLocal() as session:  # the service was down past its retry time
        session.execute(
            update(models.OutageWait)
            .where(models.OutageWait.case_id == case_id)
            .values(next_retry_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        session.commit()
    with TestClient(app):
        pass

    assert status_in_table(case_id) == "needs_approval"
    assert down.asked == [*asked, ("cash_flow", "income")]
    assert wait_of(case_id) is None


# Through the service


@pytest.fixture
def serve(monkeypatch):
    """Start the service on a scripted model and the switchable stand-ins."""
    with ExitStack() as stack:

        def _start(down: Down, script=(*INVESTIGATION, report())) -> TestClient:
            serving(monkeypatch, graph_with(down, list(script)))
            Base.metadata.drop_all(engine)
            return stack.enter_context(TestClient(app))

        yield _start


def open_case(client, **changes) -> str:
    """Send a signal. Its run has ended by the time this returns."""
    return client.post("/signals", json={**SIGNAL, **changes}, headers=sender()).json()["case_id"]


def page(client, case_id: str) -> dict:
    return client.get(f"/cases/{case_id}", headers=analyst()).json()


def retry(client, case_id: str, headers: dict):
    return client.post(f"/cases/{case_id}/retry", headers=headers)


def test_the_case_page_the_queue_and_the_path_page_say_why_a_case_waits(serve):
    client = serve(Down().fail("cash_flow", "http_503", topic="income"))
    case_id = open_case(client)

    shown = page(client, case_id)
    assert (shown["status"], shown["waiting_for"], shown["report"]) == ("waiting_for_data", None, None)
    outage = shown["outage"]
    assert {key: outage[key] for key in ("service", "codes", "kind", "attempts")} == {
        "service": "Bank Service", "codes": "http_503", "kind": "transient", "attempts": 1,
    }
    since, then = datetime.fromisoformat(outage["since"]), datetime.fromisoformat(outage["next_retry_at"])
    assert then - since == timedelta(seconds=policy.OUTAGE_RETRY_SECONDS[0])
    assert outage["message"].startswith("Bank Service unavailable (http_503) since ")
    assert "next retry at" in outage["message"]

    [row] = client.get("/cases", headers=analyst()).json()["cases"]
    assert (row["case_id"], row["status"], row["outage"]) == (case_id, "waiting_for_data", outage)
    assert client.get("/cases", params={"status": "waiting_for_data"}, headers=analyst()).json()["total"] == 1

    path = client.get(f"/cases/{case_id}/path", headers=analyst()).json()
    assert path["outage"] == outage
    # The question that met the outage is not on the path: it was not answered, and it was not "no answer".
    assert [(q["topic"], q["outcome"]) for q in path["questions"]] == [("transactions", "answered"), ("repayments", "answered")]


def test_a_case_that_ran_without_an_outage_shows_none(serve):
    client = serve(Down())
    case_id = open_case(client)

    assert page(client, case_id)["outage"] is None
    assert client.get("/cases", headers=analyst()).json()["cases"][0]["outage"] is None
    assert client.get(f"/cases/{case_id}/path", headers=analyst()).json()["outage"] is None


def test_an_analyst_or_a_manager_of_the_customer_can_retry_a_waiting_case_now(serve):
    down = Down().fail("cash_flow", "http_503", topic="income")
    client = serve(down)
    case_id = open_case(client)

    still_down = retry(client, case_id, analyst())

    assert still_down.status_code == 202 and still_down.json()["status"] == "investigating"
    assert page(client, case_id)["status"] == "waiting_for_data"
    # One counter: a retry by hand is an attempt like any other.
    assert page(client, case_id)["outage"]["attempts"] == 2

    down.up()
    assert retry(client, case_id, manager()).status_code == 202

    shown = page(client, case_id)
    assert (shown["status"], shown["waiting_for"], shown["outage"]) == ("needs_approval", "analyst", None)
    assert down.asked.count(("cash_flow", "income")) == 3
    assert down.asked.count(("cash_flow", "transactions")) == 1


@pytest.mark.parametrize(("headers", "code"), [
    (bearer(customers=("c_other",)), 403),  # an analyst of other customers
    (bearer(sub="u_admin", role="admin"), 403),  # sees the customer, but is not an analyst or a manager
    (sender(), 403),  # the signal generator
    ({}, 401),
])
def test_who_may_not_retry_a_case(serve, headers, code):
    client = serve(Down().fail("cash_flow", "http_503"))
    case_id = open_case(client)

    refused = retry(client, case_id, headers)

    assert refused.status_code == code
    assert page(client, case_id)["status"] == "waiting_for_data"
    assert page(client, case_id)["outage"]["attempts"] == 1


def test_retrying_a_case_that_does_not_exist_is_a_404(serve):
    client = serve(Down())

    assert retry(client, "case_nope", analyst()).status_code == 404


def test_with_no_model_no_case_is_retried(client):
    case_id = queued_case(client)

    refused = retry(client, case_id, analyst())

    assert refused.status_code == 503 and refused.json()["error"] == "unavailable"


def test_a_case_that_is_not_waiting_for_bank_or_card_cannot_be_retried(serve):
    client = serve(Down())
    case_id = open_case(client)  # it ran: it waits for the analyst

    refused = retry(client, case_id, analyst())

    assert refused.status_code == 409 and refused.json()["error"] == "conflict"
    assert "needs_approval" in refused.json()["message"]
    assert page(client, case_id)["status"] == "needs_approval"


def test_a_case_that_failed_for_another_reason_cannot_be_retried(serve):
    # The first draft fails the check, and the model has nothing left for the redraft: the run raised.
    client = serve(Down(), script=(*INVESTIGATION, report(guideline_ids=[])))
    case_id = open_case(client)
    assert (page(client, case_id)["status"], page(client, case_id)["outage"]) == ("failed", None)

    assert retry(client, case_id, analyst()).status_code == 409


def test_a_case_that_failed_for_an_outage_can_be_retried_once_the_service_is_back(serve):
    down = Down().fail("cash_flow", "unknown_customer")
    client = serve(down)
    case_id = open_case(client)
    failed = page(client, case_id)
    assert (failed["status"], failed["outage"]["kind"], failed["outage"]["next_retry_at"]) == ("failed", "data", None)
    assert failed["outage"]["message"].startswith("Bank Service unavailable (unknown_customer) since ")
    # It is closed: the customer is not in the queue, and a decision is refused.
    assert client.get("/cases", headers=analyst()).json()["total"] == 0
    assert client.post(f"/cases/{case_id}/decision", json={"decision": "reject"}, headers=manager()).status_code == 409

    down.up()
    assert retry(client, case_id, analyst()).status_code == 202

    assert page(client, case_id)["status"] == "needs_approval"


def test_a_failed_case_is_not_reopened_while_its_customer_has_another_open_case(serve):
    down = Down().fail("cash_flow", "unknown_customer")
    # The first case asks one question and fails on it; the second runs the whole investigation.
    client = serve(down, script=(INVESTIGATION[0], *INVESTIGATION, report()))
    case_id = open_case(client)
    down.up()
    newer = open_case(client, signal_id="sig_552")  # a failed case is closed, so the next signal opens a new one
    assert newer != case_id and page(client, newer)["status"] == "needs_approval"

    refused = retry(client, case_id, analyst())

    assert refused.status_code == 409 and newer in refused.json()["message"]
    assert page(client, case_id)["status"] == "failed"
