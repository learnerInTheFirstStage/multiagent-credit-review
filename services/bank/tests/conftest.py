import os
import tempfile

import pytest

# The app builds its engine at import time, so the URL must be set first.
# Tests run on a throwaway SQLite file. Postgres is exercised by running the stack.
_db_dir = tempfile.mkdtemp(prefix="bank-tests-")
os.environ["DATABASE_URL"] = f"sqlite:///{_db_dir}/test.db"
# The lifespan refuses tokens nobody can check; tests need a secret set.
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-used-anywhere-real")

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


def mint_token(
    *,
    sub: str = "orchestrator",
    role: str = "service",
    customers: list[str] | None = None,
    expires_in: int = 3600,
) -> str:
    """Mint a JWT for tests. Same shape as orchestrator/app/dev_token.py."""
    import time

    import jwt

    claims = {
        "sub": sub,
        "role": role,
        "customers": customers or [],
        "exp": int(time.time()) + expires_in,
    }
    return jwt.encode(claims, os.environ["JWT_SECRET"], algorithm="HS256")


@pytest.fixture
def mint():
    return mint_token


@pytest.fixture
def auth_headers():
    """Service token covering every seeded customer. Most tests use this."""
    customers = [f"c_{i:03d}" for i in range(1, 13)]
    return {"Authorization": f"Bearer {mint_token(customers=customers)}"}
