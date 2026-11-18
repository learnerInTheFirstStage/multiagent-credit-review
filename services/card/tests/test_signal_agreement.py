"""Card's /snapshots and the evaluation's signal agree on the last three repayments.

The signal (eval/signals.py) derives each repayment's due date, amount and status from the
customer files; /snapshots reads them from Card's ledger. For every customer in both files
the two must be the same, or an analyst sees one amount in the signal and another in Card.
"""

import importlib.util
import os
from pathlib import Path

import pytest

from app.db import Base, engine
from app.main import app
from tests.conftest import bearer, mint_token

ROOT = Path(__file__).resolve().parents[3]
FILES = (ROOT / "data" / "shared_customers.yaml", ROOT / "eval" / "extra_customers.yaml")

# eval/ is not a package; signals.py needs only PyYAML, so load it from its path.
_spec = importlib.util.spec_from_file_location("eval_signals", ROOT / "eval" / "signals.py")
signals = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(signals)


@pytest.fixture(scope="module")
def card_rows():
    """/snapshots for every customer of both files, from a database seeded with both."""
    from fastapi.testclient import TestClient

    before = os.environ.get("SHARED_CUSTOMERS_YAML")
    os.environ["SHARED_CUSTOMERS_YAML"] = ":".join(str(p) for p in FILES)
    try:
        Base.metadata.drop_all(engine)
        with TestClient(app) as client:
            response = client.get("/snapshots", headers=bearer(mint_token(scope="snapshots:read")))
            assert response.status_code == 200, response.text
            yield {row["customer_id"]: row for row in response.json()["customers"]}
    finally:
        if before is None:
            os.environ.pop("SHARED_CUSTOMERS_YAML", None)
        else:
            os.environ["SHARED_CUSTOMERS_YAML"] = before
        Base.metadata.drop_all(engine)


def test_signals_reads_the_same_files():
    assert signals.FILES == FILES


def test_every_customer_in_the_files_is_seeded(card_rows):
    _, customers = signals.customers()
    assert set(card_rows) == set(customers)


def test_repayments_equal_the_signals(card_rows):
    today, customers = signals.customers()
    mismatches = {}
    for cid, customer in sorted(customers.items()):
        expected = signals.signal_for(customer, today)["snapshot"]["card"]
        got = card_rows[cid]
        if got["repayments"] != expected["repayments"] or got["balance_cents"] != expected["balance_cents"]:
            mismatches[cid] = {"card": got["repayments"], "signal": expected["repayments"]}
    assert not mismatches, mismatches
