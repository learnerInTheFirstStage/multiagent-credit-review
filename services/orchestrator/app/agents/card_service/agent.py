"""The Card Service agent (KAN-26): the Credit Behavior specialist, as a LangGraph graph.

    card_service(request) -> InvestigateResponse

Same shape as mock.credit_behavior, so the case graph can swap one for the other (KAN-36). Each
call runs the graph once from the start: the agent keeps nothing between calls.

    START → preflight ─┬─ nothing came back ─────────────────────────────► finalize → END
                       ├─ no model ──────────► write_from_records ───────► finalize
                       └─► investigate ⇄ tools                              ▲
                              │          └─ budget used ─► summarise ─┐     │
                              └─ an answer ──────────────────────────►├► check ─► finalize
                                                                     repair ◄┘ (once)

| Node               | Who   | What |
|--------------------|-------|------|
| preflight          | code  | calls the tools the topic rests on (PREFLIGHT); not counted in the budget |
| investigate        | model | reads the question and the records, calls tools or answers in JSON |
| tools              | code  | runs the model's tool calls: 5 at most, a repeated call free, memos marked as data; after 8 model turns, or with the budget used, to summarise |
| summarise          | model | when the budget or the turns are used up: answers from what was fetched, with no tools |
| check              | code  | guardrails.check on every finding (ids, figures, no action, too_short_to_tell) |
| repair             | model | once: told what failed, writes its answer again; what the first check accepted is kept |
| write_from_records | code  | with no model, or when the model gave no answer (it failed, or time ran out), findings written from the records alone |
| finalize           | code  | adds a code-written finding for every failed or late repayment and paying-account change of the topic's kinds (TOPIC_KINDS); the evidence is every preflight record plus the cited ones, as Card returned them |

The 60-second deadline is checked before every Card call and every model call. Card calls time out
at min(10 s, the time left). Tool calls run through the LangChain tool objects, so callbacks see a
tool run for each. If the graph fails anyway, card_service answers from the records fetched so far,
written by code, and says so: it never raises.

The model and the Card client travel in the run's context, not in the state, so nothing that
cannot be serialised is ever in it. The graph is compiled with checkpointer=False: run inside the
case graph, it does not inherit the case graph's Postgres checkpointer, and it saves nothing. Its
nodes run as named steps of the caller's run, so LangFuse shows them inside the case's trace.
"""

from __future__ import annotations

import json
import logging
import operator
import os
import time
from typing import Annotated, Any, Literal, TypedDict

import httpx
import jwt
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from langgraph.types import Command

from app.agents.card_service import findings as written
from app.agents.card_service import guardrails, prompts
from app.agents.card_service.tools import CardTools
from app.agents.errors import Unanswered
from app.graph.causes import KIND_TOPIC
from contracts import Evidence, Finding, InvestigateRequest, InvestigateResponse

log = logging.getLogger(__name__)

# The specialist's name in the shared contract (contracts.investigate.AgentName) and in the graph.
AGENT = "credit_behavior"
# Who the agent is in the tokens it signs for the Card Service, and the graph's name in traces.
TOKEN_SUB = "card_service"
TOPICS = ("repayments", "paying_account", "card_use")
MAX_TOOL_CALLS = 5
MAX_SECONDS = 60
MAX_TURNS = 8  # model turns that call tools; a repeated call is free, so this is what bounds a loop
MAX_REPAIRS = 1
TOKEN_TTL_SECONDS = 180
RECURSION_LIMIT = 40  # the longest run is 1 + 2 * MAX_TURNS + 5 = 22 steps

# What code fetches before the model starts, per topic. None is "whatever you hold".
PREFLIGHT: dict[str | None, list[tuple[str, dict[str, Any]]]] = {
    "repayments": [("card_repayments", {"count": 3})],
    "paying_account": [("card_paying_account", {"count": 3})],
    "card_use": [("card_utilization", {}), ("card_spend_by_cycle", {})],
    None: [("card_utilization", {}), ("card_spend_by_cycle", {}), ("card_repayments", {"count": 3}),
           ("card_paying_account", {"count": 3})],
}
# The kinds of record each topic answers with, from the Orchestrator's own mapping, plus repayment_summary:
# Card's count of paid, late and failed repayments, which the Orchestrator does not map. Code-written findings
# (hard facts, and the answer written without a model) cover only these: a record fetched in support of
# another kind (the repayments the paying-account check compares) is returned as evidence, but its finding is
# written under its own topic. With no topic every kind is in scope.
TOPIC_KINDS: dict[str, tuple[str, ...]] = {
    topic: tuple(kind for kind, (tool, of) in KIND_TOPIC.items() if (tool, of) == ("AskCreditBehavior", topic))
    + (("repayment_summary",) if topic == "repayments" else ())
    for topic in TOPICS
}
BUDGET_USED = {"error": "budget_exhausted", "message": f"The {MAX_TOOL_CALLS}-call budget is used up."}


# ---------------------------------------------------------------------------
# state and context
# ---------------------------------------------------------------------------


def _first_kept(old: dict, new: dict) -> dict:
    """Records keep the content they first came with: the same id never changes in a run."""
    return {**new, **old}


def _merged(old: dict, new: dict) -> dict:
    return {**old, **new}


class AgentState(TypedDict, total=False):
    request: dict[str, Any]  # the InvestigateRequest, as JSON
    deadline: float  # time.monotonic() after which no step starts
    preflight: dict[str, Any]  # what preflight fetched, as the model is shown it
    fetched: list[str]  # the ids of the records preflight fetched, in order: all go in the evidence
    records: Annotated[dict[str, dict], _first_kept]  # every record a tool returned, by id
    cache: Annotated[dict[str, Any], _merged]  # tool call -> result as shown, so a repeat is free
    calls: int  # tool calls the model has made that counted
    turns: int  # times investigate called the model
    messages: Annotated[list[AnyMessage], add_messages]
    draft: list[dict]  # the model's findings, before the check
    suggested: str | None
    accepted: list[dict]  # findings that passed the check
    problems: list[str]  # what the check found, for a repair
    dropped: list[str]  # findings the check left out, and why
    repairs: int
    unknown: Annotated[list[str], operator.add]  # could_not_determine, added to by every step
    response: dict[str, Any]


class AgentContext(TypedDict):
    """What one run brings with it. Never saved."""

    model: BaseChatModel | None
    tools: CardTools


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _key(name: str, args: dict[str, Any]) -> str:
    return name + json.dumps(args, sort_keys=True, default=str)


def _now() -> float:
    return time.monotonic()


def _run_tool(tools: CardTools, name: str, args: dict[str, Any], config: RunnableConfig | None
              ) -> tuple[Any, dict[str, dict]]:
    """One call to Card, through its LangChain tool so callbacks see a tool run: the result as the
    model may read it, and the records it returned."""
    tool = tools.tool(name)
    if tool is None:
        raw = tools.call(name, **args)  # unknown_tool, as before
    else:
        try:
            raw = tool.invoke(args, config)
        except ValueError as error:  # the tool's own schema check: the same answer call() gives
            raw = {"error": "invalid_arguments", "message": str(error)[:400]}
    pool = guardrails.EvidencePool()
    pool.add(raw)
    return guardrails.for_the_model(raw), {i: e.model_dump(mode="json") for i, e in pool.records.items()}


def _pool(records: dict[str, dict]) -> guardrails.EvidencePool:
    pool = guardrails.EvidencePool()
    pool.records = {i: Evidence.model_validate(r) for i, r in records.items()}
    return pool


def _on_topic(pool: guardrails.EvidencePool, topic: str | None) -> guardrails.EvidencePool:
    """The records of the kinds the topic answers with: what code writes its findings from."""
    if topic is None:
        return pool
    scoped = guardrails.EvidencePool()
    scoped.records = {i: e for i, e in pool.records.items() if e.kind in TOPIC_KINDS[topic]}
    return scoped


def _nothing_on_topic(pool: guardrails.EvidencePool, topic: str | None) -> str | None:
    """What to say when records came back but none of them answers the topic asked."""
    if topic is None or not pool.records or _on_topic(pool, topic).records:
        return None
    return f"The Card Service returned no {topic} record: what it returned is about other topics."


def _late(state: AgentState) -> bool:
    return _now() > state["deadline"]


OUT_OF_TIME = f"The {MAX_SECONDS}-second budget ran out"


def _text(content: Any) -> str:
    """message.content as text: Anthropic returns a list of blocks, OpenAI a string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def _json_part(text: str) -> str:
    """The outermost JSON object in the text: the model may wrap it in prose or a code fence."""
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if 0 <= start < end else text


def _read(message: AIMessage) -> dict[str, Any]:
    """The model's JSON answer, as a state update: draft, suggested, and what it could not say."""
    try:
        payload = json.loads(_json_part(_text(message.content)))
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        return {"draft": [], "suggested": None, "unknown": ["The model's answer was not the JSON asked for."]}
    drafts, unknown = [], []
    findings, gaps = payload.get("findings") or [], payload.get("could_not_determine") or []
    if not isinstance(findings, list):
        unknown.append("The model's findings were left out because they were not in the shape asked for.")
        findings = []
    if not isinstance(gaps, list):
        unknown.append("What the model could not determine was left out because it was not in the shape asked for.")
        gaps = []
    for raw in findings:
        try:
            drafts.append(Finding.model_validate(raw).model_dump(mode="json"))
        except ValueError:
            unknown.append("A finding was left out because it was not in the shape asked for.")
    for item in gaps:
        if isinstance(item, str) and item.strip():
            if guardrails.recommends_an_action(item):
                unknown.append("An item the model could not determine was left out because it recommended an action.")
            else:
                unknown.append(" ".join(item.split())[:300])
    suggested = payload.get("suggested_next")
    suggested = " ".join(suggested.split())[:300] if isinstance(suggested, str) and suggested.strip() else None
    return {"draft": drafts, "suggested": suggested, "unknown": unknown}


def _model_failed(error: Exception) -> dict[str, Any]:
    log.warning("card_service model call failed: %s", error)
    return {"draft": [], "suggested": None, "unknown": [f"The model call failed: {type(error).__name__}."]}


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------


def preflight(state: AgentState, runtime: Runtime[AgentContext], config: RunnableConfig
              ) -> Command[Literal["investigate", "write_from_records", "finalize"]]:
    tools = runtime.context["tools"]
    shown, records, cache, unknown = {}, {}, {}, []
    late = False
    for name, args in PREFLIGHT[state["request"].get("topic")]:
        if _late(state):
            late = True
            unknown.append(f"{OUT_OF_TIME} before code fetched {name}.")
            break
        result, returned = _run_tool(tools, name, args, config)
        shown[name] = result
        cache[_key(name, args)] = result
        records = _first_kept(records, returned)
        if isinstance(result, dict) and "error" in result:
            unknown.append(f"The Card Service did not answer {name}: {result['error']}: {result.get('message', '')}".strip())
    update = {"preflight": shown, "fetched": list(records), "records": records, "cache": cache, "unknown": unknown,
              "calls": 0, "turns": 0, "repairs": 0}
    if not records:
        goto = "finalize"  # nothing came back: no finding can rest on anything
    elif runtime.context["model"] is None or late or _late(state):
        goto = "write_from_records"
    else:
        goto = "investigate"
    return Command(goto=goto, update=update)


def investigate(state: AgentState, runtime: Runtime[AgentContext]
                ) -> Command[Literal["tools", "check", "write_from_records"]]:
    if _late(state):
        return Command(goto="write_from_records", update={"draft": [], "suggested": None,
                                             "unknown": [f"{OUT_OF_TIME} before the model finished."]})
    new: list[AnyMessage] = []
    if not state.get("messages"):
        r = state["request"]
        new = [SystemMessage(content=prompts.SYSTEM_PROMPT), HumanMessage(content=prompts.first_message(
            question=r["question"], topic=r.get("topic"), window_start=r["window_start"], window_end=r["window_end"],
            context=r.get("context", ""), account_id=r.get("account_id"), record_id=r.get("record_id"),
            preflight=state["preflight"]))]
    bound = runtime.context["model"].bind_tools(runtime.context["tools"].langchain_tools())
    turns = state.get("turns", 0) + 1
    try:
        reply = bound.invoke([*state.get("messages", []), *new])
    except Exception as error:  # noqa: BLE001 - a provider error must not crash the case
        return Command(goto="write_from_records", update={"messages": new, "turns": turns, **_model_failed(error)})
    if getattr(reply, "tool_calls", None):
        return Command(goto="tools", update={"messages": [*new, reply], "turns": turns})
    return Command(goto="check", update={"messages": [*new, reply], "turns": turns, **_read(reply)})


def tools(state: AgentState, runtime: Runtime[AgentContext], config: RunnableConfig
          ) -> Command[Literal["investigate", "summarise", "write_from_records"]]:
    card = runtime.context["tools"]
    calls, cache, records = state.get("calls", 0), dict(state.get("cache", {})), {}
    messages: list[AnyMessage] = []
    over = late = False
    for call in state["messages"][-1].tool_calls:
        args = call.get("args") or {}
        key = _key(call["name"], args)
        if key in cache:
            result = cache[key]  # a repeat: the earlier result, not counted again
        elif late or _late(state):
            late, result = True, {"error": "out_of_time", "message": f"{OUT_OF_TIME}."}
        elif calls >= MAX_TOOL_CALLS:
            over, result = True, BUDGET_USED
        else:
            calls += 1
            result, returned = _run_tool(card, call["name"], args, config)
            cache[key] = result
            records = _first_kept(records, returned)
        # Every call gets an answer, or the provider refuses the next turn.
        messages.append(ToolMessage(content=json.dumps(result, default=str), tool_call_id=call.get("id") or f"call_{id(call)}"))
    update: dict[str, Any] = {"calls": calls, "cache": cache, "records": records, "messages": messages}
    if late or _late(state):
        # No time for another model call: code writes the findings from the records fetched.
        update |= {"draft": [], "suggested": None, "unknown": [f"{OUT_OF_TIME} before the model finished."]}
        return Command(goto="write_from_records", update=update)
    if over:
        messages.append(HumanMessage(content="The tool budget is used up. Do not call tools. Write your answer as one JSON object now."))
        update["unknown"] = [f"The {MAX_TOOL_CALLS}-call budget ran out; the answer rests on what was fetched."]
        return Command(goto="summarise", update=update)
    if state.get("turns", 0) >= MAX_TURNS:
        messages.append(HumanMessage(content=f"You have had {MAX_TURNS} tool rounds. Do not call tools. "
                                             "Write your answer as one JSON object now."))
        update["unknown"] = [f"The model used its {MAX_TURNS} tool rounds; the answer rests on what was fetched."]
        return Command(goto="summarise", update=update)
    return Command(goto="investigate", update=update)


def summarise(state: AgentState, runtime: Runtime[AgentContext]) -> Command[Literal["check", "write_from_records"]]:
    if _late(state):
        return Command(goto="write_from_records", update={"draft": [], "suggested": None,
                                             "unknown": [f"{OUT_OF_TIME} before the model finished."]})
    try:
        reply = runtime.context["model"].invoke(state["messages"])
    except Exception as error:  # noqa: BLE001
        return Command(goto="write_from_records", update=_model_failed(error))
    return Command(goto="check", update={"messages": [reply], **_read(reply)})


def check(state: AgentState) -> Command[Literal["repair", "finalize"]]:
    drafts = [Finding.model_validate(f) for f in state.get("draft", [])]
    checked = guardrails.check(drafts, _pool(state.get("records", {})))
    accepted = [f.model_dump(mode="json") for f in checked.accepted]
    if state.get("repairs", 0):
        # After a repair, keep what the first check accepted unless the repaired answer covers its records.
        covered = {i for f in accepted for i in f["evidence_ids"]}
        accepted = [f for f in state.get("accepted", []) if not covered & set(f["evidence_ids"])] + accepted
    update = {"accepted": accepted, "problems": checked.problems, "dropped": checked.dropped}
    if checked.problems and state.get("repairs", 0) < MAX_REPAIRS and not _late(state):
        return Command(goto="repair", update=update)
    return Command(goto="finalize", update=update)


def repair(state: AgentState, runtime: Runtime[AgentContext]) -> Command[Literal["check", "finalize"]]:
    ask = HumanMessage(content=prompts.repair_message(state["problems"]))
    try:
        reply = runtime.context["model"].invoke([*state["messages"], ask])
    except Exception as error:  # noqa: BLE001 - keep what the last check accepted
        log.warning("card_service model call failed: %s", error)
        return Command(goto="finalize", update={"messages": [ask], "repairs": state.get("repairs", 0) + 1,
                                                "unknown": [f"The model call failed: {type(error).__name__}."]})
    return Command(goto="check", update={"messages": [ask, reply], "repairs": state.get("repairs", 0) + 1, **_read(reply)})


def write_from_records(state: AgentState, runtime: Runtime[AgentContext]) -> Command[Literal["finalize"]]:
    pool, topic = _pool(state["records"]), state["request"].get("topic")
    accepted = written.from_records(_on_topic(pool, topic))
    why = ("No model is configured, so these findings" if runtime.context["model"] is None
           else "The model gave no answer, so these findings")
    # Records came back but none on the topic: code writes nothing, and says why instead.
    said = _nothing_on_topic(pool, topic) or f"{why} were written by code from the card records alone."
    return Command(goto="finalize", update={
        "accepted": [f.model_dump(mode="json") for f in accepted], "suggested": None, "unknown": [said]})


def _respond(pool: guardrails.EvidencePool, accepted: list[Finding], unknown: list[str], suggested: str | None,
             fetched: list[str], topic: str | None) -> InvestigateResponse:
    """The answer: every hard fact of the topic's kinds stated by code, and as evidence every record
    preflight fetched plus the cited ones, exactly as Card returned them."""
    accepted = list(accepted)
    # A hard fact is always stated by code: a finding that cites the record may say something else about it.
    for record in written.hard_facts(_on_topic(pool, topic)):
        finding = written.for_hard_fact(record)
        if finding is not None and not any((f.claim, f.evidence_ids) == (finding.claim, finding.evidence_ids)
                                           for f in accepted):
            accepted.append(finding)
    if off := _nothing_on_topic(pool, topic):
        unknown.append(off)
    if any(guardrails.too_short(e) for e in pool.records.values()):
        unknown.append("The card history is too short to tell what is usual for this customer.")
    if suggested and guardrails.recommends_an_action(suggested):
        unknown.append("A suggested next step was left out because it recommended an action.")
        suggested = None
    # Every line is checked, the ones code wrote too: a line that names what the check left out must not
    # carry the model's action or figure to the Orchestrator either.
    unknown = [f"A could-not-determine line was left out because {problem}."
               if (problem := guardrails.side_text_problem(text, pool)) else text for text in unknown]
    unknown = list(dict.fromkeys(unknown))
    if not accepted and not unknown:
        unknown = ["The card records fetched did not answer the question."]
    evidence = pool.pick([i for f in accepted for i in f.evidence_ids] + fetched)
    return InvestigateResponse(agent=AGENT, findings=accepted, evidence=evidence, could_not_determine=unknown,
                               suggested_next=suggested)


def finalize(state: AgentState) -> dict[str, Any]:
    response = _respond(_pool(state.get("records", {})), [Finding.model_validate(f) for f in state.get("accepted", [])],
                        list(state.get("unknown", [])) + list(state.get("dropped", [])), state.get("suggested"),
                        list(state.get("fetched", [])), state["request"].get("topic"))
    return {"response": response.model_dump(mode="json")}


def build() -> Any:
    graph = StateGraph(AgentState, context_schema=AgentContext)
    for node in (preflight, investigate, tools, summarise, check, repair, write_from_records, finalize):
        graph.add_node(node.__name__, node)
    graph.add_edge(START, "preflight")
    graph.set_finish_point("finalize")
    # checkpointer=False: never saved, even when run inside a graph that saves its own steps.
    return graph.compile(checkpointer=False, name=TOKEN_SUB)


GRAPH = build()


# ---------------------------------------------------------------------------
# the entry function
# ---------------------------------------------------------------------------


def card_service(
    request: InvestigateRequest,
    *,
    model: BaseChatModel | None = None,
    http: httpx.Client | None = None,
) -> InvestigateResponse:
    """Answer one question about one customer's card. Tests pass a scripted `model` and a stubbed `http`.
    When Card was asked and every call failed as an outage, the answer is an errors.Unanswered: the case
    graph does not record the question, and the case waits and asks again, or fails with the reason
    (app/runner.py)."""
    if request.topic is not None and request.topic not in TOPICS:
        return _cannot(f"The card side has no such topic. It has: {', '.join(TOPICS)}.")
    try:
        token = _service_token(request.customer_id, request.case_id)
    except RuntimeError as error:
        return _cannot(str(error))
    deadline = _now() + MAX_SECONDS
    card = CardTools(customer_id=request.customer_id, token=token, http=http, deadline=deadline,
                     clock=lambda: _now())  # looked up at each call, so a test's clock reaches Card calls too
    try:
        final = GRAPH.invoke(
            {"request": request.model_dump(mode="json"), "deadline": deadline},
            config={"recursion_limit": RECURSION_LIMIT},
            context=AgentContext(model=model if model is not None else _default_model(), tools=card),
        )
        response = InvestigateResponse.model_validate(final["response"])
    except Exception as error:  # noqa: BLE001 - the case drops a specialist that raises; answer from the records
        log.exception("card_service failed; answering from the records fetched")
        response = _from_what_was_fetched(card, error, request.topic)
    finally:
        card.close()
    # Decided on the calls themselves, not on the answer: an empty answer Card gave is still an answer.
    if down := card.outage():
        return Unanswered.of(response, f"every call to the Card Service failed ({down})")
    return response


def _from_what_was_fetched(card: CardTools, error: Exception, topic: str | None) -> InvestigateResponse:
    """When the graph failed: findings written by code from the topic's records Card returned in this
    run, and every record it returned as evidence."""
    pool = guardrails.EvidencePool()
    for result in card.results:
        pool.add(result)
    unknown = [f"The agent failed ({type(error).__name__}), so these findings were written by code from the "
               "card records alone."]
    try:
        accepted = written.from_records(_on_topic(pool, topic))
    except Exception:  # noqa: BLE001 - a record of an unexpected shape: no findings, still an answer
        log.exception("card_service could not write findings from the records")
        accepted, unknown = [], [f"The agent failed ({type(error).__name__}) and code could not write findings "
                                 "from the card records."]
    try:
        return _respond(pool, accepted, unknown, None, list(pool.records), topic)
    except Exception:  # noqa: BLE001
        log.exception("card_service could not answer from the records")
        return _cannot(f"The agent failed ({type(error).__name__}).")


def _service_token(customer_id: str, case_id: str) -> str:
    """Short-lived, for this customer only. Card refuses it for any other customer and for /snapshots."""
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET is not set, so the agent cannot call the Card Service.")
    claims = {"sub": f"{TOKEN_SUB}:{case_id}", "role": "service", "customers": [customer_id],
              "exp": int(time.time()) + TOKEN_TTL_SECONDS}
    return jwt.encode(claims, secret, algorithm="HS256")


def _default_model() -> BaseChatModel | None:
    """The model every agent in this service uses, from LLM_MODEL (app.runtime). None while unset."""
    try:
        from app.runtime import _chat_model

        return _chat_model()
    except Exception as error:  # noqa: BLE001 - no model is a supported state, not a crash
        log.warning("card_service has no model: %s", error)
        return None


def _cannot(reason: str) -> InvestigateResponse:
    return InvestigateResponse(agent=AGENT, findings=[], evidence=[], could_not_determine=[reason])
