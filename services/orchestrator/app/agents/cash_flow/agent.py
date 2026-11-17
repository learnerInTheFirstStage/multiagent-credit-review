"""The Cash Flow agent's run loop.

A standard tool-calling agent built on LangGraph's prebuilt tool-calling node,
bounded by code: at most 5 tool calls and 60 seconds per run. Code owns the
budget and the evidence attach — the model picks which tool to call and writes
the final claims.

The model is read from LLM_MODEL (via app.runtime._chat_model), unless the
caller injects one (tests do this). The service token is minted here — short,
scoped to one customer, read-only — so the agent and the mock are drop-in
replaceable by the graph: neither needs an external token.

Public entry point: cash_flow(request).
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Iterable

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

from app.agents.cash_flow import guardrails, prompts
from app.agents.cash_flow.tools import build_tools
from contracts import Finding, InvestigateRequest, InvestigateResponse

log = logging.getLogger(__name__)

MAX_TOOL_CALLS = 5     # per API doc §10
MAX_SECONDS = 60       # per API doc §10
TOKEN_TTL_SECONDS = 180


def cash_flow(
    request: InvestigateRequest,
    *,
    model: BaseChatModel | None = None,
    http: httpx.Client | None = None,
) -> InvestigateResponse:
    """Entry point. Build the loop, run it, assemble the response.

    Tests pass `model` (a scripted fake) and `http` (an httpx.Client wired to a
    MockTransport). Production leaves both at None and the agent reads
    LLM_MODEL / BANK_URL from the environment.
    """
    if model is None:
        model = _default_model()
    if model is None:
        # No model available — mirror app.runtime's "no LLM_MODEL" behaviour.
        return _cannot("The Cash Flow agent has no model configured (LLM_MODEL is unset).")

    token = _mint_service_token(customer_id=request.customer_id, case_id=request.case_id)
    tools = build_tools(customer_id=request.customer_id, token=token, http=http)
    tools_by_name = {tool.name: tool for tool in tools}

    pool = guardrails.EvidencePool()
    model_with_tools = model.bind_tools(tools)

    messages: list[AnyMessage] = [
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
    ]

    could_not_determine: list[str] = []
    suggested_next: str | None = None
    tool_calls_made = 0
    deadline = time.monotonic() + MAX_SECONDS

    while True:
        if time.monotonic() > deadline:
            could_not_determine.append("The 60-second budget ran out before a final answer.")
            break

        reply = model_with_tools.invoke(messages)
        messages.append(reply)

        tool_calls = getattr(reply, "tool_calls", None) or []
        if not tool_calls:
            # The model is done. Parse its final answer for findings.
            findings, suggested_next = _findings_from_message(reply)
            return guardrails.build_response(
                findings=findings,
                pool=pool,
                could_not_determine=could_not_determine,
                suggested_next=suggested_next,
            )

        for call in tool_calls:
            if tool_calls_made >= MAX_TOOL_CALLS:
                could_not_determine.append("Reached the 5-tool-call budget.")
                return guardrails.build_response(
                    findings=[],
                    pool=pool,
                    could_not_determine=could_not_determine,
                    suggested_next=None,
                )

            name = call["name"]
            args = call.get("args") or {}
            tool = tools_by_name.get(name)
            if tool is None:
                observation = {"error": "unknown_tool", "message": f"No such tool: {name}"}
            else:
                try:
                    raw = tool.invoke(args)
                    observation = guardrails.sanitize_tool_result(raw)
                    pool.add(observation)
                except Exception as error:  # noqa: BLE001 — surface any tool-level problem
                    log.warning("tool %s raised: %s", name, error)
                    observation = {"error": "tool_raised", "message": str(error)}

            messages.append(
                ToolMessage(
                    content=json.dumps(observation),
                    tool_call_id=call["id"],
                )
            )
            tool_calls_made += 1

    # Only reached when the time budget expired mid-loop.
    return guardrails.build_response(
        findings=[],
        pool=pool,
        could_not_determine=could_not_determine,
        suggested_next=None,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _findings_from_message(message: AIMessage) -> tuple[list[Finding], str | None]:
    """Extract Findings from the model's final text.

    Expected format in the final AIMessage.content (JSON object):

        {"findings": [{"claim": "...", "confidence": "high", "evidence_ids": [...]}],
         "suggested_next": "..."}

    If parsing fails, return empty findings so the guardrail layer records the
    reason via could_not_determine.
    """
    content = message.content if isinstance(message.content, str) else ""
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


def _extract_json(text: str) -> str:
    """Pull the first JSON object out of a text blob — the model may wrap it
    in prose or a ```json fence."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
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
