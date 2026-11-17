import operator
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class RunContext(TypedDict, total=False):
    """What one call to the graph brings with it. Unlike the state, it is never saved."""

    # The token of the person whose decision this call carries. Card checks it again.
    approver_token: str | None


class CaseState(TypedDict, total=False):
    """What the graph knows about one case. Saved after every step.

    Plain dicts and lists, apart from the investigator's messages, which
    LangGraph knows how to save. A case paused before a key was added has
    none of it, so every step that can run on such a case reads new keys
    with .get.
    """

    case_id: str
    signal: dict
    status: str
    # The dates the specialists look at: {"start": ..., "end": ...}.
    window: dict
    # The investigator's conversation: its tool calls and the specialists' answers.
    messages: Annotated[list[AnyMessage], add_messages]
    # One entry per question asked: the agent, the question, and its response or an error.
    answers: Annotated[list[dict], operator.add]
    rounds: int
    gaps: list[str]
    gap_passes: int
    guidelines: list[dict]
    report: dict | None
    # Why the last draft was not a report at all, when it was not.
    draft_error: str | None
    drafts: int
    check: dict
    decisions: Annotated[list[dict], operator.add]
    action: dict | None
    # The Orchestrator's own records, written once when the case opens: signal facts and earlier reviews.
    facts: list[dict]
    # One entry per specialist tool call, in step with `answers`: what was asked and what came back.
    asked: Annotated[list[dict], operator.add]
    # One entry per call of the search tool.
    searches: Annotated[list[dict], operator.add]
    # One entry per gap pass and per checked draft. For the path page and the grader.
    gap_log: Annotated[list[dict], operator.add]
    draft_log: Annotated[list[dict], operator.add]
    # Fixed flows only: the follow-up questions code added to its plan.
    plan_extra: Annotated[list[dict], operator.add]
    # Calls refused before they were sent. The first MAX_REFUSALS of a case use no round.
    refusals: int
    # "agent", "fixed" or "fixed_rules".
    flow: str
    # policy.POLICY_VERSION as it was when the case opened.
    policy_version: str
