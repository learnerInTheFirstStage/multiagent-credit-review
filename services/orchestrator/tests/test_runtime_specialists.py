"""Which specialists the running service asks (KAN-36)."""

import pytest

from app import runtime
from app.agents import mock
from app.agents.card_service import card_service
from app.agents.cash_flow import cash_flow


@pytest.mark.parametrize("value", [None, "", "real", "something else"])
def test_the_service_asks_the_real_specialists_by_default(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SPECIALISTS", raising=False)
    else:
        monkeypatch.setenv("SPECIALISTS", value)

    assert runtime.specialists() == {"cash_flow": cash_flow, "credit_behavior": card_service}


def test_stand_in_asks_the_stand_ins(monkeypatch):
    monkeypatch.setenv("SPECIALISTS", "stand_in")

    assert runtime.specialists() == {"cash_flow": mock.cash_flow, "credit_behavior": mock.credit_behavior}
