import os
import tempfile

import pytest

# The app builds its engine at import time, so the URL must be set first.
# Tests run on a throwaway SQLite file. Postgres is exercised by running the stack.
_db_dir = tempfile.mkdtemp(prefix="card-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_dir}/test.db"
# The lifespan refuses to start without a secret; tests sign their own tokens with this one.
os.environ["JWT_SECRET"] = "test-jwt-secret-not-used-anywhere-real"
os.environ.pop("AS_OF_DATE", None)
# People's tokens are HS256 here. test_auth0 sets these itself.
os.environ.pop("AUTH0_DOMAIN", None)
os.environ.pop("AUTH0_AUDIENCE", None)

import time  # noqa: E402

import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402


@event.listens_for(engine, "connect")
def _enforce_foreign_keys(dbapi_connection, _record):
    # SQLite ignores foreign keys unless told otherwise. Postgres never does.
    dbapi_connection.execute("PRAGMA foreign_keys=ON")


@pytest.fixture
def client():
    Base.metadata.drop_all(engine)
    with TestClient(app) as test_client:  # the lifespan creates the tables and seeds
        yield test_client


@pytest.fixture
def session():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db_session:
        yield db_session


@pytest.fixture(scope="module")
def seeded_client():
    """One seeded database for a whole module of read-only tests."""
    Base.metadata.drop_all(engine)
    with TestClient(app) as test_client:
        yield test_client


def mint_token(*, sub: str = "test", role: str = "service", customers: list[str] | None = None,
               scope: list[str] | str | None = None, expires_in: int = 3600, secret: str | None = None) -> str:
    """A token like the Orchestrator's (sub, role, customers), with Card's optional scope claim."""
    claims = {"sub": sub, "role": role, "customers": customers or [], "exp": int(time.time()) + expires_in}
    if scope is not None:
        claims["scope"] = scope
    return jwt.encode(claims, secret or os.environ["JWT_SECRET"], algorithm="HS256")


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def snapshot_headers():
    return bearer(mint_token(sub="detection-job", scope=["snapshots:read"]))
