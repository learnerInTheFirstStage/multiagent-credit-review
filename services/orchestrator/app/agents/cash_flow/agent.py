"""The Cash Flow agent: a LangGraph StateGraph with code-owned budgets.

    cash_flow(request) -> InvestigateResponse

Same signature as mock.cash_flow so the case graph can swap one for the other
(KAN-36). Each call compiles-and-invokes the graph once; the agent keeps
nothing between calls.

    START → investigate ⇄ tools
                │          └─ budget used → summarise ──► check → END
                └─ answer ─────────────────────────────►┘

| Node       | Who   | What                                                     |
|------------|-------|----------------------------------------------------------|
| investigate| model | reads the question and tool observations, calls a tool  |
|            |       | or answers in JSON                                       |
| tools      | code  | runs the model's tool calls: 5 at most, memos wrapped,  |
|            |       | raw results stashed in the pool                          |
| summarise  | model | only when the 5-call budget runs out; answers tool-free  |
| check      | code  | guardrails.build_response: evidence attach, cross-line  |
|            |       | block, deterministic ordering                            |

The model and the Bank tools travel in the run's Runtime context — never
serialised into state — so the graph can be compiled with
checkpointer=False and run inside the case graph without inheriting its
Postgres checkpointer. In LangFuse the case trace shows cash_flow and its
step names inside it.
"""

from __future__ import annotations

import json
import logging
import operator
import os
import time
from typing import Annotated, Any, TypedDict

import httpx
import jwt
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from langgraph.types import Command

from app.agents.cash_flow import guardrails, prompts
from app.agents.cash_flow.tools import build_tools
from contracts import Evidence, Finding, InvestigateRequest, InvestigateResponse

log = logging.getLogger(__name__)

MAX_TOOL_CALLS = 5     # per API doc §10
MAX_SECONDS = 60       # per API doc §10 (soft cap, checked between steps)
TOKEN_TTL_SECONDS = 180
# Longest run: 5 investigate + 5 tools + 1 summarise + 1 check = 12. Slack.
RECURSION_LIMIT = 20


# ---------------------------------------------------------------------------
# state and context
# ---------------------------------------------------------------------------


def _first_kept(old: dict, new: dict) -> dict:
    """Records keep their first value: the same id never changes mid-run.

    Deterministic aggregate ids rely on this — the same inputs produce the
    same id, and the response's evidence stays consistent across repeated
    tool calls.
    """
    return {**new, **old}


class AgentState(TypedDict, total=False):
    """What the graph tracks across nodes. Serialisable."""

    messages: Annotated[list[AnyMessage], add_messages]
    # Bank's raw tool results, keyed by evidence id. Reconstituted as an
    # EvidencePool by the check node for guardrails.build_response.
    records: Annotated[dict[str, dict], _first_kept]
    calls: int
    budget_exhausted: bool
    deadline: float  # time.monotonic() after which no node starts
    # Reasons the response should carry in could_not_determine. operator.add
    # lets any node append without stepping on another node's additions.
    unknown: Annotated[list[str], operator.add]
    # The model's final findings, as raw dicts; check parses them into
    # Finding models inside build_response.
    findings_raw: list[dict]
    suggested_next: str | None
    # The final InvestigateResponse, as a dict. Populated by check.
    response: dict[str, Any]


class AgentContext(TypedDict):
    """What one run carries. Never saved."""

    model: BaseChatModel
    tools_by_name: dict[str, Any]
    tools_list: list[Any]


# ---------------------------------------------------------------------------
# nodes
# ---------------------------------------------------------------------------


def _investigate(
    state: AgentState, runtime: Runtime[AgentContext]
) -> Command[Any]:
    """Model step: either call a tool or write a final JSON answer."""
    if time.monotonic() > state.get("deadline", float("inf")):
        return Command(
            update={"unknown": ["The 60-second budget ran out between steps."]},
            goto="check",
        )

    model = runtime.context["model"]
    tools_list = runtime.context["tools_list"]
    model_with_tools = model.bind_tools(tools_list)

    try:
        reply = model_with_tools.invoke(state["messages"])
    except Exception as error:  # noqa: BLE001
        log.warning("model invoke raised: %s", error)
        return Command(
            update={
                "messages": [],  # nothing to add, but keeps the key present
                "unknown": [f"The model call failed: {error}"],
            },
            goto="check",
        )

    if not getattr(reply, "tool_calls", None):
        # Final answer: parse and head to the check node.
        findings, suggested = _findings_from_message(reply)
        return Command(
            update={
                "messages": [reply],
                "findings_raw": [f.model_dump() for f in findings],
                "suggested_next": suggested,
            },
            goto="check",
        )

    return Command(update={"messages": [reply]}, goto="tools")


def _tools(state: AgentState, runtime: Runtime[AgentContext]) -> Command[Any]:
    """Code step: run each tool call the model emitted, within budget."""
    tools_by_name = runtime.context["tools_by_name"]
    last = state["messages"][-1]
    calls_made = state.get("calls", 0)
    budget_exhausted = state.get("budget_exhausted", False)

    tool_messages: list[AnyMessage] = []
    new_records: dict[str, dict] = {}

    for call in last.tool_calls or []:
        # Every call in the batch must be answered, even over budget — Anthropic
        # and OpenAI both reject a follow-up invoke if any tool_use/tool_call in
        # the previous AIMessage was left unanswered.
        if calls_made >= MAX_TOOL_CALLS:
            budget_exhausted = True
            observation: dict = {
                "error": "budget_exhausted",
                "message": "The 5-call budget was reached before this call.",
            }
        else:
            name = call["name"]
            args = call.get("args") or {}
            tool = tools_by_name.get(name)
            if tool is None:
                observation = {"error": "unknown_tool", "message": f"No such tool: {name}"}
            else:
                try:
                    raw = tool.invoke(args)
                    # Pool keeps Bank's raw result; model sees sanitised copy.
                    # The Orchestrator's downstream (facts.shown with
                    # untrusted=True) applies its own text sanitation, so
                    # raw memo text never flows unescorted into a model.
                    for item in (raw.get("evidence") or []) if isinstance(raw, dict) else []:
                        if isinstance(item, dict) and item.get("id"):
                            new_records.setdefault(item["id"], item)
                    observation = guardrails.sanitize_tool_result(raw)
                except Exception as error:  # noqa: BLE001
                    log.warning("tool %s raised: %s", name, error)
                    observation = {"error": "tool_raised", "message": str(error)}
            calls_made += 1

        tool_call_id = call.get("id") or f"call_fallback_{id(call)}"
        tool_messages.append(
            ToolMessage(content=json.dumps(observation), tool_call_id=tool_call_id)
        )

    update: dict[str, Any] = {
        "messages": tool_messages,
        "records": new_records,
        "calls": calls_made,
    }
    if budget_exhausted:
        update["budget_exhausted"] = True
        update["unknown"] = ["Reached the 5-tool-call budget; summarising from what is known."]
        return Command(update=update, goto="summarise")
    return Command(update=update, goto="investigate")


def _summarise(
    state: AgentState, runtime: Runtime[AgentContext]
) -> Command[Any]:
    """Model step: budget is spent — write the final answer tool-free."""
    model = runtime.context["model"]
    nudge = HumanMessage(
        content=(
            "You have used your tool-call budget. Do not call any more tools. "
            "Write your final answer as JSON from the evidence you already have."
        )
    )
    try:
        final = model.invoke(state["messages"] + [nudge])
    except Exception as error:  # noqa: BLE001
        log.warning("summary invoke raised: %s", error)
        return Command(
            update={"unknown": [f"The summary call failed: {error}"]},
            goto="check",
        )

    findings, suggested = _findings_from_message(final)
    return Command(
        update={
            "messages": [nudge, final],
            "findings_raw": [f.model_dump() for f in findings],
            "suggested_next": suggested,
        },
        goto="check",
    )


def _check(state: AgentState, runtime: Runtime[AgentContext]) -> dict[str, Any]:
    """Code step: run guardrails over the model's findings and the pool."""
    pool = guardrails.EvidencePool()
    for eid, data in state.get("records", {}).items():
        try:
            pool.records[eid] = Evidence.model_validate(data)
        except Exception:  # noqa: BLE001
            # A malformed evidence item is simply not addable.
            continue

    findings: list[Finding] = []
    for raw in state.get("findings_raw", []) or []:
        try:
            findings.append(Finding.model_validate(raw))
        except Exception:  # noqa: BLE001
            continue

    response = guardrails.build_response(
        findings=findings,
        pool=pool,
        could_not_determine=state.get("unknown", []),
        suggested_next=state.get("suggested_next"),
    )
    return {"response": response.model_dump()}


# ---------------------------------------------------------------------------
# graph construction
# ---------------------------------------------------------------------------


def _build_graph():
    builder = StateGraph(AgentState, context_schema=AgentContext)
    builder.add_node("investigate", _investigate)
    builder.add_node("tools", _tools)
    builder.add_node("summarise", _summarise)
    builder.add_node("check", _check)
    builder.add_edge(START, "investigate")
    builder.add_edge("check", END)
    # Compile without a checkpointer: run inside the case graph, this does
    # not inherit the case graph's Postgres checkpointer, and it saves nothing.
    return builder.compile(checkpointer=False)


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
    """Public entry. Compile is at import time; this just invokes.

    Tests pass `model` (a scripted fake) and `http` (an httpx.Client wired to
    a MockTransport). Production leaves both at None and the agent reads
    LLM_MODEL / BANK_URL from the environment.
    """
    if model is None:
        model = _default_model()
    if model is None:
        # No model available — mirror app.runtime's "no LLM_MODEL" behaviour.
        return _cannot("The Cash Flow agent has no model configured (LLM_MODEL is unset).")

    try:
        token = _mint_service_token(customer_id=request.customer_id, case_id=request.case_id)
    except RuntimeError as error:
        return _cannot(str(error))

    tools_list, close_http = build_tools(
        customer_id=request.customer_id, token=token, http=http
    )
    tools_by_name = {tool.name: tool for tool in tools_list}

    initial_state: AgentState = {
        "messages": [
            SystemMessage(content=prompts.SYSTEM_PROMPT),
            HumanMessage(
                content=prompts.user_message(
                    question=request.question,
                    topic=request.topic,
                    window_start=request.window_start.isoformat(),
                    window_end=request.window_end.isoformat(),
                    context=request.context,
                    account_id=request.account_id,
                    record_id=request.record_id,
                )
            ),
        ],
        "records": {},
        "calls": 0,
        "budget_exhausted": False,
        "deadline": time.monotonic() + MAX_SECONDS,
        "unknown": [],
        "findings_raw": [],
        "suggested_next": None,
    }

    context: AgentContext = {
        "model": model,
        "tools_by_name": tools_by_name,
        "tools_list": tools_list,
    }

    try:
        final_state = _GRAPH.invoke(
            initial_state,
            context=context,
            config={"recursion_limit": RECURSION_LIMIT},
        )
        return InvestigateResponse.model_validate(final_state["response"])
    finally:
        close_http()


# ---------------------------------------------------------------------------
# helpers (unchanged from the previous hand-rolled loop)
# ---------------------------------------------------------------------------


def _findings_from_message(message: AIMessage) -> tuple[list[Finding], str | None]:
    """Extract Findings from the model's final text.

    Expected format in the final AIMessage.content (JSON object):

        {"findings": [{"claim": "...", "confidence": "high", "evidence_ids": [...]}],
         "suggested_next": "..."}

    If parsing fails, return empty findings so the guardrail layer records the
    reason via could_not_determine.
    """
    content = _flatten_content(message.content)
    try:
        payload = json.loads(_extract_json(content))
    except (ValueError, TypeError):
        return [], None
    raw_findings = payload.get("findings") or []
    findings: list[Finding] = []
    for raw in raw_findings:
        try:
            findings.append(Finding.model_validate(raw))
        except Exception:  # noqa: BLE001
            continue
    suggested = payload.get("suggested_next")
    return findings, suggested if isinstance(suggested, str) and suggested else None


def _flatten_content(content: Any) -> str:
    """Normalise message.content to a plain string.

    Anthropic chat models via LangChain often return a list of blocks like
    ``[{"type": "text", "text": "..."}]``. OpenAI returns a plain string.
    Join the text blocks and ignore non-text ones (images, tool_use blocks
    land in message.tool_calls, not here).
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
    # Precise fence stripping: handle ```json and ``` without eating stray
    # backticks elsewhere in the content.
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _mint_service_token(*, customer_id: str, case_id: str) -> str:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise RuntimeError("JWT_SECRET is not set; the agent cannot call Bank")
    now = int(time.time())
    claims = {
        "sub": f"cash_flow:{case_id}",
        "role": "service",
        "customers": [customer_id],
        "exp": now + TOKEN_TTL_SECONDS,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def _default_model() -> BaseChatModel | None:
    # Import lazily so tests can run without the orchestrator's runtime module.
    try:
        from app.runtime import _chat_model  # type: ignore[attr-defined]
    except Exception:
        return None
    try:
        return _chat_model()
    except Exception:
        return None


def _cannot(reason: str) -> InvestigateResponse:
    return InvestigateResponse(
        agent="cash_flow",
        findings=[],
        evidence=[],
        could_not_determine=[reason],
    )
