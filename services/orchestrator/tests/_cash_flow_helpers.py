"""Test helpers for the Cash Flow agent: a scripted chat model and a Bank HTTP stub.

Shared across test_cash_flow_agent.py (schema, adversarial, contract layers).
"""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict


class ScriptedChatModel(BaseChatModel):
    """A chat model that returns a canned sequence of messages.

    Each item in the script is either:

    - a list of (tool_name, args) tuples → the step emits an AIMessage with
      those tool_calls (empty content);
    - a dict → the step emits an AIMessage whose content is JSON-encoded;
    - a str → the step emits an AIMessage whose content is that string.

    Running past the end raises. bind_tools is a no-op: the fake does not look
    at tool schemas.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    script: list[Any]
    step: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools, **kwargs):  # noqa: ARG002
        return self

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs) -> ChatResult:  # noqa: ARG002
        if self.step >= len(self.script):
            raise RuntimeError(f"ScriptedChatModel ran past its script ({len(self.script)} steps)")
        entry = self.script[self.step]
        self.step += 1

        if isinstance(entry, list):
            # tool-call step
            tool_calls = [
                {
                    "name": name,
                    "args": args,
                    "id": f"call_{self.step}_{idx}",
                    "type": "tool_call",
                }
                for idx, (name, args) in enumerate(entry)
            ]
            message = AIMessage(content="", tool_calls=tool_calls)
        elif isinstance(entry, dict):
            message = AIMessage(content=json.dumps(entry))
        else:
            message = AIMessage(content=str(entry))
        return ChatResult(generations=[ChatGeneration(message=message)])


class BankStub:
    """httpx.MockTransport factory for Bank tool endpoints.

    Build with .when("cash_summary", {...}) or .when("transactions", handler),
    then call .client() to get an httpx.Client that answers from the stubs.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    def when(
        self,
        tool: str,
        response: dict | None = None,
        *,
        status_code: int = 200,
        handler: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> "BankStub":
        if handler is not None:
            self._handlers[tool] = handler
        else:
            self._handlers[tool] = lambda _req, _r=response, _sc=status_code: httpx.Response(_sc, json=_r)
        return self

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        tool = request.url.path.rsplit("/", 1)[-1]
        handler = self._handlers.get(tool)
        if handler is None:
            return httpx.Response(
                404,
                json={"error": "stub_missing", "message": f"no stub registered for {tool}"},
            )
        return handler(request)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._dispatch))


# --- small payload builders --------------------------------------------------


def request_payload(
    *,
    case_id: str = "case_test",
    customer_id: str = "c_002",
    question: str = "Where did the money go?",
    window_start: str = "2026-11-03",
    window_end: str = "2026-11-10",
    context: str = "",
    topic: str | None = None,
    account_id: str | None = None,
    record_id: str | None = None,
) -> dict:
    """A fully-formed InvestigateRequest payload for tests."""
    payload: dict[str, Any] = {
        "case_id": case_id,
        "customer_id": customer_id,
        "question": question,
        "window_start": window_start,
        "window_end": window_end,
        "context": context,
    }
    if topic is not None:
        payload["topic"] = topic
    if account_id is not None:
        payload["account_id"] = account_id
    if record_id is not None:
        payload["record_id"] = record_id
    return payload


def evidence(id: str, kind: str, **data) -> dict:
    return {"id": id, "kind": kind, "data": data}


def bank_tool_response(data: dict, evidence_items: list[dict]) -> dict:
    return {"data": data, "evidence": evidence_items}
