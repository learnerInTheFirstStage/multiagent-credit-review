"""POST /customers/{id}/actions under real row locks. SQLite has no FOR UPDATE, so this runs only
against a throwaway Postgres named by CARD_PG_URL, e.g.

    CARD_PG_URL=postgresql+psycopg://postgres:pw@127.0.0.1:55432/postgres pytest tests/test_actions_postgres.py
"""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db import Base, get_session
from app.db.models import Customer
from app.db.seed import seed_if_empty
from app.main import app
from tests.test_actions import person, reduce

PG_URL = os.environ.get("CARD_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="CARD_PG_URL is not set")
THREADS = 10


@pytest.fixture
def pg():
    engine = create_engine(PG_URL, pool_size=THREADS + 2, max_overflow=0)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions() as session:
        seed_if_empty(session)

    def pg_session():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = pg_session
    try:
        yield engine, sessions
    finally:
        app.dependency_overrides.pop(get_session, None)
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_identical_reductions_at_once_apply_once_and_the_rest_are_already_applied(pg):
    engine, sessions = pg
    client = TestClient(app)
    for round_ in range(5):
        target = 9_500_000 - 100_000 * round_  # below the limit the round before, above half of it
        body = reduce(target, f"case_pg{round_}")
        # Hold the customer row so every copy passes the first lookup and queues on the lock.
        holder = engine.connect()
        holder.begin()
        holder.execute(text("SELECT 1 FROM customers WHERE customer_id = 'c_010' FOR UPDATE"))
        start = threading.Barrier(THREADS)

        def post(_):
            start.wait()
            return client.post("/customers/c_010/actions", json=body, headers=person("analyst"))

        with ThreadPoolExecutor(max_workers=THREADS) as pool:
            futures = [pool.submit(post, i) for i in range(THREADS)]
            time.sleep(0.5)
            holder.rollback()
            holder.close()
            answers = [f.result() for f in futures]
        codes = [a.status_code for a in answers]
        assert codes == [200] * THREADS, [(a.status_code, a.text) for a in answers if a.status_code != 200]
        assert sorted(a.json()["status"] for a in answers) == ["already_applied"] * (THREADS - 1) + ["applied"]
        with sessions() as session:
            assert session.get(Customer, "c_010").credit_limit_cents == target
