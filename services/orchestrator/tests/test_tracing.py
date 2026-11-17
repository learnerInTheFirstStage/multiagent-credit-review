import json
from contextlib import contextmanager
from uuid import uuid4

import pytest
from langfuse import get_client
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import card_client, runner, runtime
from tests.test_cases import FakeCard, graph_on
from tests.test_graph import INVESTIGATION, report, scripted
from tests.test_signals import SIGNAL  # c_010

TOKEN = "the-approvers-own-token"
STEPS = {
    "open_case",
    "investigate",
    "ask_specialist",
    "look_for_gaps",
    "fetch_guidelines",
    "draft_report",
    "check_report",
    "analyst_approval",
}


class Sent:
    """What LangFuse would be sent. Kept in memory: nothing leaves the test."""

    def __init__(self, public_key: str, exporter: InMemorySpanExporter, exporter_settings: list[dict]):
        self._public_key, self._exporter = public_key, exporter
        # How the service's own LangFuse client set up its exporter. Empty until it has made one.
        self.exporter_settings = exporter_settings

    @property
    def spans(self) -> list:
        get_client(public_key=self._public_key).flush()
        return list(self._exporter.get_finished_spans())

    @property
    def everything(self) -> str:
        return json.dumps(
            [[span.name, dict(span.attributes), [dict(event.attributes) for event in span.events]] for span in self.spans],
            default=str,
        )

    def traces(self) -> list[dict]:
        """One entry per run of the graph, oldest first: the attributes of its root."""
        roots = sorted((span for span in self.spans if span.parent is None), key=lambda span: span.start_time)
        return [{"name": root.name, **dict(root.attributes)} for root in roots]


@pytest.fixture
def langfuse(monkeypatch) -> Sent:
    """LangFuse configured through the environment, as in the running service.

    No client is made here. The service has to make its own, and when it does, the
    exporter that would send its spans over the network is swapped for one in memory.
    """
    # A key of its own per test: LangFuse keeps one client per key for the life of the process.
    public_key = f"pk-lf-test-{uuid4().hex}"
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", public_key)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://langfuse.test:3000")
    exporter, settings = InMemorySpanExporter(), []

    def in_memory(**kwargs):
        settings.append(kwargs)
        return exporter

    monkeypatch.setattr("langfuse._client.span_processor.OTLPSpanExporter", in_memory)
    return Sent(public_key, exporter, settings)


def service_graph(monkeypatch, script):
    """The graph exactly as the service assembles it, on a scripted model and a checkpointer in memory."""

    @contextmanager
    def in_memory():
        yield InMemorySaver()

    monkeypatch.setattr(runtime, "_chat_model", lambda: scripted(script))
    monkeypatch.setattr(runtime, "postgres_checkpointer", in_memory)
    return runtime.case_graph()


# The running service has no guidelines yet, so the only report that passes its check recommends nothing.
NOTHING_TO_DO = report(likely_cause="no_clear_cause", recommendation="needs_human", guideline_ids=[], evidence=[])


@pytest.mark.parametrize(
    "keys",
    [
        pytest.param({}, id="no keys"),
        pytest.param({"LANGFUSE_PUBLIC_KEY": "", "LANGFUSE_SECRET_KEY": ""}, id="empty keys, as compose passes them"),
        pytest.param({"LANGFUSE_PUBLIC_KEY": "pk-lf-only-half"}, id="only one key"),
    ],
)
def test_without_both_keys_cases_run_untraced(monkeypatch, keys):
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in keys.items():
        monkeypatch.setenv(name, value)

    with runtime.tracing() as callbacks:
        assert callbacks == []
    with service_graph(monkeypatch, [*INVESTIGATION, NOTHING_TO_DO]) as graph:
        state = graph.invoke({"case_id": "case_untraced", "signal": SIGNAL}, runner.thread("case_untraced"))
    assert state["status"] == "needs_approval"


def test_tracing_goes_to_the_langfuse_project_named_in_the_environment(langfuse):
    assert langfuse.exporter_settings == []

    with runtime.tracing() as callbacks:
        assert len(callbacks) == 1
        [settings] = langfuse.exporter_settings  # the service made one client, and it sends

    assert settings["endpoint"].startswith("http://langfuse.test:3000/")


def test_a_case_is_one_session_with_a_trace_per_run_and_every_step_in_it(langfuse, monkeypatch):
    case_id = f"case_{uuid4().hex[:8]}"

    with service_graph(monkeypatch, [*INVESTIGATION, NOTHING_TO_DO]) as graph:
        graph.invoke({"case_id": case_id, "signal": SIGNAL}, runner.thread(case_id))
        state = graph.invoke(
            Command(resume={"decision": "approve", "by": "u_analyst"}),
            runner.thread(case_id, user_id="u_analyst"),
            context={"approver_token": TOKEN},
        )
        investigation, decision = langfuse.traces()

    assert state["status"] == "done"
    assert investigation["name"] == decision["name"] == "credit-review-case"
    assert investigation["session.id"] == decision["session.id"] == case_id
    assert "user.id" not in investigation  # nobody decided anything in the investigation
    assert decision["user.id"] == "u_analyst"
    assert all(dict(span.attributes).get("session.id") == case_id for span in langfuse.spans)
    assert STEPS <= {span.name for span in langfuse.spans}
    model_calls = [span for span in langfuse.spans if dict(span.attributes).get("langfuse.observation.type") == "generation"]
    assert len(model_calls) == len(INVESTIGATION) + 1  # every question, the gap review and the report


def test_what_is_traced_holds_the_case_but_never_the_approvers_token(langfuse, monkeypatch):
    fake_card = FakeCard()
    with runtime.tracing() as callbacks:
        graph = graph_on([*INVESTIGATION, report()], fake_card).with_config({"callbacks": callbacks})
        graph.invoke({"case_id": "case_traced", "signal": SIGNAL}, runner.thread("case_traced"))
        graph.invoke(
            Command(resume={"decision": "approve", "by": "u_analyst"}),
            runner.thread("case_traced", user_id="u_analyst"),
            context={"approver_token": TOKEN},
        )
        sent = langfuse.everything

    assert [token for _, token in fake_card.calls] == [TOKEN]  # the token was used
    assert "act" in {span.name for span in langfuse.spans}
    assert "acct_c010_a" in sent  # the search does find what is traced
    assert TOKEN not in sent


def test_a_failed_call_to_card_is_traced_without_the_token(langfuse, monkeypatch):
    fake_card = FakeCard()
    fake_card.is_down = True
    with runtime.tracing() as callbacks:
        graph = graph_on([*INVESTIGATION, report()], fake_card).with_config({"callbacks": callbacks})
        graph.invoke({"case_id": "case_card_down", "signal": SIGNAL}, runner.thread("case_card_down"))
        with pytest.raises(card_client.CardError):
            graph.invoke(
                Command(resume={"decision": "approve", "by": "u_analyst"}),
                runner.thread("case_card_down", user_id="u_analyst"),
                context={"approver_token": TOKEN},
            )
        sent = langfuse.everything

    assert "the Card Service did not answer" in sent  # the failure is there to see
    assert TOKEN not in sent
