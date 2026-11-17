import os
import tempfile

import pytest

# The app builds its engine at import time, so the URL must be set first.
# Tests run on a throwaway SQLite file. Set TEST_DATABASE_URL to run them on Postgres,
# which also runs the tests that only mean something there.
_db_dir = tempfile.mkdtemp(prefix="orchestrator-tests-")
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", f"sqlite:///{_db_dir}/test.db")
os.environ["JWT_SECRET"] = "a-secret-for-tests-that-is-long-enough"
os.environ.pop("LLM_MODEL", None)  # no test may reach a real model

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


@pytest.fixture
def client():
    Base.metadata.drop_all(engine)
    with TestClient(app) as test_client:  # the lifespan creates the tables
        yield test_client


@pytest.fixture
def session():
    with SessionLocal() as db_session:
        yield db_session
