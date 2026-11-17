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
    LangGraph knows how to save.
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
