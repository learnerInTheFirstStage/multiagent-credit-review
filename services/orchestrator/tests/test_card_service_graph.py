"""The Card Service agent as a LangGraph graph: its steps, the paths runs take, and how it runs
inside the case graph (named steps in the caller's trace, nothing saved by the caller's checkpointer)."""

from __future__ import annotations

from typing import TypedDict

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph

from app.agents.card_service import card_service
from app.agents.card_service.agent import GRAPH
from contracts import InvestigateRequest
from tests._card_service_helpers import CardStub, ScriptedChatModel, answer, request

STEPS = {"preflight", "investigate", "tools", "summarise", "check", "repair", "write_from_records", "finalize"}
BALANCE = "bal_card_c_010_20261110"


class StepNames(BaseCallbackHandler):
    """Records the name of every run that starts: what a tracer such as LangFuse sees."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def on_chain_start(self, serialized, inputs, **kwargs) -> None:  # noqa: ANN001
        self.names.append(kwargs.get("name") or (serialized or {}).get("name"))


def steps_of(customer: str, script: list | None, topic: str | None = None, stub: CardStub | None = None) -> list[str]:
    """The agent's node names, in the order they ran, for one call."""
    handler = StepNames()
    model = ScriptedChatModel(script=script) if script is not None else None
    http = (stub or CardStub(customer)).client()

    class Outer(TypedDict, total=False):
        done: bool

    def ask(_state: Outer) -> Outer:
        card_service(InvestigateRequest(**request(customer, topic)), model=model, http=http)
        return {"done": True}

    outer = StateGraph(Outer)
    outer.add_node("ask_specialist", ask)
    outer.add_edge(START, "ask_specialist")
    outer.compile().invoke({}, config={"callbacks": [handler]})
    return [n for n in handler.names if n in STEPS]


def test_the_graph_has_the_named_steps():
    assert set(GRAPH.get_graph().nodes) == STEPS | {"__start__", "__end__"}
    assert GRAPH.name == "card_service"


def test_a_model_run_goes_preflight_investigate_tools_check_finalize():
    path = steps_of("c_003", [
        [("card_spend_breakdown", {"start": "2026-10-11", "end": "2026-11-10", "by": "category"})],
        answer(("Spend in the cycle was 91% above the customer's usual.", ["agg_af9486b9"])),
    ], topic="card_use")
    assert path == ["preflight", "investigate", "tools", "investigate", "check", "finalize"]


def test_a_failed_check_goes_through_one_repair():
    path = steps_of("c_010", [
        answer(("Utilization is spiking.", ["bal_card_c_010_made_up"])),
        answer(("Utilization is spiking.", [BALANCE])),
    ], topic="card_use")
    assert path == ["preflight", "investigate", "check", "repair", "check", "finalize"]


def test_the_budget_ends_in_summarise():
    calls = [[("card_transactions", {"start": f"2026-10-0{d}", "end": "2026-11-10"})] for d in range(1, 7)]
    path = steps_of("c_003", [*calls, answer()], topic="repayments")
    assert path.count("tools") == 6 and path[-3:] == ["summarise", "check", "finalize"]


def test_no_model_writes_from_the_records():
    assert steps_of("c_010", None) == ["preflight", "write_from_records", "finalize"]


def test_card_down_goes_straight_to_finalize():
    stub = CardStub("c_010")
    for tool in ("repayments",):
        stub.when(tool, handler=lambda req: (_ for _ in ()).throw(httpx.ConnectError("refused", request=req)))
    assert steps_of("c_010", [answer()], topic="repayments", stub=stub) == ["preflight", "finalize"]


def test_inside_a_graph_that_saves_its_steps_the_agent_saves_nothing():
    # The case graph runs on a Postgres checkpointer. The agent's graph must not inherit it: its
    # state is not the case's, and it holds tool results the case never keeps.
    saver = InMemorySaver()

    class Case(TypedDict, total=False):
        findings: int

    def ask(_state: Case) -> Case:
        response = card_service(InvestigateRequest(**request("c_010")), model=None, http=CardStub("c_010").client())
        return {"findings": len(response.findings)}

    outer = StateGraph(Case)
    outer.add_node("ask_specialist", ask)
    outer.add_edge(START, "ask_specialist")
    config = {"configurable": {"thread_id": "case_t"}}
    result = outer.compile(checkpointer=saver).invoke({}, config=config)
    assert result["findings"] >= 4
    namespaces = {c.config["configurable"].get("checkpoint_ns", "") for c in saver.list(None)}
    assert namespaces == {""}  # only the case's own checkpoints, none of the agent's
