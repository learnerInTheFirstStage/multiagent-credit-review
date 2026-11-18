"""Both specialists refuse the same limit and card actions and let the same readings through
(tests/_action_phrasings.py). The first five refused lines passed the Card filter before the fix."""

from __future__ import annotations

import pytest

from app.agents.card_service import guardrails as card
from app.agents.cash_flow import guardrails as cash
from tests._action_phrasings import ALLOWED, REFUSED

FILTERS = {"cash_flow": cash.recommends_an_action, "card_service": card.recommends_an_action}


@pytest.mark.parametrize("agent", sorted(FILTERS))
@pytest.mark.parametrize("text", REFUSED)
def test_an_action_is_refused(agent, text):
    assert FILTERS[agent](text)


@pytest.mark.parametrize("agent", sorted(FILTERS))
@pytest.mark.parametrize("text", ALLOWED)
def test_a_reading_or_a_fact_is_let_through(agent, text):
    assert not FILTERS[agent](text)
