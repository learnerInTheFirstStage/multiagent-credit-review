import copy

import pytest
from sqlalchemy import func, select

from app.api import signals
from app.db.models import Case, SignalRow

# The example in API doc section 3.
SIGNAL = {
    "signal_id": "sig_551",
    "customer_id": "c_002",
    "type": "balance_drop",
    "engine_limit_cents": 9600000,
    "snapshot": {
        "bank": {"cash_now_cents": 48000000, "cash_7d_ago_cents": 120000000},
        "card": {
            "limit_cents": 22000000,
            "utilization_pct": 28,
            "repayments_last3": ["paid", "paid", "paid"],
        },
    },
    "hard_facts": [],
}


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def test_new_signal_opens_a_queued_case(client, session):
    response = client.post("/signals", json=SIGNAL)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "queued"
    assert body["duplicate"] is False

    case = session.get(Case, body["case_id"])
    assert case.signal_id == "sig_551"
    assert case.customer_id == "c_002"
    assert session.get(SignalRow, "sig_551").payload == {
        **SIGNAL,
        "review_reason": None,
        "as_of": None,
    }


def test_repeated_signal_returns_the_same_case_and_opens_nothing(client, session):
    first = client.post("/signals", json=SIGNAL)
    second = client.post("/signals", json=SIGNAL)

    assert second.status_code == 200
    assert second.json() == {**first.json(), "duplicate": True}
    assert count(session, Case) == 1
    assert count(session, SignalRow) == 1


def test_signal_stored_by_another_request_mid_flight_is_a_duplicate(client, session, monkeypatch):
    """Two requests carry the same new signal. The loser's insert fails and it returns the winner's case."""
    first = client.post("/signals", json=SIGNAL)

    # The second request looks before the first one has committed, so it sees no case yet.
    real_lookup = signals._case_for
    first_lookup = [True]

    def lookup(db_session, signal_id):
        if first_lookup[0]:
            first_lookup[0] = False
            return None
        return real_lookup(db_session, signal_id)

    monkeypatch.setattr(signals, "_case_for", lookup)
    second = client.post("/signals", json=SIGNAL)

    assert second.status_code == 200
    assert second.json() == {**first.json(), "duplicate": True}
    assert count(session, Case) == 1
    assert count(session, SignalRow) == 1


def test_two_signals_open_two_cases(client, session):
    other = {**SIGNAL, "signal_id": "sig_552", "customer_id": "c_008"}

    first = client.post("/signals", json=SIGNAL)
    second = client.post("/signals", json=other)

    assert second.status_code == 201
    assert first.json()["case_id"] != second.json()["case_id"]
    assert count(session, Case) == 2


def without(field: str) -> dict:
    signal = copy.deepcopy(SIGNAL)
    del signal[field]
    return signal


@pytest.mark.parametrize(
    "signal",
    [
        pytest.param(without("signal_id"), id="no signal_id"),
        pytest.param(without("customer_id"), id="no customer_id"),
        pytest.param(without("type"), id="no type"),
        pytest.param(without("snapshot"), id="no snapshot"),
        pytest.param(without("engine_limit_cents"), id="no engine limit"),
        pytest.param(without("hard_facts"), id="no hard_facts"),
        pytest.param({**SIGNAL, "signal_id": ""}, id="empty signal_id"),
        pytest.param({**SIGNAL, "customer_id": "002"}, id="customer_id not c_NNN"),
        pytest.param({**SIGNAL, "customer_id": "c_٠٠٢"}, id="customer_id with non-ASCII digits"),
        pytest.param({**SIGNAL, "type": "limit_increase_request"}, id="unsupported type"),
        pytest.param({**SIGNAL, "engine_limit_cents": -1}, id="negative limit"),
        pytest.param({**SIGNAL, "engine_limit_cents": "9600000"}, id="limit as a string"),
        pytest.param({**SIGNAL, "engine_limit_cents": True}, id="limit as a boolean"),
        pytest.param({**SIGNAL, "engine_limit_cents": 9600000.5}, id="limit with a fraction"),
        pytest.param({**SIGNAL, "snapshot": {"bank": {}}}, id="snapshot without card"),
        pytest.param({**SIGNAL, "cause": "payroll"}, id="unknown field"),
    ],
)
def test_malformed_signal_is_refused_and_opens_nothing(client, session, signal):
    response = client.post("/signals", json=signal)

    assert response.status_code == 422
    assert count(session, Case) == 0
    assert count(session, SignalRow) == 0
