import copy
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
TEAM_FILE = REPO / "data" / "shared_customers.yaml"
EXTRA_FILE = REPO / "eval" / "extra_customers.yaml"

# The app builds its engine at import time, so the URL must be set first. Tests run on a
# throwaway SQLite file, or on the database BANK_TEST_DATABASE_URL names (an empty, throwaway
# Postgres: every test drops and recreates the tables).
_db_dir = tempfile.mkdtemp(prefix="bank-tests-")
os.environ["DATABASE_URL"] = os.environ.get("BANK_TEST_DATABASE_URL") or f"sqlite:///{_db_dir}/test.db"
# The lifespan refuses tokens nobody can check; tests need a secret set.
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-not-used-anywhere-real")
# Tests read the clock from the seeded files unless a test sets it.
os.environ.pop("AS_OF_DATE", None)
# The team's customers and the evaluation's: every customer the stand-in answers for.
os.environ["SHARED_CUSTOMERS_YAML"] = os.pathsep.join([str(TEAM_FILE), str(EXTRA_FILE)])

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402

if engine.dialect.name == "sqlite":

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        # SQLite ignores foreign keys unless told otherwise. Postgres never does.
        dbapi_connection.execute("PRAGMA foreign_keys=ON")


def _customer_ids() -> list[str]:
    return [customer["id"] for path in (TEAM_FILE, EXTRA_FILE) for customer in yaml.safe_load(path.read_text())["customers"]]


ALL_CUSTOMERS = _customer_ids()


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
    return {"Authorization": f"Bearer {mint_token(customers=ALL_CUSTOMERS)}"}


def call(client, headers, customer_id: str, tool: str, **params):
    """GET one tool. Returns the response; params left as None are not sent."""
    sent = {key: str(value) for key, value in params.items() if value is not None}
    return client.get(f"/customers/{customer_id}/tools/{tool}", params=sent, headers=headers)


def evidence_by_id(body: dict) -> dict[str, dict]:
    return {record["id"]: record for record in body["evidence"]}


@pytest.fixture
def seeded_with(monkeypatch):
    """A client on a database seeded from the files given, instead of the usual two."""

    @contextmanager
    def seeded(*paths):
        monkeypatch.setenv("SHARED_CUSTOMERS_YAML", os.pathsep.join(str(path) for path in paths))
        Base.metadata.drop_all(engine)
        with TestClient(app) as test_client:
            yield test_client

    return seeded


def write_customers(folder: Path, name: str, customers: list[dict], clock: dict | None = None, notes: dict | None = None) -> Path:
    """A data file in the shared file's shape."""
    data = {"clock": clock or {"signal_date": "2026-11-10", "signal_window_days": 7, "trailing_window_weeks": 13}}
    data["customers"] = customers
    if notes:
        data["data_integrity"] = notes
    path = folder / name
    path.write_text(yaml.safe_dump(data))
    return path


def team_customer(cid: str) -> dict:
    """A copy of one customer of the two data files."""
    for path in (TEAM_FILE, EXTRA_FILE):
        for customer in yaml.safe_load(path.read_text())["customers"]:
            if customer["id"] == cid:
                return copy.deepcopy(customer)
    raise KeyError(cid)
