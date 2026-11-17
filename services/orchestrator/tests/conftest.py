import os
import tempfile
import time

import pytest

# The app builds its engine at import time, so the URL must be set first.
# Tests run on a throwaway SQLite file. Set TEST_DATABASE_URL to run them on Postgres,
# which also runs the tests that only mean something there.
_db_dir = tempfile.mkdtemp(prefix="orchestrator-tests-")
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{_db_dir}/test.db")
os.environ["JWT_SECRET"] = "a-secret-for-tests-that-is-long-enough"
os.environ.pop("LLM_MODEL", None)  # no test may reach a real model

import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402


ON_POSTGRES = engine.dialect.name == "postgresql"
needs_postgres = pytest.mark.skipif(not ON_POSTGRES, reason="set TEST_DATABASE_URL to a Postgres database")

if not ON_POSTGRES:

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        # SQLite ignores foreign keys unless told otherwise. Postgres never does.
        dbapi_connection.execute("PRAGMA foreign_keys=ON")


def bearer(sub="u_analyst", role="analyst", customers=("c_010",), expires_in=3600, secret=None, **claims) -> dict:
    """An Authorization header. A claim set to None is left out of the token.

    Tokens in a parametrize list are made when the tests are collected, so they last an hour.
    """
    payload = {"sub": sub, "role": role, "customers": customers, "exp": int(time.time()) + expires_in, **claims}
    payload = {
        name: list(value) if isinstance(value, tuple) else value
        for name, value in payload.items()
        if value is not None
    }
    return {"Authorization": f"Bearer {jwt.encode(payload, secret or os.environ['JWT_SECRET'], algorithm='HS256')}"}


def sender() -> dict:
    """What the signal generator sends with: a service token."""
    return bearer(sub="signal-generator", role="service", customers=None)


@pytest.fixture
def client():
    Base.metadata.drop_all(engine)
    with TestClient(app) as test_client:  # the lifespan creates the tables
        # Signals go out as the signal generator. Another Authorization header replaces this one,
        # but headers={} does not remove it: a test of a caller with no token needs a client of its own.
        test_client.headers.update(sender())
        yield test_client


@pytest.fixture
def session():
    with SessionLocal() as db_session:
        yield db_session
