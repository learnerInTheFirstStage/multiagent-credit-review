"""What the running service builds its case graph from. Settings are read here and nowhere else."""

import functools
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel
from langfuse import Langfuse
from langfuse.langchain import CallbackHandler
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app import card_client, guidelines, history, retrieval
from app.agents import mock
from app.db import engine
from app.graph import build_graph, plans

log = logging.getLogger(__name__)

# Where the image keeps the seeded reviews. The evaluation mounts its own files and names both.
HISTORY_SEED_DEFAULT = "/eval/history.yaml"
# Who chooses the questions (8.4): the model, a written plan, or the plan and then code's own gap list.
FLOWS = ("agent", "fixed", "fixed_rules")


@contextmanager
def case_graph() -> Iterator:
    """The graph cases run on, or None while no model is configured."""
    model = _chat_model()
    if model is None:
        log.warning("LLM_MODEL is not set. Signals are stored and their cases wait as queued.")
        yield None
        return
    flow = _flow()
    # Only the agent flow searches: in the other two, code asks every question and no model investigates.
    embedder = retrieval.load(os.environ.get("EMBEDDINGS_MODEL")) if flow == "agent" else None
    with postgres_checkpointer() as checkpointer, tracing() as callbacks:
        graph = build_graph(
            model=model,
            # Stand-ins until the real specialists can be called (KAN-36).
            specialists={"cash_flow": mock.cash_flow, "credit_behavior": mock.credit_behavior},
            load_guidelines=guidelines.load,
            carry_out=card_client.carry_out,
            checkpointer=checkpointer,
            structured_output=_structured_output(),
            # Every earlier review, not the ten newest: the counts of the history summary cover them all.
            earlier_reviews=functools.partial(history.earlier_reviews, limit=None),
            search_reviews=retrieval.searcher(embedder) if embedder is not None else None,
            plan=None if flow == "agent" else plans.FIXED_PLAN,
            follow_gaps=flow == "fixed_rules",
        )
        yield graph.with_config({"callbacks": callbacks})


def _flow() -> str:
    """INVESTIGATION_FLOW, one of FLOWS. Unset, empty or a name it does not know: the agent flow, with one warning."""
    flow = os.environ.get("INVESTIGATION_FLOW")
    if flow in FLOWS:
        return flow
    if flow:
        log.warning("INVESTIGATION_FLOW %s is not a flow this service knows. Cases run on the agent flow.", flow)
    else:
        log.warning("INVESTIGATION_FLOW is not set. Cases run on the agent flow.")
    return "agent"


def history_seed_files() -> list[str]:
    """The files the service seeds closed reviews from at start.

    HISTORY_SEED_FILE lists one path or several separated by ":", as SHARED_CUSTOMERS_FILE does. Empty
    names are ignored. Unset, or naming no file at all, it is the file the image holds.
    """
    named = [path for path in os.environ.get("HISTORY_SEED_FILE", "").split(":") if path]
    return named or [HISTORY_SEED_DEFAULT]


@contextmanager
def tracing() -> Iterator[list]:
    """LangFuse callbacks for every run of the graph, or none while its keys are not set.

    Set once, here. The specialists run inside the graph, so their model calls land
    in the same trace with no tracing code of their own, as long as they call the
    model through LangChain. LangFuse reads LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY
    and LANGFUSE_BASE_URL itself.
    """
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    if not (public_key and os.environ.get("LANGFUSE_SECRET_KEY")):
        log.warning("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not set. Cases run without tracing.")
        yield []
        return
    # The client comes first: a handler made for a key that has no client yet is switched off.
    client = Langfuse()  # reads the three LANGFUSE_ settings itself
    handler = CallbackHandler(public_key=public_key)
    try:
        yield [handler]
    finally:
        # Sends what is still queued. If LangFuse does not answer, this gives up after a few seconds.
        client.shutdown()


def _chat_model() -> BaseChatModel | None:
    """LLM_MODEL is "provider:model". The provider's package has to be installed."""
    name = os.environ.get("LLM_MODEL")
    if not name:
        return None
    # A fixed limit, enough for the model's reasoning and a full report. Left out, LangChain's Anthropic
    # default is the model's maximum, which Anthropic's SDK refuses for a call that does not stream.
    settings = {"max_tokens": 16000}
    if api_key := os.environ.get("LLM_API_KEY"):
        settings["api_key"] = api_key
    return init_chat_model(name, **settings)


def _structured_output() -> dict:
    """How the model is asked for the gap review and the report.

    Empty means a tool call, which every provider has. LLM_STRUCTURED_OUTPUT names a
    provider's own method instead, where it has one: json_schema on Anthropic and OpenAI.
    """
    method = os.environ.get("LLM_STRUCTURED_OUTPUT")
    return {"method": method} if method else {}


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
