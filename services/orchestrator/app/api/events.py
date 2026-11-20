"""Server-sent events for the review page.

Browsers' built-in EventSource cannot set an Authorization header, so the token
comes in as a query string parameter (`?token=...`). We validate it with the
same HS256/RS256 rules as `current_user`, filter events to the token's
customers, and stream `case.updated` events as a case's row changes.

The stream is a per-connection poll of the cases table (every POLL_SECONDS):
simple, no shared state, zero changes to signals.py / runner.py / graph. If a
later team needs true push, this is the single place to swap in a pub/sub bus.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os

import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import User, _person, auth0
from app.db import SessionLocal
from app.db.models import Case, OutageWait, SignalRow

log = logging.getLogger(__name__)
router = APIRouter()

POLL_SECONDS = float(os.environ.get("SSE_POLL_SECONDS", "0.5"))
# After this long with no event, send a comment heartbeat so the browser's
# built-in reconnect does not mistake a quiet connection for a dead one.
HEARTBEAT_SECONDS = float(os.environ.get("SSE_HEARTBEAT_SECONDS", "15"))


def _user_from_token(token: str) -> User:
    """Validate a JWT passed as the SSE query parameter and build a User.

    Mirrors app.auth.current_user but reads from a string rather than the
    Authorization header, since EventSource cannot set headers.
    """
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "JWT_SECRET is not set")
    tenant = auth0()
    refused = HTTPException(status.HTTP_401_UNAUTHORIZED, "A valid bearer token is required")
    try:
        if tenant and jwt.get_unverified_header(token).get("alg") == "RS256":
            claims = _person(token, *tenant)
        else:
            claims = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
            if tenant and claims.get("role") != "service":
                raise refused
        return User.model_validate({**claims, "token": token})
    except jwt.PyJWKClientConnectionError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Auth0's signing keys cannot be fetched") from None
    except (jwt.InvalidTokenError, jwt.PyJWKClientError, ValidationError):
        raise refused from None


def _sse_user(token: str = Query(...)) -> User:
    """Dependency that reads the token from the ?token=... query and returns a User."""
    return _user_from_token(token)


def _outage_message(wait: OutageWait | None) -> dict | None:
    if wait is None:
        return None
    return {
        "service": wait.service,
        "codes": wait.codes,
        "kind": wait.kind,
        "attempts": wait.attempts,
        "since": wait.first_seen.isoformat(),
        "next_retry_at": wait.next_retry_at.isoformat() if wait.next_retry_at else None,
        "message": (
            f"{wait.service} is unreachable ({wait.codes}). "
            f"Attempts: {wait.attempts}. Last try: {wait.first_seen.isoformat()}"
        ),
    }


def _row_signature(row: Case, wait: OutageWait | None) -> tuple:
    """Everything the UI's queue row depends on, in a value shape we can compare across polls."""
    return (
        row.status,
        row.updated_at,
        None if wait is None else (wait.codes, wait.attempts, wait.next_retry_at),
    )


def _row_payload(row: Case, wait: OutageWait | None, reasons: list[str]) -> dict:
    return {
        "case_id": row.case_id,
        "customer_id": row.customer_id,
        "status": row.status,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "outage": _outage_message(wait),
        "signal_reasons": reasons,
    }


def _reasons_for(session: Session, case_ids: list[str]) -> dict[str, list[str]]:
    """Distinct review_reasons per case in arrival order. Mirrors cases.py's helper."""
    if not case_ids:
        return {}
    reasons: dict[str, list[str]] = {}
    for signal in session.execute(
        select(SignalRow)
        .where(SignalRow.case_id.in_(case_ids))
        .order_by(SignalRow.received_at, SignalRow.signal_id)
    ).scalars():
        seen = reasons.setdefault(signal.case_id, [])
        for reason in signal.payload.get("review_reasons") or []:
            if reason not in seen:
                seen.append(reason)
    return reasons


def _scan(customers: set[str]) -> dict[str, tuple[tuple, dict]]:
    """A snapshot of every case this token may see, keyed by case_id.

    Includes open and closed cases: a `done` or `rejected` case is still of
    interest because the decision just happened. Each value is (signature, payload):
    the UI compares signatures to tell if anything changed. Session is opened and
    closed inside this call so a long-running stream does not hold a connection.
    """
    if not customers:
        return {}
    with contextlib.closing(SessionLocal()) as session:
        rows = list(session.execute(
            select(Case, OutageWait)
            .outerjoin(OutageWait, OutageWait.case_id == Case.case_id)
            .where(Case.customer_id.in_(customers))
        ))
        reasons = _reasons_for(session, [row.case_id for row, _ in rows])
        return {
            row.case_id: (_row_signature(row, wait), _row_payload(row, wait, reasons.get(row.case_id, [])))
            for row, wait in rows
        }


async def _stream(request: Request, user: User):
    """One generator per connection. Polls the cases table and yields `case.updated` events.

    The first scan is taken as a silent baseline: it fills `previous` without
    yielding events. Otherwise every connect would emit one event per visible
    case (open and closed), each of which cancels any in-flight `GET /cases` on
    the client — wasteful, and surprising for the reviewer who just signed in.
    The frontend already does an initial `GET` on page load; the stream only
    needs to surface *changes* from that point on.
    """
    customers = set(user.customers)
    previous: dict[str, tuple] = {}
    baseline_taken = False
    last_heartbeat = asyncio.get_running_loop().time()
    log.info("sse: open sub=%s role=%s customers=%d", user.sub, user.role, len(customers))

    # A short preamble so the browser treats the connection as established right away.
    yield b":ok\n\n"

    while True:
        if await request.is_disconnected():
            log.info("sse: client disconnected sub=%s", user.sub)
            return

        # Run the sync DB query off the event loop so other requests can be served while we wait on it.
        try:
            snapshot = await asyncio.to_thread(_scan, customers)
        except Exception:
            # A DB blip (connection pool exhausted, Postgres restart) used to end
            # the generator, which triggered the browser's auto-reconnect and a
            # tight retry loop against a database that is still down. Log and
            # back off a little instead; the next tick picks it up cleanly.
            log.exception("sse: scan failed, backing off")
            await asyncio.sleep(max(POLL_SECONDS, 2))
            continue

        if not baseline_taken:
            previous = {case_id: signature for case_id, (signature, _) in snapshot.items()}
            baseline_taken = True
        else:
            for case_id, (signature, payload) in snapshot.items():
                if previous.get(case_id) != signature:
                    previous[case_id] = signature
                    message = f"event: case.updated\ndata: {json.dumps(payload)}\n\n"
                    yield message.encode()

        now = asyncio.get_running_loop().time()
        if now - last_heartbeat >= HEARTBEAT_SECONDS:
            yield b":heartbeat\n\n"
            last_heartbeat = now

        await asyncio.sleep(POLL_SECONDS)


@router.get("/cases/events")
async def cases_events(request: Request, user: User = Depends(_sse_user)) -> StreamingResponse:
    """Server-sent events. One generator per connection, polling the cases table.

    EventSource has no header support, so the token comes as ?token=... The response
    is `text/event-stream`; nginx is set to not buffer it (X-Accel-Buffering: no).
    """
    return StreamingResponse(
        _stream(request, user),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Gateway nginx otherwise buffers the whole response; a streamed body would wait for close.
            "X-Accel-Buffering": "no",
        },
    )
