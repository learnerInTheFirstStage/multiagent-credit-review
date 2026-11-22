"""Rules that hold for the whole API: who may call what, the queue, and the shape of an error."""

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app import dev_token
from app.api import signals
from app.db import Base, SessionLocal, engine
from app.db.models import Case, SignalRow
from app.graph import facts
from app.main import app
from tests.conftest import bearer, sender
from tests.test_signals import SIGNAL  # c_010


@pytest.fixture
def api():
    """The service with no model behind it. Unlike `client`, it sends no token of its own."""
    Base.metadata.drop_all(engine)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def stored(model) -> int:
    with SessionLocal() as session:
        return session.scalar(select(func.count()).select_from(model))


# Who may send a signal


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param(sender(), 201, id="the signal generator"),
        pytest.param(bearer(role="admin"), 201, id="an admin"),
        pytest.param({}, 401, id="no token"),
        pytest.param(bearer(sub="signal-generator", role="service", expires_in=-60), 401, id="an expired token"),
        pytest.param(bearer(role="analyst"), 403, id="an analyst"),
        pytest.param(bearer(role="manager"), 403, id="a manager"),
    ],
)
def test_only_a_service_or_an_admin_may_send_a_signal(api, headers, expected):
    answer = api.post("/signals", json=SIGNAL, headers=headers)

    assert answer.status_code == expected
    assert stored(Case) == stored(SignalRow) == (1 if expected == 201 else 0)


def test_a_caller_with_no_token_learns_nothing_about_what_a_signal_looks_like(api):
    answer = api.post("/signals", json={"signal_id": ""})

    assert answer.status_code == 401
    assert "signal_id" not in answer.text


def test_a_caller_who_may_not_send_is_refused_before_the_signal_is_looked_at(api):
    answer = api.post("/signals", json={"signal_id": ""}, headers=bearer(role="analyst"))

    assert answer.status_code == 403
    assert "signal_id" not in answer.text


# The queue


def open_cases(api, *customers: str) -> dict[str, str]:
    """One queued case per customer, each a minute older than the next. Returns customer -> case_id."""
    cases = {}
    for age, customer in enumerate(customers):
        signal = {**SIGNAL, "customer_id": customer, "signal_id": f"sig_{customer}"}
        cases[customer] = api.post("/signals", json=signal, headers=sender()).json()["case_id"]
        created = datetime.now(UTC) - timedelta(minutes=len(customers) - age)
        with SessionLocal() as session:
            session.execute(update(Case).where(Case.case_id == cases[customer]).values(created_at=created))
            session.commit()
    return cases


def set_status(case_id: str, status: str) -> None:
    with SessionLocal() as session:
        session.execute(update(Case).where(Case.case_id == case_id).values(status=status))
        session.commit()


def test_the_queue_shows_a_token_only_its_own_customers_the_longest_waiting_first(api):
    cases = open_cases(api, "c_010", "c_007", "c_002")

    queue = api.get("/cases", headers=bearer(customers=("c_002", "c_010")))

    assert queue.status_code == 200
    assert [row["case_id"] for row in queue.json()["cases"]] == [cases["c_010"], cases["c_002"]]
    assert queue.json()["total"] == 2
    first = queue.json()["cases"][0]
    assert first["customer_id"] == "c_010"
    assert first["status"] == "queued"
    # outage: why the case waits for Bank or Card, or failed for want of them (tests/test_outage_wait.py).
    # signal_reasons: the review reasons the case's signals named, in arrival order (tests/test_cases.py).
    # recommendation, confidence and waiting_for as on the case page (tests/test_cases.py); cut_share_bp below.
    assert set(first) == {
        "case_id", "customer_id", "status", "created_at", "updated_at", "outage", "signal_reasons",
        "recommendation", "confidence", "cut_share_bp", "waiting_for", "imported", "documents_due",
    }
    assert first["outage"] is None


def test_the_queue_leaves_out_closed_cases_unless_they_are_asked_for(api):
    cases = open_cases(api, "c_010", "c_007", "c_002")  # c_010 is the oldest
    set_status(cases["c_010"], "done")
    set_status(cases["c_007"], "failed")
    set_status(cases["c_002"], "needs_manager")
    everyone = bearer(customers=("c_002", "c_007", "c_010"))

    queue = api.get("/cases", headers=everyone).json()
    finished = api.get("/cases", params={"status": "done"}, headers=everyone).json()

    assert [row["case_id"] for row in queue["cases"]] == [cases["c_002"]]
    assert queue["total"] == 1
    assert [row["case_id"] for row in finished["cases"]] == [cases["c_010"]]


def test_the_queue_can_be_narrowed_to_one_status(api):
    cases = open_cases(api, "c_010", "c_002")
    set_status(cases["c_002"], "needs_approval")
    both = bearer(customers=("c_002", "c_010"))

    waiting = api.get("/cases", params={"status": "needs_approval"}, headers=both).json()
    nothing = api.get("/cases", params={"status": "rejected"}, headers=both).json()

    assert [row["case_id"] for row in waiting["cases"]] == [cases["c_002"]]
    assert waiting["total"] == 1
    assert nothing == {"cases": [], "total": 0}


def test_the_queue_is_read_a_page_at_a_time_and_the_total_counts_every_match(api):
    cases = open_cases(api, "c_010", "c_007", "c_002")
    everyone = bearer(customers=("c_002", "c_007", "c_010"))

    second = api.get("/cases", params={"limit": 1, "offset": 1}, headers=everyone).json()
    past_the_end = api.get("/cases", params={"limit": 5, "offset": 3}, headers=everyone).json()

    assert [row["case_id"] for row in second["cases"]] == [cases["c_007"]]
    assert second["total"] == 3
    assert past_the_end == {"cases": [], "total": 3}


def cut_on_record(signal: dict) -> int | None:
    """The engine's cut as the signal's own record of the limit gives it, the one a report cites."""
    window = {"start": signal["as_of"], "end": signal["as_of"]}  # the limit's record does not use it
    [limit] = [record for record in facts.signal_facts(signal, window) if record["id"] == "sig_limit"]
    return limit["data"].get("cut_share_bp")


@pytest.mark.parametrize(
    "engine_limit_cents",
    [
        pytest.param(SIGNAL["current_limit_cents"] // 3, id="a cut that is not a whole basis point"),
        pytest.param(SIGNAL["current_limit_cents"], id="no cut"),
    ],
)
def test_a_queue_row_carries_the_engines_cut_as_the_signals_record_of_it_says(api, engine_limit_cents):
    signal = {**SIGNAL, "engine_limit_cents": engine_limit_cents}
    api.post("/signals", json=signal, headers=sender())

    [row] = api.get("/cases", headers=bearer()).json()["cases"]

    assert row["cut_share_bp"] == cut_on_record(signal)
    assert (row["recommendation"], row["confidence"], row["waiting_for"]) == (None, None, None)  # nobody ran it


def test_a_queue_row_keeps_the_cut_of_the_signal_the_case_runs_on_when_another_joins(api):
    api.post("/signals", json=SIGNAL, headers=sender())
    # Sorts after the first, as received_at can tie within a second.
    joined = {**SIGNAL, "signal_id": "sig_552", "engine_limit_cents": SIGNAL["current_limit_cents"] // 3}
    assert api.post("/signals", json=joined, headers=sender()).json()["result"] == "joined"

    [row] = api.get("/cases", headers=bearer()).json()["cases"]

    assert row["cut_share_bp"] == cut_on_record(SIGNAL) != cut_on_record(joined)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param(bearer(customers=()), {"cases": [], "total": 0}, id="a token with no customers"),
        pytest.param(sender(), {"cases": [], "total": 0}, id="a service token"),
        pytest.param(bearer(customers=("c_099",)), {"cases": [], "total": 0}, id="a customer with no case"),
    ],
)
def test_a_token_that_may_see_no_case_gets_an_empty_queue(api, headers, expected):
    open_cases(api, "c_010")

    assert api.get("/cases", headers=headers).json() == expected


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        pytest.param({"status": "on_fire"}, 422, id="a status that does not exist"),
        pytest.param({"limit": 0}, 422, id="a page of nothing"),
        pytest.param({"limit": 201}, 422, id="a page too large"),
        pytest.param({"offset": -1}, 422, id="a page before the first"),
        pytest.param({"limit": 200}, 200, id="the largest page"),
    ],
)
def test_the_queue_refuses_a_question_it_cannot_answer(api, params, expected):
    assert api.get("/cases", params=params, headers=bearer()).status_code == expected


def test_the_queue_needs_a_token(api):
    open_cases(api, "c_010")

    answer = api.get("/cases")

    assert answer.status_code == 401
    assert "case_" not in answer.text


# The shape of an error. API doc section 8.


def error_of(answer) -> str:
    """The error name, after checking the body has the one agreed shape."""
    body = answer.json()
    assert set(body) == {"error", "message"}, body
    assert isinstance(body["message"], str) and body["message"]
    return body["error"]


def test_every_refusal_has_the_same_shape(api):
    case_id = open_cases(api, "c_010")["c_010"]
    changed = {**SIGNAL, "signal_id": "sig_c_010", "engine_limit_cents": 1}

    assert error_of(api.get(f"/cases/{case_id}")) == "unauthorized"
    assert error_of(api.get(f"/cases/{case_id}", headers=bearer(customers=("c_002",)))) == "forbidden"
    assert error_of(api.get("/cases/case_nothere", headers=bearer())) == "not_found"
    assert error_of(api.post("/signals", json=changed, headers=sender())) == "conflict"
    assert error_of(api.get("/cases", params={"limit": 0}, headers=bearer())) == "invalid_request"
    assert error_of(api.post(f"/cases/{case_id}/decision", json={"decision": "approve"}, headers=bearer())) == "unavailable"
    assert error_of(api.get("/no/such/page")) == "not_found"
    assert error_of(api.delete("/signals")) == "method_not_allowed"


def test_a_refused_token_still_says_how_to_authenticate(api):
    assert api.get("/cases").headers["WWW-Authenticate"] == "Bearer"


def test_a_malformed_request_is_told_where_every_problem_is(api):
    bad = {**SIGNAL, "review_reasons": [], "as_of": "not a date"}

    answer = api.post("/signals", json=bad, headers=sender())

    assert answer.status_code == 422
    assert error_of(answer) == "invalid_request"
    assert "body.review_reasons" in answer.json()["message"]
    assert "body.as_of" in answer.json()["message"]


def test_a_failure_inside_the_service_is_an_error_of_the_same_shape_that_gives_nothing_away(api, monkeypatch):
    def broken(session, signal):
        raise RuntimeError("password=hunter2 in the connection string")

    monkeypatch.setattr(signals, "_place", broken)

    answer = api.post("/signals", json=SIGNAL, headers=sender())

    assert answer.status_code == 500
    assert error_of(answer) == "internal_server_error"
    assert "hunter2" not in answer.text


# Tokens for local use


def test_a_local_token_is_accepted_like_any_other_and_carries_what_was_asked(api):
    case_id = open_cases(api, "c_010")["c_010"]
    token = dev_token.make("u_new_analyst", "analyst", ["c_010"], hours=1)

    assert api.get(f"/cases/{case_id}", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    claims = jwt.decode(token, options={"verify_signature": False})
    assert (claims["sub"], claims["role"], claims["customers"]) == ("u_new_analyst", "analyst", ["c_010"])
    assert 3500 < claims["exp"] - datetime.now(UTC).timestamp() <= 3600


def test_the_token_command_prints_a_token_the_service_accepts(api):
    printed = subprocess.run(
        [sys.executable, "-m", "app.dev_token", "--role", "service", "--sub", "signal-generator"],
        cwd=Path(__file__).parents[1],  # wherever pytest was started from
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    answer = api.post("/signals", json=SIGNAL, headers={"Authorization": f"Bearer {printed}"})

    assert answer.status_code == 201
