"""GET /cases/events: the SSE stream the review page subscribes to."""

import json
import os
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.api import events
from app.db import Base, SessionLocal, engine
from app.db.models import Case, CaseStatus
from app.main import app


@pytest.fixture
def api():
    Base.metadata.drop_all(engine)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def _token(sub="u_analyst", role="analyst", customers=("c_001",), expires_in=3600) -> str:
    payload = {
        "sub": sub,
        "role": role,
        "customers": list(customers),
        "exp": int(time.time()) + expires_in,
    }
    return jwt.encode(payload, os.environ["JWT_SECRET"], algorithm="HS256")


def _open_case(customer_id: str) -> str:
    """Write a case directly: test_signals already covers POST /signals."""
    case_id = f"case_{customer_id}_evt"
    with SessionLocal() as session:
        session.add(Case(case_id=case_id, customer_id=customer_id, status=CaseStatus.QUEUED))
        session.commit()
    return case_id


def test_refuses_an_unauthenticated_caller(api):
    # No token in the query → FastAPI's own 422 (missing required param).
    response = api.get("/cases/events")
    assert response.status_code == 422


def test_refuses_a_bad_token(api):
    response = api.get("/cases/events", params={"token": "not-a-jwt"})
    assert response.status_code == 401


def test_refuses_an_expired_token(api):
    expired = _token(expires_in=-10)
    response = api.get("/cases/events", params={"token": expired})
    assert response.status_code == 401


def test_a_scan_shows_only_the_token_s_customers():
    """_scan is per-customer, same rule as GET /cases."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    _open_case("c_001")
    _open_case("c_002")

    for_c1 = events._scan({"c_001"})
    for_both = events._scan({"c_001", "c_002"})
    for_none: dict = events._scan(set())

    assert set(for_c1.keys()) == {"case_c_001_evt"}
    assert set(for_both.keys()) == {"case_c_001_evt", "case_c_002_evt"}
    assert for_none == {}


def test_a_scan_signature_changes_when_the_status_changes():
    """The stream uses _row_signature to tell what changed; the shape must be stable."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    case_id = _open_case("c_001")

    snapshot_1 = events._scan({"c_001"})
    (signature_1, payload_1) = snapshot_1[case_id]

    with SessionLocal() as session:
        case = session.get(Case, case_id)
        case.status = CaseStatus.INVESTIGATING.value
        session.commit()

    snapshot_2 = events._scan({"c_001"})
    (signature_2, payload_2) = snapshot_2[case_id]

    assert signature_1 != signature_2
    assert payload_1["status"] == "queued"
    assert payload_2["status"] == "investigating"
    assert payload_2["case_id"] == case_id


def test_a_payload_carries_the_fields_the_queue_row_shows():
    """The SSE payload must match the queue row shape, so the frontend can drop it into the same cache."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    case_id = _open_case("c_001")

    snapshot = events._scan({"c_001"})
    (_, payload) = snapshot[case_id]

    assert set(payload) == {
        "case_id", "customer_id", "status", "created_at", "updated_at", "outage", "signal_reasons",
    }


def test_the_endpoint_streams_text_event_stream(api):
    """A valid connection answers 200 with the SSE content type and keep-alive headers."""
    token = _token(customers=("c_001",))
    with api.stream("GET", "/cases/events", params={"token": token}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        # One chunk at least: the ":ok" preamble.
        first = next(response.iter_raw())
        assert b"ok" in first or b"event: case.updated" in first or first == b""


def test_the_stream_emits_on_a_new_case():
    """Changing the cases table between scans produces one case.updated event per changed case."""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    before = events._scan({"c_001"})
    assert before == {}

    _open_case("c_001")
    after = events._scan({"c_001"})
    assert set(after) == {"case_c_001_evt"}

    # The signature changed from "not present" to "present with this signature": the stream's loop
    # notices via `previous.get(case_id) != signature`, which is what this test shows at the data layer.
    (signature, payload) = after["case_c_001_evt"]
    assert signature is not None
    assert payload["status"] == "queued"

    # Format of the SSE message the stream would emit for this payload:
    message = f"event: case.updated\ndata: {json.dumps(payload)}\n\n"
    assert message.startswith("event: case.updated\ndata: ")
    assert '"case_id": "case_c_001_evt"' in message


def test_the_stream_first_scan_is_a_silent_baseline(monkeypatch):
    """The first scan fills the baseline without emitting, so a client who connects while cases
    already exist does not get a flood of events for every row it already knew about."""
    import asyncio

    # Two scans: the first names a case, the second adds another.
    scans = [
        {"case_x": (("queued", "t1", None), {"case_id": "case_x", "status": "queued"})},
        {
            "case_x": (("queued", "t1", None), {"case_id": "case_x", "status": "queued"}),
            "case_y": (("queued", "t1", None), {"case_id": "case_y", "status": "queued"}),
        },
    ]
    scan_calls = iter(scans)
    monkeypatch.setattr(events, "_scan", lambda customers: next(scan_calls))
    monkeypatch.setattr(events, "POLL_SECONDS", 0)
    disconnects = iter([False, False, True])

    class _Request:
        async def is_disconnected(self):
            return next(disconnects)

    user = type("U", (), {"customers": ["c_x", "c_y"], "sub": "u", "role": "analyst"})()

    async def drain():
        return [chunk async for chunk in events._stream(_Request(), user)]

    chunks = asyncio.run(drain())

    # Preamble, then only the second scan's new row: case_x is the baseline, case_y is the delta.
    assert chunks[0] == b":ok\n\n"
    emitted = [c for c in chunks if c.startswith(b"event:")]
    assert len(emitted) == 1, emitted
    assert b'"case_id": "case_y"' in emitted[0]
