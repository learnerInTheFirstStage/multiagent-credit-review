"""What the running service builds its case graph from. Settings are read here and nowhere else."""

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app import card_client
from app.agents import mock
from app.db import engine
from app.graph import build_graph

log = logging.getLogger(__name__)


@contextmanager
def case_graph() -> Iterator:
    """The graph cases run on, or None while no model is configured."""
    model = _chat_model()
    if model is None:
        log.warning("LLM_MODEL is not set. Signals are stored and their cases wait as queued.")
        yield None
        return
    with postgres_checkpointer() as checkpointer:
        yield build_graph(
            model=model,
            # Stand-ins until the real specialists can be called (KAN-36).
            specialists={"cash_flow": mock.cash_flow, "credit_behavior": mock.credit_behavior},
            # No guidelines yet (KAN-33). Without one a report can only recommend needs_human.
            load_guidelines=lambda: [],
            carry_out=card_client.carry_out,
            checkpointer=checkpointer,
        )


def _chat_model() -> BaseChatModel | None:
    """LLM_MODEL is "provider:model". The provider's package has to be installed."""
    name = os.environ.get("LLM_MODEL")
    if not name:
        return None
    api_key = os.environ.get("LLM_API_KEY")
    return init_chat_model(name, **({"api_key": api_key} if api_key else {}))


@contextmanager
def postgres_checkpointer() -> Iterator[PostgresSaver]:
    """Graph state, saved in the service's own database after every step."""
    # The checkpointer talks to Postgres through psycopg, not through SQLAlchemy.
    conninfo = engine.url.set(drivername="postgresql").render_as_string(hide_password=False)
    # These three connection settings are the ones PostgresSaver sets for itself.
    settings = {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}
    with ConnectionPool(conninfo, kwargs=settings, open=False) as pool:
        checkpointer = PostgresSaver(pool)
        checkpointer.setup()  # creates its tables the first time
        yield checkpointer
