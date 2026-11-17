"""The case graph: nine steps from a stored signal to an approved action.

Code fixes the order of the steps. The model decides three things: which
question to ask next, whether anything is still unexplained, and what the
report says. A person decides whether anything changes.
"""

import json
import logging
from collections.abc import Callable
from datetime import date, timedelta
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field, ValidationError

from app.graph import prompts
from app.graph.report import ReportDraft
from app.graph.state import CaseState
from contracts import InvestigateRequest, InvestigateResponse

log = logging.getLogger(__name__)

MAX_ROUNDS = 6  # questions to specialists per case
MAX_GAP_RETURNS = 2  # times step 3 may send the case back to step 2
MAX_DRAFTS = 2  # the report is redrafted once if the check fails
WINDOW_DAYS = 7
MANAGER_CUT = 0.5  # a cut above this share of the limit needs a manager

Specialist = Callable[[InvestigateRequest], InvestigateResponse]


class AskCashFlow(BaseModel):
    """Ask the Cash Flow agent one question about the customer's bank records."""

    question: str
    context: str = Field(default="", description="What it needs to know from earlier answers.")


class AskCreditBehavior(BaseModel):
    """Ask the Credit Behavior agent one question about the customer's card records."""

    question: str
    context: str = Field(default="", description="What it needs to know from earlier answers.")


AGENT_FOR_TOOL = {"AskCashFlow": "cash_flow", "AskCreditBehavior": "credit_behavior"}


class GapReview(BaseModel):
    """What the investigation has not explained yet."""

    gaps: list[str]


DECISION_SHAPE = 'A decision must look like {"decision": "approve" or "reject", "by": who decided}.'


def _person_decides(state: CaseState, step: str) -> dict:
    """Pause the case until a person approves or rejects.

    This runs again from its first line each time the case resumes, so it has no
    side effects. An answer that is not a decision pauses the case again. Raising
    instead would leave that answer stored as the reply to the pause for good.
    """
    shown = {"case_id": state["case_id"], "step": step, "report": state["report"]}
    while True:
        answer = interrupt(shown)
        if (
            isinstance(answer, dict)
            and answer.get("decision") in ("approve", "reject")
            and isinstance(answer.get("by"), str)
            and answer["by"]
        ):
            return {"step": step, "decision": answer["decision"], "by": answer["by"]}
        shown = {**shown, "problem": DECISION_SHAPE}


def _needs_manager(state: CaseState) -> bool:
    recommendation = state["report"]["recommendation"]
    if recommendation == "pause_card":
        return True
    current = state["signal"]["current_limit_cents"]
    cut = 1 - state["signal"]["engine_limit_cents"] / current if current else 0
    return recommendation == "reduce_limit" and cut > MANAGER_CUT


def _report_problems(state: CaseState) -> list[str]:
    report = state["report"]
    if report is None:
        return [f"The output did not fit the report shape. {state.get('draft_error') or ''}".strip()]

    problems = []
    signal = state["signal"]
    recommendation = report["recommendation"]
    acts = recommendation != "needs_human"
    returned = {
        evidence["id"]
        for answer in state.get("answers", [])
        if "response" in answer
        for evidence in answer["response"]["evidence"]
    }
    cited = {
        evidence_id
        for part in ("evidence", "ruled_out", "other_findings", "review_reasons")
        for item in report[part]
        for evidence_id in item["evidence_ids"]
    }
    if unknown := sorted(cited - returned):
        problems.append(f"It cites evidence no specialist returned: {', '.join(unknown)}.")
    if acts and not report["evidence"]:
        problems.append(f"It recommends {recommendation} without citing evidence.")

    addressed = {item["reason"] for item in report["review_reasons"]}
    if missing := sorted(set(signal["review_reasons"]) - addressed):
        problems.append(f"It does not address the review reason: {', '.join(missing)}.")

    allows = {guideline["id"]: guideline["allows"] for guideline in state["guidelines"]}
    cited_guidelines = report["guideline_ids"]
    if unknown := sorted(set(cited_guidelines) - set(allows)):
        problems.append(f"It cites a guideline that does not exist: {', '.join(unknown)}.")
    elif acts and not cited_guidelines:
        problems.append("It cites no guideline.")
    elif acts and not any(recommendation in allows[guideline_id] for guideline_id in cited_guidelines):
        problems.append(f"No guideline it cites allows {recommendation}.")

    # Step 9 sets the engine's limit, so a "reduction" must be one.
    if recommendation == "reduce_limit" and signal["engine_limit_cents"] >= signal["current_limit_cents"]:
        problems.append("It recommends reduce_limit, but the engine's limit is not below the current limit.")
    return problems


def build_graph(
    *,
    model: BaseChatModel,
    specialists: dict[str, Specialist],
    load_guidelines: Callable[[], list[dict]],
    carry_out: Callable[[dict], dict],
    checkpointer=None,
):
    """Build the graph.

    specialists maps "cash_flow" and "credit_behavior" to their entry functions.
    load_guidelines returns dicts with an "id" and the recommendations each "allows".
    carry_out sends one approved action to the Card Service and returns its answer.

    Start a case once per thread, then resume it with Command(resume=...). The
    caller must not invoke a thread with a signal twice: the answers add up.
    """
    investigator = model.bind_tools([AskCashFlow, AskCreditBehavior])
    gap_reviewer = model.with_structured_output(GapReview, include_raw=True)
    report_writer = model.with_structured_output(ReportDraft, include_raw=True)

    def open_case(state: CaseState) -> dict:
        signal = state["signal"]
        end = date.fromisoformat(signal["as_of"])
        window = {"start": (end - timedelta(days=WINDOW_DAYS)).isoformat(), "end": end.isoformat()}
        return {
            "status": "investigating",
            "window": window,
            "rounds": 0,
            "gap_passes": 0,
            "drafts": 0,
            "messages": [
                SystemMessage(prompts.INVESTIGATOR),
                HumanMessage(prompts.signal_brief(signal, window)),
            ],
        }

    def investigate(state: CaseState) -> dict:
        if state["rounds"] >= MAX_ROUNDS:
            return {}
        reply = investigator.invoke(state["messages"])
        if reply.invalid_tool_calls and not reply.tool_calls:
            # Read as "enough" for now. To settle on the first run with a real model.
            log.warning("case %s: the investigator's tool call could not be read", state["case_id"])
        return {"messages": [reply]}

    def after_investigate(state: CaseState) -> str:
        # At the round limit investigate adds no message, so the last one is a specialist's answer.
        last = state["messages"][-1]
        asked = isinstance(last, AIMessage) and bool(last.tool_calls)
        return "ask_specialist" if asked else "look_for_gaps"

    def ask_specialist(state: CaseState) -> dict:
        call, *extra_calls = state["messages"][-1].tool_calls
        agent = AGENT_FOR_TOOL.get(call["name"])
        asked = agent or call["name"]
        answer = {"agent": agent, "question": call["args"].get("question")}
        try:
            if agent is None:
                raise ValueError(f"there is no specialist behind the tool {call['name']}")
            request = InvestigateRequest(
                case_id=state["case_id"],
                customer_id=state["signal"]["customer_id"],
                question=call["args"].get("question", ""),
                window_start=state["window"]["start"],
                window_end=state["window"]["end"],
                context=call["args"].get("context", ""),
            )
            returned = specialists[agent](request)
            # Checked again here: an answer that breaks the rules must not reach the report.
            response = InvestigateResponse.model_validate(
                returned.model_dump() if isinstance(returned, BaseModel) else returned
            )
            if response.agent != agent:
                raise ValueError(f"{agent} was asked but {response.agent} answered")
            answer["response"] = response.model_dump(mode="json")
        except Exception as error:  # a specialist that fails is "no answer", the case goes on
            log.warning("case %s: %s gave no usable answer: %s", state["case_id"], asked, error)
            answer["error"] = f"No usable answer from {asked}."

        told = answer.get("response") or {"error": answer["error"]}
        messages = [ToolMessage(json.dumps(told), tool_call_id=call["id"])]
        messages += [
            ToolMessage("Not asked. Ask one question per turn.", tool_call_id=extra["id"])
            for extra in extra_calls
        ]
        return {"answers": [answer], "messages": messages, "rounds": state["rounds"] + 1}

    def _goes_back(gaps: list[str], rounds: int, gap_passes: int) -> bool:
        return bool(gaps) and rounds < MAX_ROUNDS and gap_passes <= MAX_GAP_RETURNS

    def look_for_gaps(state: CaseState) -> dict:
        brief = prompts.investigation_brief(state["signal"], state.get("answers", []))
        reply = gap_reviewer.invoke([SystemMessage(prompts.GAPS), HumanMessage(brief)])
        review = reply["parsed"]
        if review is None:
            # The case goes on: step 6 still checks that the report addresses every review reason.
            log.warning("case %s: the gap review gave no usable answer: %s", state["case_id"], reply["parsing_error"])
        gaps = review.gaps if review else []
        gap_passes = state["gap_passes"] + 1
        update = {"gaps": gaps, "gap_passes": gap_passes}
        if _goes_back(gaps, state["rounds"], gap_passes):
            update["messages"] = [HumanMessage("Still unexplained:\n" + "\n".join(f"- {gap}" for gap in gaps))]
        return update

    def after_gaps(state: CaseState) -> str:
        goes_back = _goes_back(state["gaps"], state["rounds"], state["gap_passes"])
        return "investigate" if goes_back else "fetch_guidelines"

    def fetch_guidelines(state: CaseState) -> dict:
        return {"guidelines": load_guidelines()}

    def draft_report(state: CaseState) -> dict:
        signal = state["signal"]
        brief = prompts.report_brief(
            signal,
            state.get("answers", []),
            state["guidelines"],
            state.get("check", {}).get("problems", []),
        )
        reply = report_writer.invoke([SystemMessage(prompts.REPORT), HumanMessage(brief)])
        draft = reply["parsed"]
        if draft is None:
            error = str(reply["parsing_error"] or "No report was returned.")
            log.warning("case %s: draft %d is not a report: %s", state["case_id"], state["drafts"] + 1, error)
            # The redraft is told what did not fit.
            return {"report": None, "draft_error": error[:1000], "drafts": state["drafts"] + 1}
        report = {
            "case_id": state["case_id"],
            "customer_id": signal["customer_id"],
            **draft.model_dump(mode="json"),
            "engine_limit_cents": signal["engine_limit_cents"],
        }
        return {"report": report, "draft_error": None, "drafts": state["drafts"] + 1}

    def check_report(state: CaseState) -> dict:
        problems = _report_problems(state)
        report = state["report"]
        out_of_drafts = state["drafts"] >= MAX_DRAFTS
        if problems and not out_of_drafts:
            return {"check": {"passed": False, "problems": problems}}

        if problems:
            # Two drafts failed. A person decides from what was found.
            report = {
                "case_id": state["case_id"],
                "customer_id": state["signal"]["customer_id"],
                "likely_cause": "no_clear_cause",
                "summary": "The report could not be completed. " + " ".join(problems),
                "evidence": [],
                "ruled_out": [],
                "other_findings": [],
                "review_reasons": [],
                "confidence": "low",
                "recommendation": "needs_human",
                "guideline_ids": [],
                "next_action": None,
                "engine_limit_cents": state["signal"]["engine_limit_cents"],
            }
        elif report["confidence"] == "low":
            report = {**report, "recommendation": "needs_human"}
        return {
            "check": {"passed": not problems, "problems": problems},
            "report": report,
            "status": "needs_approval",
        }

    def after_check(state: CaseState) -> str:
        done = state["check"]["passed"] or state["drafts"] >= MAX_DRAFTS
        return "analyst_approval" if done else "draft_report"

    def analyst_approval(
        state: CaseState,
    ) -> Command[Literal["manager_approval", "act", "__end__"]]:
        decision = _person_decides(state, "analyst")
        update = {"decisions": [decision]}
        if decision["decision"] == "reject":
            return Command(goto=END, update={**update, "status": "rejected"})
        if state["report"]["recommendation"] == "needs_human":
            # Nothing to carry out: the person decided by hand.
            return Command(goto=END, update={**update, "status": "done"})
        if _needs_manager(state):
            return Command(goto="manager_approval", update={**update, "status": "needs_manager"})
        return Command(goto="act", update=update)

    def manager_approval(state: CaseState) -> Command[Literal["act", "__end__"]]:
        decision = _person_decides(state, "manager")
        update = {"decisions": [decision]}
        if decision["decision"] == "reject":
            return Command(goto=END, update={**update, "status": "rejected"})
        return Command(goto="act", update=update)

    def act(state: CaseState) -> dict:
        # Its own step: a resumed approval step runs again from its first line, this one does not.
        recommendation = state["report"]["recommendation"]
        action = None
        if recommendation == "reduce_limit":
            action = {"type": "reduce_limit", "limit_cents": state["report"]["engine_limit_cents"]}
        elif recommendation == "pause_card":
            action = {"type": "pause_card"}
        if action is not None:
            action |= {
                "case_id": state["case_id"],
                "customer_id": state["signal"]["customer_id"],
                "approved_by": state["decisions"][-1]["by"],
            }
            action["result"] = carry_out(action)
        return {"action": action, "status": "done"}

    graph = StateGraph(CaseState)
    graph.add_node("open_case", open_case)
    graph.add_node("investigate", investigate)
    graph.add_node("ask_specialist", ask_specialist)
    graph.add_node("look_for_gaps", look_for_gaps)
    graph.add_node("fetch_guidelines", fetch_guidelines)
    graph.add_node("draft_report", draft_report)
    graph.add_node("check_report", check_report)
    graph.add_node("analyst_approval", analyst_approval)
    graph.add_node("manager_approval", manager_approval)
    graph.add_node("act", act)

    graph.add_edge(START, "open_case")
    graph.add_edge("open_case", "investigate")
    graph.add_conditional_edges("investigate", after_investigate, ["ask_specialist", "look_for_gaps"])
    graph.add_edge("ask_specialist", "investigate")
    graph.add_conditional_edges("look_for_gaps", after_gaps, ["investigate", "fetch_guidelines"])
    graph.add_edge("fetch_guidelines", "draft_report")
    graph.add_edge("draft_report", "check_report")
    graph.add_conditional_edges("check_report", after_check, ["draft_report", "analyst_approval"])
    graph.add_edge("act", END)
    return graph.compile(checkpointer=checkpointer)
