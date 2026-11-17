"""How the running service sets up its model."""

import os

import pytest

from app import runner, runtime
from tests import test_tracing
from tests.test_graph import INVESTIGATION, ScriptedModel, report, scripted
from tests.test_signals import SIGNAL  # c_010
from tests.test_tracing import service_graph


@pytest.fixture
def made(monkeypatch) -> list[tuple]:
    """Every model the service asks LangChain for, as (name, settings)."""
    calls = []
    monkeypatch.setattr(runtime, "init_chat_model", lambda name, **settings: calls.append((name, settings)) or "the model")
    for name in ("LLM_MODEL", "LLM_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return calls


def test_with_no_model_named_the_service_makes_none(made):
    assert runtime._chat_model() is None
    assert made == []


def test_the_model_gets_room_to_reason_and_the_key_when_there_is_one(made, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "anthropic:claude-sonnet-5-5")

    assert runtime._chat_model() == "the model"
    monkeypatch.setenv("LLM_API_KEY", "a-key")
    runtime._chat_model()

    without_key, with_key = made
    assert without_key == ("anthropic:claude-sonnet-5-5", {"max_tokens": 16000})  # no api_key=None for the provider to trip on
    assert with_key == ("anthropic:claude-sonnet-5-5", {"max_tokens": 16000, "api_key": "a-key"})


@pytest.mark.parametrize(
    ("setting", "method"),
    [
        pytest.param(None, None, id="not set: a tool call, which every provider has"),
        pytest.param("", None, id="empty, as compose passes it"),
        pytest.param("json_schema", "json_schema", id="the provider's own method"),
    ],
)
def test_the_gap_review_and_the_report_are_asked_for_the_way_the_service_is_set_up(monkeypatch, setting, method):
    asked = []

    class Recording(ScriptedModel):
        def with_structured_output(self, schema, **settings):
            asked.append((schema.__name__, settings.pop("method", None)))
            return super().with_structured_output(schema, **settings)

    monkeypatch.setattr(test_tracing, "scripted", lambda script: scripted(script, Recording))
    monkeypatch.delenv("LLM_STRUCTURED_OUTPUT", raising=False)
    if setting is not None:
        monkeypatch.setenv("LLM_STRUCTURED_OUTPUT", setting)
    nothing_to_do = report(recommendation="pause_card", guideline_ids=["G-08b"], confidence="low")

    with service_graph(monkeypatch, [*INVESTIGATION, nothing_to_do]) as graph:
        state = graph.invoke({"case_id": "case_set_up", "signal": SIGNAL}, runner.thread("case_set_up"))

    assert asked == [("GapReview", method), ("ReportDraft", method)]
    assert state["status"] == "needs_approval"


def test_the_suite_starts_with_no_flow_or_embedder_from_the_shell():
    # conftest drops both, so a shell that exports INVESTIGATION_FLOW=fixed cannot turn service tests into the fixed arm.
    assert "INVESTIGATION_FLOW" not in os.environ
    assert "EMBEDDINGS_MODEL" not in os.environ
