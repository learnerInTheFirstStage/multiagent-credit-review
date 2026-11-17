"""The Card Service agent's run (KAN-26): the Credit Behavior specialist.

    card_service(request) -> InvestigateResponse

Same shape as mock.credit_behavior, so the graph can swap one for the other (KAN-36). Each call
is self-contained: the agent keeps nothing between calls.

The steps, and who does each:
1. Preflight (code). Calls the tools the topic rests on (PREFLIGHT), so the facts that matter are
   fetched whatever the model does. These calls do not count against the model's budget.
2. Investigate (model). A standard tool-calling loop over the six Card tools: at most 5 calls
   and 60 seconds. A repeated call gets the earlier result and is not counted again.
3. Check (code, guardrails.check). Every cited id was returned by a tool, every $ and % is in the
   cited records, no limit action is recommended, and too_short_to_tell is never called normal.
   What fails goes back to the model once; what still fails is left out and said so.
4. Hard facts (code). A failed or late repayment, or a change of the paying account, that no
   accepted finding cites gets a finding written by code (findings.py).
5. Evidence (code). The response's evidence is the records the findings cite, as the tools
   returned them. The model never writes a record.

With no model configured (LLM_MODEL unset, and none passed in), steps 2 and 3 are skipped and the
answer is what code alone can say from the preflight records, with a note that it did so.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx
import jwt
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

from app.agents.card_service import findings as written
from app.agents.card_service import guardrails, prompts
from app.agents.card_service.tools import CardTools
from contracts import Finding, InvestigateRequest, InvestigateResponse

log = logging.getLogger(__name__)

# The specialist's name in the shared contract (contracts.investigate.AgentName) and in the graph.
AGENT = "credit_behavior"
# Who the agent is in the tokens it signs for the Card Service.
TOKEN_SUB = "card_service"
TOPICS = ("repayments", "paying_account", "card_use")
MAX_TOOL_CALLS = 5
MAX_SECONDS = 60
MAX_REPAIRS = 1
TOKEN_TTL_SECONDS = 180

# What code fetches before the model starts, per topic. None is "whatever you hold".
PREFLIGHT: dict[str | None, list[tuple[str, dict[str, Any]]]] = {
    "repayments": [("card_repayments", {"count": 3})],
    "paying_account": [("card_paying_account", {"count": 3})],
    "card_use": [("card_utilization", {}), ("card_spend_by_cycle", {})],
    None: [("card_utilization", {}), ("card_spend_by_cycle", {}), ("card_repayments", {"count": 3}),
           ("card_paying_account", {"count": 3})],
}


def card_service(
    request: InvestigateRequest,
    *,
    model: BaseChatModel | None = None,
    http: httpx.Client | None = None,
) -> InvestigateResponse:
    """Answer one question about one customer's card. Tests pass a scripted `model` and a stubbed `http`."""
    if request.topic is not None and request.topic not in TOPICS:
        return _cannot(f"The card side has no such topic. It has: {', '.join(TOPICS)}.")
    try:
        token = _service_token(request.customer_id, request.case_id)
    except RuntimeError as error:
        return _cannot(str(error))

    tools = CardTools(customer_id=request.customer_id, token=token, http=http)
    try:
        return _Run(request, tools, model if model is not None else _default_model()).answer()
    finally:
        tools.close()


class _Run:
    def __init__(self, request: InvestigateRequest, tools: CardTools, model: BaseChatModel | None):
        self.request, self.tools, self.model = request, tools, model
        self.pool = guardrails.EvidencePool()
        self.unknown: list[str] = []
        self.cache: dict[str, Any] = {}
        self.calls = 0
        self.deadline = time.monotonic() + MAX_SECONDS

    # --- tools ------------------------------------------------------------------------------------

    def run_tool(self, name: str, args: dict[str, Any], *, counted: bool = True) -> Any:
        """One tool call, through the cache and the budget. Returns what the model may read."""
        key = name + json.dumps(args, sort_keys=True, default=str)
        if key in self.cache:
            return self.cache[key]
        if counted:
            if self.calls >= MAX_TOOL_CALLS:
                return {"error": "budget_exhausted", "message": f"The {MAX_TOOL_CALLS}-call budget is used up."}
            self.calls += 1
        raw = self.tools.call(name, **args)
        self.pool.add(raw)
        shown = guardrails.for_the_model(raw)
        self.cache[key] = shown
        return shown

    def preflight(self) -> dict[str, Any]:
        results = {}
        for name, args in PREFLIGHT[self.request.topic]:
            result = self.run_tool(name, args, counted=False)
            results[name] = result
            if isinstance(result, dict) and "error" in result:
                self.unknown.append(f"The Card Service did not answer {name}: {result['error']}: {result.get('message', '')}".strip())
        return results

    # --- the run ----------------------------------------------------------------------------------

    def answer(self) -> InvestigateResponse:
        preflight = self.preflight()
        if not self.pool.records:
            # Nothing came back at all: no finding can rest on anything.
            return self._response([], suggested_next=None)
        if self.model is None:
            self.unknown.append("No model is configured, so these findings were written by code from the card records alone.")
            return self._response(written.from_records(self.pool), suggested_next=None)
        draft, suggested = self._investigate(preflight)
        checked = guardrails.check(draft, self.pool)
        for _ in range(MAX_REPAIRS):
            if not checked.problems or time.monotonic() > self.deadline:
                break
            redraft, suggested = self._repair(checked.problems)
            if redraft is None:
                break
            checked = guardrails.check(redraft, self.pool)
        self.unknown += checked.dropped
        if suggested and guardrails.recommends_an_action(suggested):
            self.unknown.append("A suggested next step was left out because it recommended an action.")
            suggested = None
        return self._response(checked.accepted, suggested_next=suggested)

    def _investigate(self, preflight: dict[str, Any]) -> tuple[list[Finding], str | None]:
        r = self.request
        self.messages: list[AnyMessage] = [
            SystemMessage(content=prompts.SYSTEM_PROMPT),
            HumanMessage(content=prompts.first_message(
                question=r.question, topic=r.topic, window_start=r.window_start.isoformat(),
                window_end=r.window_end.isoformat(), context=r.context, account_id=r.account_id,
                record_id=r.record_id, preflight=preflight)),
        ]
        bound = self.model.bind_tools(self.tools.langchain_tools(lambda name, args: self.run_tool(name, args)))
        while True:
            if time.monotonic() > self.deadline:
                self.unknown.append(f"The {MAX_SECONDS}-second budget ran out before the model finished.")
                return [], None
            try:
                reply = bound.invoke(self.messages)
            except Exception as error:  # noqa: BLE001 - a provider error must not crash the case
                log.warning("card_service model call failed: %s", error)
                self.unknown.append(f"The model call failed: {type(error).__name__}.")
                return [], None
            self.messages.append(reply)
            calls = getattr(reply, "tool_calls", None) or []
            if not calls:
                return self._read(reply)
            over = False
            for call in calls:
                # Every call gets an answer, or the provider refuses the next turn.
                if self.calls >= MAX_TOOL_CALLS and not self._cached(call):
                    over = True
                    result: Any = {"error": "budget_exhausted", "message": f"The {MAX_TOOL_CALLS}-call budget is used up."}
                else:
                    result = self.run_tool(call["name"], call.get("args") or {})
                self.messages.append(ToolMessage(content=json.dumps(result, default=str),
                                                 tool_call_id=call.get("id") or f"call_{id(call)}"))
            if over:
                self.unknown.append(f"The {MAX_TOOL_CALLS}-call budget ran out; the answer rests on what was fetched.")
                self.messages.append(HumanMessage(content="The tool budget is used up. Do not call tools. "
                                                          "Write your answer as one JSON object now."))
                return self._final()

    def _cached(self, call: dict) -> bool:
        return call["name"] + json.dumps(call.get("args") or {}, sort_keys=True, default=str) in self.cache

    def _final(self) -> tuple[list[Finding], str | None]:
        try:
            return self._read(self.model.invoke(self.messages))
        except Exception as error:  # noqa: BLE001
            self.unknown.append(f"The model call failed: {type(error).__name__}.")
            return [], None

    def _repair(self, problems: list[str]) -> tuple[list[Finding] | None, str | None]:
        self.messages.append(HumanMessage(content=prompts.repair_message(problems)))
        try:
            reply = self.model.invoke(self.messages)
        except Exception as error:  # noqa: BLE001
            self.unknown.append(f"The model call failed: {type(error).__name__}.")
            return None, None
        self.messages.append(reply)
        return self._read(reply)

    def _read(self, message: AIMessage) -> tuple[list[Finding], str | None]:
        """The model's JSON answer. What it cannot say goes to could_not_determine."""
        try:
            payload = json.loads(_json_part(_text(message.content)))
        except (TypeError, ValueError):
            self.unknown.append("The model's answer was not the JSON asked for.")
            return [], None
        if not isinstance(payload, dict):
            self.unknown.append("The model's answer was not the JSON asked for.")
            return [], None
        drafts = []
        for raw in payload.get("findings") or []:
            try:
                drafts.append(Finding.model_validate(raw))
            except ValueError:
                self.unknown.append("A finding was left out because it was not in the shape asked for.")
        for item in payload.get("could_not_determine") or []:
            if isinstance(item, str) and item.strip():
                self.unknown.append(" ".join(item.split())[:300])
        suggested = payload.get("suggested_next")
        return drafts, (" ".join(suggested.split())[:300] if isinstance(suggested, str) and suggested.strip() else None)

    # --- the response -----------------------------------------------------------------------------

    def _response(self, accepted: list[Finding], *, suggested_next: str | None) -> InvestigateResponse:
        cited = {i for f in accepted for i in f.evidence_ids}
        for record in written.hard_facts(self.pool):
            if record.id not in cited and (finding := written.for_hard_fact(record)) is not None:
                accepted.append(finding)
                cited.add(record.id)
        if any(guardrails.too_short(e) for e in self.pool.records.values()):
            note = "The card history is too short to tell what is usual for this customer."
            if note not in self.unknown:
                self.unknown.append(note)
        evidence = self.pool.pick([i for f in accepted for i in f.evidence_ids])
        unknown = list(dict.fromkeys(self.unknown))
        if not accepted and not unknown:
            unknown = ["The card records fetched did not answer the question."]
        return InvestigateResponse(agent=AGENT, findings=accepted, evidence=evidence,
                                   could_not_determine=unknown, suggested_next=suggested_next)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


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
