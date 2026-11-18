"""When a specialist's service gave nothing at all: every call the agent made for the request failed as an
outage (see outage). That is neither an answer nor "no answer": the case stops before the question and waits
(CaseWaiting, app.runner)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import PrivateAttr

from contracts import InvestigateResponse

# What retrying can do about an outage. transient: the service is down or slow, and may be back soon. configuration:
# the service refuses this Orchestrator (a token it does not accept, a URL it does not serve) until someone fixes the
# setup. data: the service does not know the customer, and asking again gets the same answer.
OutageKind = Literal["transient", "configuration", "data"]
_KIND_OF = {"unreachable": "transient", "timeout": "transient", "unknown_customer": "data",
            "http_401": "configuration", "http_403": "configuration", "http_404": "configuration"}  # fmt: skip
# When several codes were met, the one retrying helps least decides.
_LEAST_HELPED = ("data", "configuration", "transient")
_CODES = re.compile(r"\(([^()]*)\)\s*$")


class CaseWaiting(Exception):
    """Raised out of the case graph when a specialist's service gave nothing at all.

    The graph's checkpoint stays before the question, so a later run of the case asks only that question again
    and keeps everything asked before it. app.runner decides whether the case waits or goes to a person.
    """

    def __init__(self, service: str, reason: str, *, asked_before: int = 0):
        self.service = service
        # How many questions the case had recorded: the outage is at the next one. A later outage at another
        # question is a new outage, with its own attempts (app.runner).
        self.asked_before = asked_before
        # The codes outage() gave, from the end of an Unanswered's reason: "... failed (http_503, timeout)".
        found = _CODES.search(reason)
        self.codes = [code.strip() for code in (found.group(1) if found else reason).split(",") if code.strip()]
        self.kind: OutageKind = kind_of(self.codes)
        super().__init__(f"{service} unavailable ({', '.join(self.codes)})")


def kind_of(codes: list[str]) -> OutageKind:
    """transient, configuration or data. A code nobody classified is configuration: a person is told."""
    kinds = {_KIND_OF.get(code) or ("transient" if re.fullmatch(r"http_5\d\d", code) else "configuration")
             for code in codes} or {"configuration"}
    return next(kind for kind in _LEAST_HELPED if kind in kinds)


class Unanswered(InvestigateResponse):
    """The answer an agent gives when its service gave nothing. To the case graph it stops the case (CaseWaiting),
    with `reason`; to any other caller (the agents' test sets) it is the plain answer it also is, which says in
    could_not_determine what failed. The reason holds error codes only, never text the service sent."""

    _reason: str = PrivateAttr(default="")

    @property
    def reason(self) -> str:
        return self._reason

    @classmethod
    def of(cls, response: InvestigateResponse, reason: str) -> Unanswered:
        unanswered = cls.model_validate(response.model_dump())
        unanswered._reason = reason
        return unanswered


def outage(status: int, detail: dict) -> str | None:
    """The code of an HTTP answer that says the service cannot serve this customer at all, or None: a refused
    token (401, 403), a failure (5xx), an unknown customer, or a path the service does not have (a 404 with
    no error body). A 404 for one record (an unknown transaction) and a 422 are answers about the request."""
    if status in (401, 403) or status >= 500:
        return f"http_{status}"
    if status == 404 and not detail:
        return "http_404"
    if status == 404 and str(detail.get("message", "")).startswith("Unknown customer"):
        return "unknown_customer"
    return None
