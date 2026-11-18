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
# The service reads these at start. A test that wants another flow, the search tool or other data files
# sets its own.
os.environ.pop("INVESTIGATION_FLOW", None)
os.environ.pop("EMBEDDINGS_MODEL", None)
os.environ.pop("SHARED_CUSTOMERS_FILE", None)
# The service built in tests asks the stand-ins: no Bank or Card runs here. The real agents have their own tests.
os.environ["SPECIALISTS"] = "stand_in"
# Every start seeds reviews from this setting, and unset it names the image's file. A test that wants
# seeds sets its own; the rest must not pick up an exported setting or a /eval/history.yaml on the host.
os.environ["HISTORY_SEED_FILE"] = f"{_db_dir}/no-seeds.yaml"
# The service looks for due retries at start and then every so often. In tests only the look at start happens on its
# own: a test calls runner.retry_due itself, with the time it means.
os.environ["OUTAGE_CHECK_SECONDS"] = "3600"

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
        # Keep SQLite inside a transaction, as Postgres is, so a released savepoint never commits on its own.
        # Without it, a missing commit after history.record in runner.keep_missing_reviews goes unseen.
        dbapi_connection.autocommit = False


@pytest.fixture(autouse=True)
def tables_exist():
    # The service's graph reads earlier reviews from the database when a case opens. A test that builds
    # it without starting the app, which creates the tables, must find them there too.
    Base.metadata.create_all(engine)


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
