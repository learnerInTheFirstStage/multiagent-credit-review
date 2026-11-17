import copy

import pytest
from sqlalchemy import func, select

from app.api import signals
from app.db.models import Case, SignalRow

# The example in API doc section 3: two review reasons at once.
SIGNAL = {
    "signal_id": "sig_551",
    "customer_id": "c_010",
    "as_of": "2026-11-10",
    "current_limit_cents": 10000000,
    "engine_limit_cents": 5800000,
    "review_reasons": ["large_cut", "failed_repayment"],
    "snapshot": {
        "bank": {
            "cash_now_cents": 29000000,
            "cash_7d_ago_cents": 52000000,
            "cash_30d_avg_cents": 47000000,
            "accounts": [{"id": "acct_c010_a", "last_synced": "2026-11-10"}],
        },
        "card": {
            "balance_cents": 9200000,
            "repayments": [
                {"due": "2026-09-15", "amount_cents": 4100000, "status": "paid"},
                {"due": "2026-10-15", "amount_cents": 4600000, "status": "paid"},
                {"due": "2026-11-05", "amount_cents": 5000000, "status": "failed"},
            ],
        },
    },
}


def count(session, model) -> int:
    return session.scalar(select(func.count()).select_from(model))


def changed(path: str, value) -> dict:
    """The example with one value replaced, for instance changed("snapshot.bank.accounts", [])."""
    signal = copy.deepcopy(SIGNAL)
    *parents, last = path.split(".")
    target = signal
    for key in parents:
        target = target[key]
    target[last] = value
    return signal


def without(path: str) -> dict:
    signal = copy.deepcopy(SIGNAL)
    *parents, last = path.split(".")
    target = signal
    for key in parents:
        target = target[key]
    del target[last]
    return signal


def test_new_signal_opens_a_queued_case(client, session):
    response = client.post("/signals", json=SIGNAL)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "queued"
    assert body["result"] == "opened"

    assert session.get(Case, body["case_id"]).customer_id == "c_010"
    stored = session.get(SignalRow, "sig_551")
    assert stored.case_id == body["case_id"]
    assert stored.payload == SIGNAL


def test_repeated_signal_returns_the_same_case_and_stores_nothing(client, session):
    first = client.post("/signals", json=SIGNAL)
    second = client.post("/signals", json=SIGNAL)

    assert second.status_code == 200
    assert second.json() == {**first.json(), "result": "duplicate"}
    assert count(session, Case) == 1
    assert count(session, SignalRow) == 1


@pytest.mark.parametrize(
    "reused",
    [
        pytest.param({**SIGNAL, "customer_id": "c_008"}, id="another customer"),
        pytest.param(changed("engine_limit_cents", 100), id="other numbers"),
    ],
)
def test_signal_id_reused_with_other_content_is_refused(client, session, reused):
    first = client.post("/signals", json=SIGNAL)

    second = client.post("/signals", json=reused)

    assert second.status_code == 409
    assert first.json()["case_id"] not in second.text
    assert count(session, Case) == 1
    assert count(session, SignalRow) == 1
    assert session.get(SignalRow, "sig_551").payload == SIGNAL


def test_new_signal_for_a_customer_with_an_open_case_joins_it(client, session):
    first = client.post("/signals", json=SIGNAL)
    second = client.post("/signals", json=changed("signal_id", "sig_552"))

    assert second.status_code == 200
    assert second.json() == {**first.json(), "result": "joined"}
    assert count(session, Case) == 1
    assert count(session, SignalRow) == 2
    assert session.get(SignalRow, "sig_552").case_id == first.json()["case_id"]


@pytest.mark.parametrize("closed", ["done", "rejected", "failed"])
def test_new_signal_after_the_case_is_closed_opens_a_new_case(client, session, closed):
    first = client.post("/signals", json=SIGNAL)
    session.get(Case, first.json()["case_id"]).status = closed
    session.commit()

    second = client.post("/signals", json=changed("signal_id", "sig_552"))

    assert second.status_code == 201
    assert second.json()["result"] == "opened"
    assert second.json()["case_id"] != first.json()["case_id"]
    assert count(session, Case) == 2


def test_signals_for_two_customers_open_two_cases(client, session):
    other = {**SIGNAL, "signal_id": "sig_552", "customer_id": "c_008"}

    first = client.post("/signals", json=SIGNAL)
    second = client.post("/signals", json=other)

    assert second.status_code == 201
    assert first.json()["case_id"] != second.json()["case_id"]
    assert count(session, Case) == 2


def blind_once(monkeypatch, name: str) -> None:
    """Make the next call of a lookup see nothing, as if another request had not committed yet."""
    real = getattr(signals, name)
    first_call = [True]

    def lookup(*args):
        if first_call[0]:
            first_call[0] = False
            return None
        return real(*args)

    monkeypatch.setattr(signals, name, lookup)


def test_same_signal_stored_by_another_request_mid_flight_is_a_duplicate(
    client, session, monkeypatch
):
    first = client.post("/signals", json=SIGNAL)

    # The second request looks before the first has committed: it sees no signal and no open case.
    blind_once(monkeypatch, "_stored")
    blind_once(monkeypatch, "_open_case")
    second = client.post("/signals", json=SIGNAL)

    assert second.status_code == 200
    assert second.json() == {**first.json(), "result": "duplicate"}
    assert count(session, Case) == 1
    assert count(session, SignalRow) == 1


def test_same_signal_joining_twice_at_once_is_stored_once(client, session, monkeypatch):
    client.post("/signals", json=SIGNAL)
    first = client.post("/signals", json=changed("signal_id", "sig_552"))

    # A repeat of sig_552 sees the open case but not the stored signal, so it fails on commit.
    blind_once(monkeypatch, "_stored")
    second = client.post("/signals", json=changed("signal_id", "sig_552"))

    assert second.status_code == 200
    assert second.json() == {**first.json(), "result": "duplicate"}
    assert count(session, Case) == 1
    assert count(session, SignalRow) == 2


def test_case_opened_by_another_request_mid_flight_is_joined(client, session, monkeypatch):
    first = client.post("/signals", json=SIGNAL)

    # A different signal for the same customer looks before the first has committed.
    blind_once(monkeypatch, "_open_case")
    second = client.post("/signals", json=changed("signal_id", "sig_552"))

    assert second.status_code == 200
    assert second.json() == {**first.json(), "result": "joined"}
    assert count(session, Case) == 1
    assert count(session, SignalRow) == 2


@pytest.mark.parametrize(
    "signal",
    [
        pytest.param(without("signal_id"), id="no signal_id"),
        pytest.param(without("customer_id"), id="no customer_id"),
        pytest.param(without("as_of"), id="no as_of"),
        pytest.param(without("current_limit_cents"), id="no current limit"),
        pytest.param(without("engine_limit_cents"), id="no engine limit"),
        pytest.param(without("review_reasons"), id="no review reasons"),
        pytest.param(without("snapshot"), id="no snapshot"),
        pytest.param(without("snapshot.card"), id="snapshot without card"),
        pytest.param(without("snapshot.bank.cash_30d_avg_cents"), id="no 30-day average"),
        pytest.param(without("snapshot.bank.accounts"), id="no linked accounts field"),
        pytest.param(without("snapshot.card.balance_cents"), id="no card balance"),
        pytest.param(changed("signal_id", ""), id="empty signal_id"),
        pytest.param(changed("customer_id", "010"), id="customer_id not c_NNN"),
        pytest.param(changed("customer_id", "c_٠١٠"), id="customer_id with non-ASCII digits"),
        pytest.param(changed("as_of", "last Tuesday"), id="as_of not a date"),
        pytest.param(changed("as_of", 1731196800), id="as_of as a timestamp"),
        pytest.param(changed("as_of", "1731196800"), id="as_of as a string of digits"),
        pytest.param(changed("as_of", 0), id="as_of zero"),
        pytest.param(changed("as_of", "2026-11-10T09:00:00Z"), id="as_of with a time"),
        pytest.param(changed("review_reasons", []), id="empty review reasons"),
        pytest.param(changed("review_reasons", ["cash_looks_low"]), id="unknown review reason"),
        pytest.param(
            changed("review_reasons", ["large_cut", "large_cut"]), id="review reason twice"
        ),
        pytest.param(changed("engine_limit_cents", -1), id="negative limit"),
        pytest.param(changed("engine_limit_cents", "5800000"), id="limit as a string"),
        pytest.param(changed("engine_limit_cents", True), id="limit as a boolean"),
        pytest.param(changed("engine_limit_cents", 5800000.5), id="limit with a fraction"),
        pytest.param(changed("snapshot.bank.accounts", []), id="no linked account"),
        pytest.param(
            changed("snapshot.bank.accounts", [{"id": "acct_c010_a"}]), id="account without sync date"
        ),
        pytest.param(
            changed(
                "snapshot.card.repayments",
                [{"due": "2026-11-05", "amount_cents": 5000000, "status": "bounced"}],
            ),
            id="unknown repayment status",
        ),
        pytest.param(
            changed("snapshot.bank.accounts", [{"id": "acct_c010_a", "last_synced": 1731196800}]),
            id="sync date as a timestamp",
        ),
        pytest.param(
            changed(
                "snapshot.bank.accounts",
                [{"id": "acct_c010_a", "last_synced": "2026-11-10", "bank": "Chase"}],
            ),
            id="unknown field in an account",
        ),
        pytest.param(
            changed(
                "snapshot.card.repayments",
                [{"due": 1731196800, "amount_cents": 5000000, "status": "failed"}],
            ),
            id="due date as a timestamp",
        ),
        pytest.param(
            changed(
                "snapshot.card.repayments",
                [{"due": "2026-11-05", "amount_cents": 5000000, "status": "failed", "why": "nsf"}],
            ),
            id="unknown field in a repayment",
        ),
        pytest.param(changed("snapshot.cause", "payroll"), id="a cause inside the snapshot"),
        pytest.param({**SIGNAL, "type": "balance_drop"}, id="field from the old shape"),
        pytest.param({**SIGNAL, "cause": "payroll"}, id="a cause"),
    ],
)
def test_malformed_signal_is_refused_and_stores_nothing(client, session, signal):
    response = client.post("/signals", json=signal)

    assert response.status_code == 422
    assert count(session, Case) == 0
    assert count(session, SignalRow) == 0


def test_overdrawn_account_and_no_repayment_history_are_accepted(client):
    signal = changed("snapshot.bank.cash_now_cents", -150000)
    signal["snapshot"]["card"]["repayments"] = []

    assert client.post("/signals", json=signal).status_code == 201


def test_extra_snapshot_numbers_are_kept(client, session):
    signal = changed("snapshot.card.utilization_pct", 92)

    client.post("/signals", json=signal)

    assert session.get(SignalRow, "sig_551").payload["snapshot"]["card"]["utilization_pct"] == 92
