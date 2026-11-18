"""The Cash Flow agent: a LangGraph StateGraph with code-owned budgets.

    cash_flow(request) -> InvestigateResponse

Same signature as mock.cash_flow so the case graph can swap one for the other
(KAN-36). Each call invokes the compiled graph once; the agent keeps nothing
between calls.

    START → preflight ─┬─ nothing came back ─────────────────────────────► finalize → END
                       ├─ no model ──────────► write_from_records ───────► finalize
                       └─► investigate ⇄ tools                              ▲
                              │          └─ budget used ─► summarise ─┐     │
                              └─ an answer ──────────────────────────►├► check ─► finalize
                                                                     repair ◄┘ (once)

A model step that fails or runs out of time goes to write_from_records.

| Node               | Who   | What |
|--------------------|-------|------|
| preflight          | code  | calls the tools the topic rests on (tools.PREFLIGHT); not counted in the budget |
| investigate        | model | reads the question and the records, calls tools or answers in JSON; 6 turns at most |
| tools              | code  | runs the model's tool calls: 5 at most, a repeat served from the cache, text marked as data |
| summarise          | model | when the budget is used up: answers from what was fetched, with no tools |
| check              | code  | guardrails.check on every finding: ids, figures, no action, no quoted customer text |
| repair             | model | once: told what failed, writes its answer again |
| write_from_records | code  | with no model, or when the model failed: findings written from the records |
| finalize           | code  | adds the hard facts on the topic (findings.hard_facts, tools.on_topic), says what Bank said it could not work out for the preflight calls (tools.gaps), filters what the model left in could_not_determine and suggested_next, returns the preflight records and the cited ones exactly as Bank returned them |

Every node checks the 60-second deadline before it starts; a model call is cut off at the deadline, and a
Bank call waits at most min(10 s, what is left). The model and the Bank tools travel in the run's Runtime
context, never in the state. Tool calls go through the LangChain tool objects (`.invoke`) with the node's
config, so callbacks, LangFuse among them, see each Bank call as a tool run inside the case's trace. The
graph is compiled with checkpointer=False: inside the case graph it saves nothing.
"""

from __future__ import annotations

import contextvars
import json
import logging
import operator
import os
import threading
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal, TypedDict

import httpx
import jwt
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from langgraph.types import Command

from app.agents.cash_flow import findings as written
from app.agents.cash_flow import guardrails, prompts, tools as bank_tools
from app.agents.cash_flow.tools import BankTools, error_code, preflight_calls, records_in
from app.agents.errors import Unanswered
from contracts import Finding, InvestigateRequest, InvestigateResponse

log = logging.getLogger(__name__)

AGENT = "cash_flow"
MAX_TOOL_CALLS = 5     # per API doc §10
MAX_SECONDS = 60       # per API doc §10, enforced in every node
MAX_MODEL_TURNS = 6    # investigate turns: five tool turns and an answer
MAX_REPAIRS = 1
TOKEN_TTL_SECONDS = 180
# Longest run: preflight + 6 investigate + 6 tools + summarise + check + repair + check + finalize = 18.
RECURSION_LIMIT = 40
BUDGET_USED = {"error": "budget_exhausted", "message": f"The {MAX_TOOL_CALLS}-call budget was reached before this call."}
UNREADABLE = {"error": "unreadable_call", "message": "The arguments of this call could not be read. Send it again."}
LATE = {"error": "deadline_passed", "message": "The run's time was used up before this call."}


# ---------------------------------------------------------------------------
# state and context
# ---------------------------------------------------------------------------


def _first_kept(old: dict, new: dict) -> dict:
    """Records keep their first value: the same id never changes mid-run."""
    return {**new, **old}


def _merged(old: dict, new: dict) -> dict:
    return {**old, **new}


class AgentState(TypedDict, total=False):
    """What the graph tracks across nodes. Serialisable."""

    request: dict[str, Any]  # the InvestigateRequest, as JSON
    deadline: float  # time.monotonic() after which no node starts
    messages: Annotated[list[AnyMessage], add_messages]
    # Every record a tool returned in this run, as Bank returned it, by id.
    records: Annotated[dict[str, dict], _first_kept]
    kept: list[str]  # the ids preflight fetched, in order: always returned
    # What Bank said it could not work out for the preflight calls (tools.gaps): always said.
    bank_said: list[str]
    cache: Annotated[dict[str, Any], _merged]  # tool call -> result as shown, so a repeat does not reach Bank
    calls: int  # the model's tool calls that counted
    turns: int  # investigate turns
    budget_exhausted: bool
    # Reasons the response should carry in could_not_determine. operator.add lets any node append.
    unknown: Annotated[list[str], operator.add]
    findings_raw: list[dict]  # the model's findings, type-checked, before the check
    said: list[str]  # what the model put in could_not_determine, before the check
    suggested_next: str | None
    accepted: list[dict]  # findings that passed the check
    problems: list[str]  # what the check found, for a repair
    dropped: list[str]  # findings the check left out, and why
    repairs: int
    response: dict[str, Any]  # the final InvestigateResponse, as a dict


class AgentContext(TypedDict):
    """What one run carries. Never saved."""

    model: BaseChatModel | None
    tools_by_name: dict[str, Any]
    tools_list: list[Any]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _late(state: AgentState) -> bool:
    return bank_tools.now() > state["deadline"]


def _run_tool(tool: Any, name: str, args: Any, config: RunnableConfig) -> Any:
    """One tool call through the LangChain tool object, so callbacks see it. Never raises."""
    if tool is None:
        return {"error": "unknown_tool", "message": f"No such tool: {name}."}
    if not isinstance(args, dict):
        return {"error": "invalid_arguments", "message": "Arguments must be an object."}
    try:
        return tool.invoke(args, config=config)
    except Exception as error:  # noqa: BLE001 - arguments the schema refused, or anything else
        log.warning("cash_flow tool %s raised: %s", name, error)
        return {"error": "invalid_arguments", "message": str(error)[:300]}


def _add(records: dict[str, dict], result: Any) -> tuple[dict[str, dict], list[str], list[str]]:
    """The records of a result not held yet, the ids it returned in order, and a note for any id that came back
    with other content (the first is kept)."""
    new, ids, notes = {}, [], []
    for item in records_in(result):
        ids.append(item["id"])
        held = records.get(item["id"]) or new.get(item["id"])
        if held is None:
            new[item["id"]] = item
        elif held != item:
            notes.append(f"The Bank Service returned {item['id']} twice with different content; the first is kept.")
    return new, ids, notes


def _ask_model(call: Callable[[list], AIMessage], messages: list, state: AgentState) -> AIMessage:
    """One model call, cut off at the run's deadline. Run in a thread that carries the node's context, so
    the call is traced inside the run; a call cut off is left to finish on its own and its reply dropped."""
    remaining = state["deadline"] - bank_tools.now()
    if remaining <= 0:
        raise TimeoutError("the run's time was used up")
    box: dict[str, Any] = {}
    context = contextvars.copy_context()

    def run() -> None:
        try:
            box["reply"] = context.run(call, messages)
        except BaseException as error:  # noqa: BLE001 - handed back to the caller
            box["error"] = error

    thread = threading.Thread(target=run, daemon=True, name="cash_flow-model")
    thread.start()
    thread.join(remaining)
    if thread.is_alive():
        raise TimeoutError(f"the model did not answer within the {MAX_SECONDS}-second budget")
    if "error" in box:
        raise box["error"]
    return box["reply"]


def _model_failed(error: BaseException) -> str:
    log.warning("cash_flow model call failed: %s", error)
    if isinstance(error, TimeoutError):
        return f"The {MAX_SECONDS}-second budget ran out before the model finished."
    return f"The model call failed: {type(error).__name__}."


def _after_model_failure(state: AgentState, reason: str, **update: Any) -> Command:
    """The model failed or ran out of time: answer from the records, unless the check already accepted some."""
    has_records = state.get("records") or update.get("records")
    goto = "finalize" if state.get("accepted") or not has_records else "write_from_records"
    return Command(goto=goto, update={**update, "unknown": [*update.get("unknown", []), reason]})


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------


def _preflight(state: AgentState, config: RunnableConfig, runtime: Runtime[AgentContext]
               ) -> Command[Literal["investigate", "write_from_records", "finalize"]]:
    """Code step: fetch what the topic rests on, before the model starts. Not counted in the budget."""
    request, by_name = state["request"], runtime.context["tools_by_name"]
    calls, unknown = preflight_calls(request)
    shown, records, cache, kept, said = {}, {}, {}, [], []
    answered = False
    for name, args in calls:
        if _late(state):
            unknown.append(f"The {MAX_SECONDS}-second budget ran out before {name} was asked.")
            break
        result = _run_tool(by_name.get(name), name, args, config)
        if code := error_code(result):
            unknown.append(f"The Bank Service did not answer {name} ({code}).")
        else:
            answered = True
        new, ids, notes = _add(records, result)
        records |= new
        kept += [i for i in ids if i not in kept]
        unknown += notes
        said += bank_tools.gaps(result)
        shown[name] = cache[bank_tools.call_key(name, args)] = guardrails.for_the_model(result)
    said = list(dict.fromkeys(said))
    update: dict[str, Any] = {"records": records, "kept": kept, "bank_said": said, "cache": cache,
                              "unknown": unknown, "calls": 0, "turns": 0, "repairs": 0}
    if not answered:
        # Bank answered none of the calls (or none was possible): the model would have nothing to cite.
        return Command(goto="finalize", update=update)
    if runtime.context["model"] is None:
        # With no record on the topic code writes nothing, and says why instead: Bank's reason (finalize says
        # it), or that what came back is about other topics. Otherwise: that code wrote the findings.
        if not said or any(bank_tools.on_topic(r, request) for r in records.values()):
            update["unknown"] = [*unknown, bank_tools.nothing_on_topic(records, request, said)
                                 or "No model is configured, so these findings were written by code from the "
                                    "bank records alone."]
        return Command(goto="write_from_records", update=update)
    if _late(state):
        return Command(goto="write_from_records", update=update)
    update["messages"] = [SystemMessage(content=prompts.SYSTEM_PROMPT), HumanMessage(content=prompts.first_message(
        question=request["question"], topic=request.get("topic"), window_start=request["window_start"],
        window_end=request["window_end"], context=request.get("context", ""), account_id=request.get("account_id"),
        record_id=request.get("record_id"), preflight=shown))]
    return Command(goto="investigate", update=update)


def _investigate(state: AgentState, runtime: Runtime[AgentContext]
                 ) -> Command[Literal["tools", "check", "summarise", "write_from_records", "finalize"]]:
    """Model step: either call a tool or write a final JSON answer."""
    if _late(state):
        return _after_model_failure(state, f"The {MAX_SECONDS}-second budget ran out before the model finished.")
    if state.get("turns", 0) >= MAX_MODEL_TURNS:
        return Command(goto="summarise", update={
            "unknown": [f"The model took {MAX_MODEL_TURNS} turns; it answers from what was fetched."]})
    bound = runtime.context["model"].bind_tools(runtime.context["tools_list"])
    try:
        reply = _ask_model(bound.invoke, state["messages"], state)
    except Exception as error:  # noqa: BLE001 - a provider error must not crash the case
        return _after_model_failure(state, _model_failed(error))
    turns = state.get("turns", 0) + 1
    if getattr(reply, "tool_calls", None) or getattr(reply, "invalid_tool_calls", None):
        return Command(goto="tools", update={"messages": [reply], "turns": turns})
    return Command(goto="check", update={"messages": [reply], "turns": turns, **_findings_from_message(reply)})


def _tools(state: AgentState, config: RunnableConfig, runtime: Runtime[AgentContext]
           ) -> Command[Literal["investigate", "summarise", "write_from_records", "finalize"]]:
    """Code step: run each tool call the model emitted, within budget and time."""
    by_name = runtime.context["tools_by_name"]
    calls, cache, records = state.get("calls", 0), dict(state.get("cache", {})), dict(state.get("records", {}))
    messages: list[AnyMessage] = []
    new_records: dict[str, dict] = {}
    notes: list[str] = []
    over = late = False
    for call in state["messages"][-1].tool_calls:
        # Every call in the batch gets an answer, even over budget: Anthropic and OpenAI both refuse a
        # follow-up invoke if any tool call of the previous message was left unanswered.
        name, args = call["name"], call.get("args") or {}
        if calls >= MAX_TOOL_CALLS:
            over, result = True, BUDGET_USED
        elif _late(state):
            late, result = True, LATE
        else:
            calls += 1
            key = bank_tools.call_key(name, args)
            if key in cache:
                result = cache[key]  # the same call again: counted, but Bank is not asked twice
            else:
                raw = _run_tool(by_name.get(name), name, args, config)
                added, _, found = _add(records, raw)
                records |= added
                new_records |= added
                notes += found
                result = cache[key] = guardrails.for_the_model(raw)
        messages.append(ToolMessage(content=json.dumps(result, default=str),
                                    tool_call_id=call.get("id") or f"call_fallback_{id(call)}"))
    # A call whose arguments could not be read gets an answer too, and counts, so it cannot loop.
    for bad in getattr(state["messages"][-1], "invalid_tool_calls", None) or []:
        if bad.get("id"):
            calls += 1
            messages.append(ToolMessage(content=json.dumps(UNREADABLE), tool_call_id=bad["id"], status="error"))
    update: dict[str, Any] = {"messages": messages, "records": new_records, "calls": calls, "cache": cache,
                              "unknown": notes}
    if late:
        return _after_model_failure(state, f"The {MAX_SECONDS}-second budget ran out before the model finished.",
                                    **update)
    if over:
        update |= {"budget_exhausted": True,
                   "unknown": [*notes, f"Reached the {MAX_TOOL_CALLS}-tool-call budget; summarising from what is known."]}
        return Command(goto="summarise", update=update)
    return Command(goto="investigate", update=update)


def _summarise(state: AgentState, runtime: Runtime[AgentContext]
               ) -> Command[Literal["check", "write_from_records", "finalize"]]:
    """Model step: budget or turns are spent — write the final answer tool-free."""
    nudge = HumanMessage(content=prompts.SUMMARISE)
    try:
        final = _ask_model(runtime.context["model"].invoke, [*state["messages"], nudge], state)
    except Exception as error:  # noqa: BLE001
        return _after_model_failure(state, _model_failed(error), messages=[nudge])
    return Command(goto="check", update={"messages": [nudge, final], **_findings_from_message(final)})


def _check(state: AgentState) -> Command[Literal["repair", "finalize"]]:
    """Code step: every drafted finding against the records of this run."""
    checked = guardrails.check(state.get("findings_raw", []), state.get("records", {}))
    update = {"accepted": checked.accepted, "problems": checked.problems, "dropped": checked.dropped}
    if checked.problems and state.get("repairs", 0) < MAX_REPAIRS and not _late(state):
        return Command(goto="repair", update=update)
    return Command(goto="finalize", update=update)


def _repair(state: AgentState, runtime: Runtime[AgentContext]) -> Command[Literal["check", "finalize"]]:
    """Model step, once: told what the check refused, the model writes its whole answer again."""
    ask = HumanMessage(content=prompts.repair_message(state["problems"]))
    repairs = state.get("repairs", 0) + 1
    try:
        reply = _ask_model(runtime.context["model"].invoke, [*state["messages"], ask], state)
    except Exception as error:  # noqa: BLE001 - keep what the last check accepted
        return Command(goto="finalize", update={"messages": [ask], "repairs": repairs,
                                                "unknown": [_model_failed(error)]})
    return Command(goto="check", update={"messages": [ask, reply], "repairs": repairs,
                                         **_findings_from_message(reply)})


def _write_from_records(state: AgentState) -> Command[Literal["finalize"]]:
    """Code step: findings written from the records alone."""
    accepted = written.from_records(state.get("records", {}), state["request"])
    return Command(goto="finalize", update={"accepted": [f.model_dump(mode="json") for f in accepted],
                                            "suggested_next": None, "said": [], "dropped": []})


def _finalize(state: AgentState) -> dict[str, Any]:
    """Code step: hard facts, the model's side text checked, the evidence as Bank returned it."""
    return {"response": _respond(state.get("records", {}), state.get("kept", []),
                                 [Finding.model_validate(f) for f in state.get("accepted", [])],
                                 list(state.get("unknown", [])) + list(state.get("dropped", [])),
                                 state.get("said", []), state.get("suggested_next"), state["request"],
                                 state.get("bank_said", []))}


def _respond(records: dict[str, dict], kept: list[str], accepted: list[Finding], unknown: list[str],
             said: list[str], suggested: str | None, request: dict[str, Any], bank_said: list[str]
             ) -> dict[str, Any]:
    """The response: the topic's hard facts first, whatever the model wrote, then the accepted findings.
    What Bank said it could not work out for the topic's calls is always said."""
    claims = {finding.claim for finding in accepted}
    hard = [finding for finding in written.hard_facts(records, request) if finding.claim not in claims]
    if off := bank_tools.nothing_on_topic(records, request, bank_said):
        unknown.append(off)
    unknown += bank_said
    found = hard + accepted
    # Every line is checked, the ones code wrote too: a line that names what the check left out must not
    # carry the model's action or figure to the Orchestrator either.
    lines, unknown = [*unknown, *said], []
    for text in lines:
        if problem := guardrails.side_text_problem(text, records):
            unknown.append(f"A could-not-determine line was left out because {problem}.")
        else:
            unknown.append(text)
    if suggested and (problem := guardrails.side_text_problem(suggested, records)):
        unknown.append(f"A suggested next step was left out because {problem}.")
        suggested = None
    unknown = list(dict.fromkeys(unknown))
    if not found and not unknown:
        unknown = ["The bank records fetched did not answer the question."]
    ids = list(dict.fromkeys([*kept, *(i for finding in found for i in finding.evidence_ids)]))
    response = InvestigateResponse(agent=AGENT, findings=found, evidence=[records[i] for i in ids if i in records],
                                   could_not_determine=unknown, suggested_next=suggested)
    return response.model_dump(mode="json")


# ---------------------------------------------------------------------------
# graph construction
# ---------------------------------------------------------------------------


def _build_graph():
    builder = StateGraph(AgentState, context_schema=AgentContext)
    builder.add_node("preflight", _preflight)
    builder.add_node("investigate", _investigate)
    builder.add_node("tools", _tools)
    builder.add_node("summarise", _summarise)
    builder.add_node("check", _check)
    builder.add_node("repair", _repair)
    builder.add_node("write_from_records", _write_from_records)
    builder.add_node("finalize", _finalize)
    builder.add_edge(START, "preflight")
    builder.add_edge("finalize", END)
    # checkpointer=False: never saved, even when run inside a graph that saves its own steps.
    return builder.compile(checkpointer=False, name=AGENT)


_GRAPH = _build_graph()


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def cash_flow(
    request: InvestigateRequest,
    *,
    model: BaseChatModel | None = None,
    http: httpx.Client | None = None,
) -> InvestigateResponse:
    """Answer one question about one customer's bank records. Never raises. When Bank was asked and every
    call failed as an outage (unreachable, timeout, 401, 403, 5xx, unknown customer), the answer is an
    errors.Unanswered: the case graph does not record the question, and the case waits and asks again,
    or fails with the reason (app/runner.py).

    Tests pass `model` (a scripted fake) and `http` (an httpx.Client wired to
    a MockTransport). Production leaves both at None and the agent reads
    LLM_MODEL / BANK_URL from the environment. With no model, the findings are
    written by code from the records the topic rests on.
    """
    try:
        return _answer(request, model, http)
    except Exception as error:  # noqa: BLE001 - the case goes on whatever happens here
        log.exception("cash_flow failed")
        return _cannot(f"The Cash Flow agent failed: {type(error).__name__}.")


def _answer(request: InvestigateRequest, model: BaseChatModel | None, http: httpx.Client | None) -> InvestigateResponse:
    if request.topic not in bank_tools.PREFLIGHT:
        return _cannot(f"The bank side has no such topic. It has: {', '.join(bank_tools.TOPICS)}.")
    try:
        token = _mint_service_token(customer_id=request.customer_id, case_id=request.case_id)
    except RuntimeError as error:
        return _cannot(str(error))
    deadline = bank_tools.now() + MAX_SECONDS
    bank = BankTools(customer_id=request.customer_id, token=token, deadline=deadline, http=http)
    try:
        if model is None:
            model = _default_model()
        try:
            final_state = _GRAPH.invoke(
                {"request": request.model_dump(mode="json"), "deadline": deadline},
                context=AgentContext(model=model, tools_by_name=bank.by_name, tools_list=bank.tools),
                config={"recursion_limit": RECURSION_LIMIT},
            )
            response = InvestigateResponse.model_validate(final_state["response"])
        except Exception as error:  # noqa: BLE001 - answer from what was fetched before it failed
            log.warning("cash_flow graph failed: %s", error)
            # What Bank said about the topic's own calls; a call the model chose may be about another window.
            calls, _ = preflight_calls(request.model_dump(mode="json"))
            said = [line for name, args in calls for line in bank.said.get(bank_tools.call_key(name, args), [])]
            response = _from_what_was_fetched(bank.records, request, f"The Cash Flow agent's run failed: "
                                                                     f"{type(error).__name__}.",
                                              list(dict.fromkeys(said)))
    finally:
        bank.close()
    # Decided on the calls themselves, not on the answer: an empty answer Bank gave is still an answer.
    if down := bank.outage():
        return Unanswered.of(response, f"every call to the Bank Service failed ({down})")
    return response


def _from_what_was_fetched(records: dict[str, dict], request: InvestigateRequest, reason: str,
                           bank_said: list[str]) -> InvestigateResponse:
    """Code-written findings from every record any call returned, and every one of those records."""
    accepted = written.from_records(records, request.model_dump(mode="json"))
    return InvestigateResponse.model_validate(_respond(records, list(records), accepted, [reason], [], None,
                                                       request.model_dump(mode="json"), bank_said))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _findings_from_message(message: AIMessage) -> dict[str, Any]:
    """The model's JSON answer as a state update: findings_raw, said, suggested_next, unknown.

    Expected in the final AIMessage.content (JSON object):

        {"findings": [{"claim": "...", "confidence": "high", "evidence_ids": [...]}],
         "could_not_determine": ["..."], "suggested_next": "..."}

    Every field is type-checked; what is not of the shape asked for is left out and said so.
    """
    content = _flatten_content(message.content)
    try:
        payload = json.loads(_extract_json(content))
    except (ValueError, TypeError):
        payload = None
    if not isinstance(payload, dict):
        return {"findings_raw": [], "said": [], "suggested_next": None,
                "unknown": ["The model's answer was not the JSON object asked for."]}
    drafts, unknown = [], []
    raw_findings = payload.get("findings", [])
    if not isinstance(raw_findings, list):
        unknown.append("The model's findings were not a list, so none were taken.")
        raw_findings = []
    for raw in raw_findings:
        if not isinstance(raw, dict):
            unknown.append("A finding was left out because it was not an object.")
            continue
        claim, ids = raw.get("claim"), raw.get("evidence_ids")
        if not isinstance(claim, str) or not claim.strip():
            unknown.append("A finding was left out because it had no claim.")
            continue
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            unknown.append("A finding was left out because its evidence_ids were not a list of ids.")
            continue
        try:
            drafts.append(Finding(claim=guardrails.clean(claim), confidence=raw.get("confidence"),
                                  evidence_ids=ids).model_dump(mode="json"))
        except ValueError:
            unknown.append("A finding was left out because it was not in the shape asked for.")
    said = payload.get("could_not_determine", [])
    said = [said] if isinstance(said, str) else said if isinstance(said, list) else []
    said = [guardrails.clean(item, 300) for item in said if isinstance(item, str) and item.strip()]
    suggested = payload.get("suggested_next")
    suggested = guardrails.clean(suggested, 300) if isinstance(suggested, str) and suggested.strip() else None
    return {"findings_raw": drafts, "said": said, "suggested_next": suggested, "unknown": unknown}


def _flatten_content(content: Any) -> str:
    """Normalise message.content to a plain string.

    Anthropic chat models via LangChain often return a list of blocks like
    ``[{"type": "text", "text": "..."}]``. OpenAI returns a plain string.
    Join the text blocks and ignore non-text ones.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


def _extract_json(text: str) -> str:
    """Pull the first JSON object out of a text blob — the model may wrap it
    in prose or a ```json fence."""
    text = text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _mint_service_token(*, customer_id: str, case_id: str) -> str:
    """Short-lived, scoped to one customer, read-only."""
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET is not set; the agent cannot call Bank")
    claims = {
        "sub": f"cash_flow:{case_id}",
        "role": "service",
        "customers": [customer_id],
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def _default_model() -> BaseChatModel | None:
    """The model every agent in this service uses, from LLM_MODEL (app.runtime). None while unset."""
    try:
        from app.runtime import _chat_model

        return _chat_model()
    except Exception as error:  # noqa: BLE001 - no model is a supported state, not a crash
        log.warning("cash_flow has no model: %s", error)
        return None


def _cannot(reason: str) -> InvestigateResponse:
    return InvestigateResponse(agent=AGENT, findings=[], evidence=[], could_not_determine=[reason])
