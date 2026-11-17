"""Test helpers for the Credit Behavior agent: a Card HTTP stub that answers from recorded responses.

tests/fixtures/card_tools.json holds real Card Service responses (KAN-25), recorded from the
seeded stack, keyed "<customer>/<tool>". The scripted chat model is the Cash Flow tests' one.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable

import httpx

from tests._cash_flow_helpers import ScriptedChatModel  # noqa: F401  (re-exported)

RECORDED: dict[str, Any] = json.loads((Path(__file__).parent / "fixtures" / "card_tools.json").read_text())


class CardStub:
    """httpx.MockTransport over the recorded responses. Records every request it answers."""

    def __init__(self, customer_id: str) -> None:
        self.customer_id = customer_id
        self.requests: list[httpx.Request] = []
        self._overrides: dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    def when(self, tool: str, body: dict | None = None, *, status_code: int = 200,
             handler: Callable[[httpx.Request], httpx.Response] | None = None) -> "CardStub":
        self._overrides[tool] = handler or (lambda _req: httpx.Response(status_code, json=body))
        return self

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        tool = request.url.path.rsplit("/", 1)[-1]
        if tool in self._overrides:
            return self._overrides[tool](request)
        key = f"{self.customer_id}/{tool}"
        if key not in RECORDED:
            return httpx.Response(404, json={"detail": {"error": "not_found", "message": f"no recording for {key}"}})
        return httpx.Response(200, json=copy.deepcopy(RECORDED[key]))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._dispatch))

    def tools_called(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[-1] for r in self.requests]


def recorded(customer_id: str, tool: str) -> dict[str, Any]:
    return copy.deepcopy(RECORDED[f"{customer_id}/{tool}"])


def request(customer_id: str = "c_010", topic: str | None = None, **extra) -> dict[str, Any]:
    return {"case_id": "case_test", "customer_id": customer_id, "question": "What does the card side show?",
            "window_start": "2026-11-03", "window_end": "2026-11-10", "topic": topic, **extra}


def answer(*findings: tuple[str, list[str]], could_not_determine: list[str] | None = None,
           suggested_next: str | None = None, confidence: str = "high") -> dict[str, Any]:
    """The model's final JSON answer."""
    return {"findings": [{"claim": c, "confidence": confidence, "evidence_ids": ids} for c, ids in findings],
            "could_not_determine": could_not_determine or [], "suggested_next": suggested_next}
