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

from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field, ValidationError

from app import policy
from app.graph import facts, figures, needs, prompts
from app.graph.causes import CAUSE_NEEDS, CAUSES, OWN_KINDS, TOPIC_TESTS, TOPICS
from app.graph.report import ReportDraft
from app.graph.state import CaseState, RunContext
from contracts import InvestigateRequest, InvestigateResponse

log = logging.getLogger(__name__)

MAX_ROUNDS = 6  # questions to specialists per case
MAX_REFUSALS = 3  # calls refused before they were sent that use no round
MAX_GAP_RETURNS = 2  # times step 3 may send the case back to step 2
MAX_DRAFTS = 2  # the report is redrafted once if the check fails
WINDOW_DAYS = 7
EARLIEST_DAYS = 400  # how far before the signal's day a question's dates may reach
MANAGER_CUT = 0.5  # a cut above this share of the limit needs a manager
NOTE_MAX = 500  # characters of a decision's note

Specialist = Callable[[InvestigateRequest], InvestigateResponse]

Cause = Literal[
    "payroll_or_tax", "planned_vendor_payment", "seasonal_dip", "funds_moved_out_of_view",
    "revenue_loss", "unexplained_outflow", "insufficient_data",
]  # fmt: skip


class AskCashFlow(BaseModel):
    """Ask the Cash Flow agent about one topic of the customer's bank records."""

    topic: Literal["transactions", "income", "payee_history", "accounts", "reconciliation", "prior_year"]
    question: str = Field(min_length=1)
    separates: list[Cause] = Field(min_length=1, description="The causes this answer will tell apart.")
    record_id: str | None = Field(
        default=None,
        description="An evidence id from an earlier answer. payee_history needs the id of one transaction.",
    )
    account_id: str | None = Field(
        default=None, description="One account, by an id the signal facts or an earlier answer named."
    )
    start: str | None = Field(
        default=None, description="First day to look at, as YYYY-MM-DD. Leave out for the review window."
    )
    end: str | None = Field(default=None, description="Last day to look at.")


class AskCreditBehavior(BaseModel):
    """Ask the Credit Behavior agent about one topic of the customer's card records."""

    topic: Literal["repayments", "paying_account", "card_use"]
    question: str = Field(min_length=1)
    separates: list[Cause] = Field(min_length=1, description="The causes this answer will tell apart.")
    start: str | None = None
    end: str | None = None


TOOLS = {"AskCashFlow": AskCashFlow, "AskCreditBehavior": AskCreditBehavior}
AGENT_FOR_TOOL = {"AskCashFlow": "cash_flow", "AskCreditBehavior": "credit_behavior"}
# Ids only the Orchestrator gives (1.4). A specialist's record under one would forge the case's own facts.
_OWN_IDS = ("sig_", "hist_", "xs_")
_FOCUS = ("record_id", "account_id")
# The parts of a report whose items cite records.
_CITING = ("evidence", "ruled_out", "other_findings", "review_reasons")
UNREADABLE = "That call could not be read. Send it again."
# In the summary of a report given up on: the draft's figures are not repeated to the analyst.
FIGURES_NOT_SHOWN = "It stated figures that are not in the evidence it cited."
DECIDED = ("keep_limit", "reduce_limit", "pause_card", "request_documents")


class GapReview(BaseModel):
    """What the investigation has not explained yet."""

    gaps: list[str]


DECISION_SHAPE = 'A decision must look like {"decision": "approve" or "reject", "by": who decided}.'


def _person_decides(state: CaseState, step: str) -> dict:
    """Pause the case until a person approves or rejects.

    This runs again from its first line each time the case resumes, so it has no
    side effects. An answer that is not a decision pauses the case again. Raising
    instead would leave that answer stored as the reply to the pause for good.

    A note and what the person decided on a hand-over are kept only when given:
    a decision without them looks as it did before they existed. What the person
    decided is kept only where it means something (1.9): the analyst approving a
    report that hands the case over.
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
            decision = {"step": step, "decision": answer["decision"], "by": answer["by"]}
            note = answer.get("note")
            if isinstance(note, str) and (note := note.replace("\x00", "").strip()[:NOTE_MAX]):
                decision["note"] = note
            approves_a_hand_over = (
                step == "analyst"
                and decision["decision"] == "approve"
                and state["report"]["recommendation"] == "needs_human"
            )
            if approves_a_hand_over and answer.get("decided") in DECIDED:
                decision["decided"] = answer["decided"]
            return decision
        shown = {**shown, "problem": DECISION_SHAPE}


def _cut_off(reply) -> bool:
    """Whether the model ran out of room before it finished.

    It has to be asked: a provider's own structured output parses what did arrive,
    so a report cut short can still come back looking complete.
    """
    metadata = getattr(reply, "response_metadata", None) or {}
    return metadata.get("stop_reason") == "max_tokens" or metadata.get("finish_reason") == "length"


def _needs_manager(state: CaseState) -> bool:
    recommendation = state["report"]["recommendation"]
    if recommendation == "pause_card":
        return True
    current = state["signal"]["current_limit_cents"]
    cut = 1 - state["signal"]["engine_limit_cents"] / current if current else 0
    return recommendation == "reduce_limit" and cut > MANAGER_CUT


def _cited(report: dict) -> list[str]:
    """The ids a report cites, in its four parts, in order and once each."""
    ids = (record_id for part in _CITING for item in report[part] for record_id in item["evidence_ids"])
    return list(dict.fromkeys(ids))


def _reads_as_id(value: str) -> bool:
    """Whether a value a draft wrote where an id goes is shaped like an id (6.1, step 1).

    Code repeats no other such value: it is the draft's own text, and could carry a figure the model made up
    to the analyst, in the summary of a report given up on, or into draft_log.
    """
    return figures._IDS.fullmatch(value) is not None


def _named(unknown: list[str]) -> str:
    """Ids a draft cited that the case does not hold, as a sentence code writes names them."""
    named = [value for value in unknown if _reads_as_id(value)]
    if len(named) < len(unknown):
        named.append("text that is not an id")
    return ", ".join(named)


def _them(items: list) -> str:
    return "it" if len(items) == 1 else "them"


def _would_meet(need: dict, records: list[dict]) -> list[str]:
    """The ids of the case's records that would meet a need if the report cited them, or none."""
    matching = [record for record in records if needs.meets({**need, "share_at_least_bp": None}, [record])]
    return [record["id"] for record in matching] if matching and needs.meets(need, matching) else []


def _guideline_defects(
    state: CaseState, report: dict, ledger: dict[str, dict], cited_records: list[dict]
) -> list[tuple[str, str]]:
    """Rules 5 to 8 of 2.A: every defect of the guidelines a draft cites, as (code, sentence) pairs, told at once.

    Called when rules 1 to 4 are silent, so every cited id is a loaded guideline. The sentences name loaded
    guidelines, records of the ledger and the draft's own labels, never text the draft wrote, and never the
    guideline that fits or its action.
    """
    guidelines = state["guidelines"]
    by_id = {guideline["id"]: guideline for guideline in guidelines}
    recommendation = report["recommendation"]
    acts = recommendation != "needs_human"
    cited = [by_id[guideline_id] for guideline_id in dict.fromkeys(report["guideline_ids"])]
    records = list(ledger.values())
    found = []
    # The guidelines the report rests on: rules 7 and 8 leave out those that rule 5 or 6 tells it to drop.
    resting = cited
    if acts and policy.RULES["every_cited_guideline_allows"]:
        if others := [guideline for guideline in cited if recommendation not in guideline["allows"]]:
            found.append((
                "guideline_allows",
                f"It cites {', '.join(g['id'] for g in others)}, which {'does' if len(others) == 1 else 'do'} not "
                f"allow {recommendation}. Cite only the guidelines the recommendation rests on.",
            ))
            resting = [guideline for guideline in cited if guideline not in others]
    if not any(guideline.get("needs") for guideline in guidelines):
        return found

    if not acts and policy.RULES["hand_over_cites_a_guideline"]:
        if not cited or any("needs_human" not in guideline["allows"] for guideline in cited):
            hand_overs = ", ".join(g["id"] for g in guidelines if g["allows"] == ["needs_human"])
            found.append((
                "hand_over",
                f"It hands the case to a person. Cite the guideline that says so, and only that: {hand_overs}.",
            ))
            resting = [guideline for guideline in cited if "needs_human" in guideline["allows"]]

    asked = state.get("asked", [])
    for guideline in resting:
        unmet = needs.unmet(guideline, cited_records, guidelines=guidelines, known=records, asked=asked)
        if missing := [need for need in unmet if need["kind"] != "none_fits"]:
            says = [need["says"] for need in missing]
            sentence = (
                f"{guideline['id']} needs {' and '.join(says)}. The report cites no record that shows {_them(says)}."
            )
            if would := list(dict.fromkeys(i for need in missing for i in _would_meet(need, records))):
                sentence += f" {', '.join(would)} would: cite {_them(would)}."
            found.append(("guideline_need", sentence))
        if len(missing) == len(unmet):
            continue
        # none_fits, judged on the whole ledger and on what was asked.
        if needs.permitted(guidelines, records):
            found.append((
                "guideline_need",
                f"{guideline['id']} says no guideline fits, but the records of this case meet every need of a "
                "guideline that allows an action. Recommend what that guideline allows.",
            ))
        else:
            unasked = "; ".join(
                f"{ask['tool']}, topic {ask['args']['topic']}"
                + (f", record_id {ask['args']['record_id']}" if "record_id" in ask["args"] else "")
                for ask in needs.unsettled(guidelines, records, asked)
            )
            found.append((
                "not_settled",
                f"{guideline['id']} says no guideline fits, but that is not settled. A guideline that allows an "
                f"action needs a record nobody asked for: {unasked}. The report cannot say that none fits.",
            ))

    if acts:
        covered = {need["kind"] for guideline in resting for need in guideline.get("needs", [])}
        for entry in needs.triggers(guidelines, records):
            if entry["covered_by"] not in covered:
                found.append((
                    "guideline_cover",
                    f"{entry['says']} ({', '.join(entry['record_ids'])}). No guideline the report cites covers it. "
                    "Cite one whose needs include it.",
                ))
    return found


def _a_side_gave_nothing(state: CaseState, report: dict) -> bool:
    """For insufficient_data only: some specialist was sent questions and gave no usable answer to any of them.

    Only questions that went out count (outcome answered or no_answer). A refused call never asked that side.
    """
    if report["likely_cause"] != "insufficient_data":
        return False
    outcomes: dict[str, list[str]] = {}
    for entry in state.get("asked", []):
        if entry["outcome"] in ("answered", "no_answer"):
            outcomes.setdefault(entry["agent"], []).append(entry["outcome"])
    return any(all(outcome == "no_answer" for outcome in sent) for sent in outcomes.values())


def _report_problems(state: CaseState) -> list[tuple[list[str], str]]:
    """What is wrong with the draft, as (codes, sentence) pairs.

    The sentences go to the redraft and the check, the codes to draft_log.
    """
    report = state["report"]
    if report is None:
        sentence = f"The output did not fit the report shape. {state.get('draft_error') or ''}".strip()
        return [(["not_a_report"], sentence)]

    problems = []
    signal = state["signal"]
    recommendation = report["recommendation"]
    acts = recommendation != "needs_human"
    ledger = facts.ledger(state)
    cited_records = [ledger[record_id] for record_id in _cited(report) if record_id in ledger]
    if unknown := sorted(set(_cited(report)) - set(ledger)):
        problems.append((["evidence_unknown"], f"It cites evidence no specialist returned: {_named(unknown)}."))
    if acts and not report["evidence"]:
        problems.append((["no_evidence"], f"It recommends {recommendation} without citing evidence."))

    addressed = {item["reason"] for item in report["review_reasons"]}
    if missing := sorted(set(signal["review_reasons"]) - addressed):
        problems.append((["review_reason"], f"It does not address the review reason: {', '.join(missing)}."))

    allows = {guideline["id"]: guideline["allows"] for guideline in state["guidelines"]}
    cited_guidelines = report["guideline_ids"]
    if unknown := sorted(set(cited_guidelines) - set(allows)):
        problems.append((["guideline_unknown"], f"It cites a guideline that does not exist: {_named(unknown)}."))
    elif acts and not cited_guidelines:
        problems.append((["guideline_missing"], "It cites no guideline."))
    elif acts and (a_person_decides := [g for g in cited_guidelines if allows[g] == ["needs_human"]]):
        # Another guideline cited next to it must not outvote one that leaves the decision to a person.
        problems.append((
            ["guideline_person"],
            f"It cites {', '.join(a_person_decides)}, under which a person decides, "
            "so it may only recommend needs_human.",
        ))
    elif acts and not any(recommendation in allows[guideline_id] for guideline_id in cited_guidelines):
        problems.append((["guideline_allows"], f"No guideline it cites allows {recommendation}."))
    elif found := _guideline_defects(state, report, ledger, cited_records):
        # One line however many defects: with one redraft, a defect told only on the second draft is a give-up.
        problems.append(([code for code, _ in found], " ".join(sentence for _, sentence in found)))

    # Step 9 sets the engine's limit, so a "reduction" must be one.
    if recommendation == "reduce_limit" and signal["engine_limit_cents"] >= signal["current_limit_cents"]:
        problems.append((
            ["not_a_reduction"], "It recommends reduce_limit, but the engine's limit is not below the current limit."
        ))

    # A cause rests on a record of this case that shows it (2.D.4). no_clear_cause claims nothing.
    cause = report["likely_cause"]
    alternatives = CAUSE_NEEDS.get(cause, [])
    if (
        alternatives
        and not any(needs.meets(need, cited_records) for need in alternatives)
        and not _a_side_gave_nothing(state, report)
    ):
        problems.append((
            ["cause"],
            f"It gives {cause} as the likely cause, but cites no record that shows it. "
            f"One of these would: {'; '.join(need['says'] for need in alternatives)}.",
        ))

    # Last, so that the first problem keeps its meaning. A figure is backed only by a record the sentence cites (6.1).
    if refused := figures.made_up(report, ledger):
        by_part: dict[str, list[str]] = {}
        for part, written, elsewhere in refused:
            named = f" ({elsewhere} shows it and is not cited there)" if elsewhere else ""
            by_part.setdefault(part, []).append(written + named)
        listed = "; ".join(f"{part}: {', '.join(written)}" for part, written in by_part.items())
        problems.append((
            ["figure"],
            f"It states figures that are not in the evidence it cites for them: {listed}. "
            "State only figures the cited evidence shows, exactly as shown, or leave the figure out.",
        ))
    return problems


def _logged(draft: int, codes: list[str], report: dict | None, result: str, known: set[str]) -> dict:
    """One checked draft for draft_log: its labels and the ids it cited, never its prose.

    An id is kept when the case holds that record or guideline (known), or when it is shaped like an id.
    """

    def ids(values: list[str]) -> list[str]:
        return [value for value in values if value in known or _reads_as_id(value)]

    return {
        "draft": draft,
        "codes": codes,
        "result": result,
        "recommendation": report and report["recommendation"],
        "likely_cause": report and report["likely_cause"],
        "confidence": report and report["confidence"],
        "guideline_ids": ids(report["guideline_ids"]) if report else [],
        "cited": ids(_cited(report)) if report else [],
    }


def _found(answers: list[dict], ledger: dict[str, dict]) -> list[dict]:
    """The specialists' findings, for a report given up on: each one whose figures the records it cites show."""
    found = []
    for answer in answers:
        for finding in (answer.get("response") or {}).get("findings", []):
            cited = [ledger[record_id] for record_id in finding["evidence_ids"] if record_id in ledger]
            shown = figures.allowed_figures(cited)
            item = {"claim": finding["claim"], "evidence_ids": finding["evidence_ids"]}
            if not figures.not_backed(finding["claim"], shown) and item not in found:
                found.append(item)
    return found


def _not_a_report(error: Exception | None) -> str:
    """Which fields of a draft did not fit, by path and error type.

    Never the parser's own message: it quotes the whole draft, and a figure the model made up would ride
    along into the redraft and, on the last draft, into what the analyst reads.
    """
    if error is None:
        return "No report was returned."
    invalid = error if isinstance(error, ValidationError) else error.__cause__
    if not isinstance(invalid, ValidationError):
        return "It could not be read as a report."
    return "; ".join(
        f"{'.'.join(str(part) for part in problem['loc'])}: {problem['type']}" for problem in invalid.errors()[:10]
    )


def _why(error: ValueError, tool: str) -> str:
    """Why a call was not sent, in words code wrote. Never the call's own values, which pydantic's messages quote."""
    if not isinstance(error, ValidationError):
        return str(error)
    rules = {
        "topic": f"must be one of {', '.join(TOPICS[tool])}",
        "question": "say in words what is asked",
        "separates": f"name at least one cause, from {', '.join(CAUSES)}",
    }
    fields = dict.fromkeys(str(problem["loc"][0]) if problem["loc"] else "the call" for problem in error.errors())
    return "; ".join(f"{field}: {rules.get(field, 'must be text, or be left out')}" for field in fields)


def _window(ask: AskCashFlow | AskCreditBehavior, state: CaseState) -> dict:
    """The dates of this question: the review window, or the start and end it names."""
    if ask.start is None and ask.end is None:
        return state["window"]
    if ask.start is None or ask.end is None:
        raise ValueError("Give start and end together, or neither.")
    try:
        start, end = date.fromisoformat(ask.start), date.fromisoformat(ask.end)
    except ValueError:
        raise ValueError("start and end must be dates written as YYYY-MM-DD.") from None
    as_of = date.fromisoformat(state["signal"]["as_of"])
    if not as_of - timedelta(days=EARLIEST_DAYS) <= start <= end <= as_of:
        raise ValueError(
            "start must not be after end, end must not be after the signal's day, "
            "and start may reach back a little over a year from it."
        )
    return {"start": start.isoformat(), "end": end.isoformat()}


def _accounts(known: dict[str, dict]) -> set[str]:
    """The accounts the case has seen: those the signal lists or a bank answer links, where a listed payment
    went, and the accounts that paid the card before and after a change.

    Only values a model is shown as they are. Text a record holds there is shown marked or withheld, and as a
    question's focus it would reach the gap reviewer and the report writer unmarked.
    """
    seen = []
    for record in known.values():
        data = facts.shown(record)["data"]
        if record["kind"] == "account_sync":
            seen += [account.get("id") for account in data.get("accounts", []) if isinstance(account, dict)]
        elif record["kind"] == "linked_account":
            seen.append(record["id"])
        elif record["kind"] == "bank_transaction":
            seen.append(data.get("to_account"))
        elif record["kind"] == "funding_account_change":
            seen += [data.get("from"), data.get("to")]
    return {value for value in seen if isinstance(value, str)}


def _focus_is_known(ask: AskCashFlow | AskCreditBehavior, known: dict[str, dict]) -> None:
    record_id, account_id = (getattr(ask, key, None) for key in _FOCUS)
    if ask.topic == "payee_history":
        if record_id is None or known.get(record_id, {}).get("kind") != "bank_transaction":
            raise ValueError("payee_history needs record_id: the id of one transaction an earlier answer returned.")
    elif record_id is not None and record_id not in known:
        raise ValueError("record_id must be the id of a record this case holds.")
    if account_id is not None and account_id not in _accounts(known):
        raise ValueError("account_id must be an account the signal facts or an earlier answer named.")


def _context(state: CaseState) -> str:
    """What a specialist is told about the case. Written by code: no model's words reach it this way."""
    signal, window = state["signal"], state["window"]
    bank = signal["snapshot"]["bank"]
    change = bank["cash_now_cents"] - bank["cash_7d_ago_cents"]
    moved = "did not change"
    if change:
        moved = f"fell by {facts.usd(-change)}" if change < 0 else f"rose by {facts.usd(change)}"
    return (
        f"Linked cash {moved} between {window['start']} and {window['end']}. "
        f"Review reasons: {', '.join(signal['review_reasons'])}."
    )


def _usable(response: InvestigateResponse, known: dict[str, dict]) -> None:
    """Raises when an answer forges one of the case's own records, or changes one the case holds.

    One id means one payload, whichever side or topic returned it.
    """
    for item in response.evidence:
        if item.kind in OWN_KINDS or item.id.startswith(_OWN_IDS):
            raise ValueError(f"{item.id} is of a kind or an id only the Orchestrator gives")
        record = item.model_dump(mode="json")
        before = known.get(item.id)
        if before is not None and (before["kind"], before["data"]) != (record["kind"], record["data"]):
            raise ValueError(f"{item.id} came back with other content")


def _unread(message) -> list[ToolMessage]:
    """An answer to each tool call that could not be read. A provider refuses a call left without one."""
    return [
        ToolMessage(UNREADABLE, tool_call_id=bad["id"], status="error")
        for bad in getattr(message, "invalid_tool_calls", None) or []
        if bad.get("id")
    ]


def _ended_by(last) -> str:
    """How the questions ended, from the last message before the gap pass."""
    if not isinstance(last, AIMessage):
        return "round_limit"  # at the limit investigate adds no message: the last one is an answer
    if last.invalid_tool_calls:
        return "unreadable_call"
    return "done" if last.text.strip() == "DONE" else "prose"


def _own_facts(state: CaseState) -> list[dict]:
    """The case's own records for the briefs: signal facts, earlier reviews, and the cross-side match once made."""
    return [record for record in facts.ledger(state).values() if record["kind"] in OWN_KINDS]


def build_graph(
    *,
    model: BaseChatModel,
    specialists: dict[str, Specialist],
    load_guidelines: Callable[[], list[dict]],
    carry_out: Callable[[dict, str | None], dict],
    checkpointer=None,
    structured_output: dict | None = None,
    earlier_reviews: Callable[[str, str], list[dict]] = lambda customer_id, as_of: [],
):
    """Build the graph.

    specialists maps "cash_flow" and "credit_behavior" to their entry functions.
    load_guidelines returns dicts with an "id" and the recommendations each "allows".
    carry_out sends one approved action to the Card Service, with the approver's
    token from the call's context, and returns Card's answer.

    structured_output goes to the model's with_structured_output, for a provider's
    own way of returning a schema, such as {"method": "json_schema"}.

    earlier_reviews(customer_id, as_of) gives the customer's reviews before as_of,
    which become the hist_ records of the case. Left out, every case is a first
    review. It is not caught here: a lookup that fails must not read as "no history".

    Start a case once per thread, then resume it with Command(resume=...). The
    caller must not invoke a thread with a signal twice: the answers add up.
    """
    investigator = model.bind_tools([AskCashFlow, AskCreditBehavior], parallel_tool_calls=False)
    how = structured_output or {}
    gap_reviewer = model.with_structured_output(GapReview, include_raw=True, **how)
    report_writer = model.with_structured_output(ReportDraft, include_raw=True, **how)

    def open_case(state: CaseState) -> dict:
        signal = state["signal"]
        end = date.fromisoformat(signal["as_of"])
        window = {"start": (end - timedelta(days=WINDOW_DAYS)).isoformat(), "end": end.isoformat()}
        reviews = earlier_reviews(signal["customer_id"], signal["as_of"])
        case_facts = [*facts.signal_facts(signal, window), *facts.history_facts(reviews, signal)]
        # The investigator is shown what each guideline needs before its first question.
        guidelines = load_guidelines()
        return {
            "status": "investigating",
            "window": window,
            "guidelines": guidelines,
            "facts": case_facts,
            "flow": "agent",
            "policy_version": policy.POLICY_VERSION,
            "rounds": 0,
            "refusals": 0,
            "gap_passes": 0,
            "drafts": 0,
            "messages": [
                SystemMessage(prompts.INVESTIGATOR),
                HumanMessage(prompts.opening_brief(case_facts, guidelines, window)),
            ],
        }

    def investigate(state: CaseState) -> dict:
        if state["rounds"] >= MAX_ROUNDS:
            return {}
        reply = investigator.invoke(state["messages"])
        if _cut_off(reply):
            log.warning("case %s: the investigator's answer was cut off", state["case_id"])
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
        last = state["messages"][-1]
        call, *extra_calls = last.tool_calls
        agent = AGENT_FOR_TOOL.get(call["name"])
        asked = agent or call["name"]
        answer = {"agent": agent, "question": call["args"].get("question")}
        entry = {"tool": call["name"], "agent": agent, "outcome": "refused", "returned": []}
        known = facts.ledger(state)
        try:
            if agent is None:
                raise ValueError(f"there is no specialist behind the tool {call['name']}")
            # What a schema cannot say is checked here too. A call that fails is not sent, and the model
            # is told why in words code wrote.
            try:
                ask = TOOLS[call["name"]].model_validate(call["args"])
                window = _window(ask, state)
                _focus_is_known(ask, known)
                if figures.not_backed(ask.question, figures.allowed_figures(known.values()), loose=True):
                    raise ValueError(
                        "The question states a figure no record of the case shows. Name the record by its id."
                    )
            except ValueError as error:
                answer["error"] = f"Not asked: {_why(error, call['name'])}"
                raise
            entry |= {
                "topic": ask.topic,
                "separates": list(ask.separates),
                "tests": list(TOPIC_TESTS[ask.topic]),
                "outcome": "no_answer",
            }
            entry |= {key: value for key in _FOCUS if (value := getattr(ask, key, None))}
            if ask.start is not None:
                entry |= window
            request = InvestigateRequest(
                case_id=state["case_id"],
                customer_id=state["signal"]["customer_id"],
                question=ask.question,
                window_start=window["start"],
                window_end=window["end"],
                context=_context(state),
                topic=ask.topic,
                account_id=getattr(ask, "account_id", None),
                record_id=getattr(ask, "record_id", None),
            )
            returned = specialists[agent](request)
            # Checked again here: an answer that breaks the rules must not reach the report.
            response = InvestigateResponse.model_validate(
                returned.model_dump() if isinstance(returned, BaseModel) else returned
            )
            if response.agent != agent:
                raise ValueError(f"{agent} was asked but {response.agent} answered")
            _usable(response, known)
            answer["response"] = response.model_dump(mode="json")
            entry |= {"outcome": "answered", "returned": [item["id"] for item in answer["response"]["evidence"]]}
        except Exception as error:  # a specialist that fails is "no answer", the case goes on
            log.warning("case %s: %s gave no usable answer: %s", state["case_id"], asked, error)
            answer.setdefault("error", f"No usable answer from {asked}.")

        told = facts.shown_answer(answer, untrusted=True)
        if "response" in answer and "xs_account_match" not in known:
            # Neither side could say it alone: whether the money went to the account that now pays the card.
            if match := facts.ledger({**state, "answers": [*state.get("answers", []), answer]}).get("xs_account_match"):
                told["worked_out_by_code"] = facts.shown(match)
        status = "error" if "error" in answer else "success"
        messages = [ToolMessage(json.dumps(told), tool_call_id=call["id"], status=status)]
        messages += [
            ToolMessage("Not asked. Ask one question per turn.", tool_call_id=extra["id"])
            for extra in extra_calls
        ]
        messages += _unread(last)
        update = {"answers": [answer], "asked": [entry], "messages": messages}
        # A slip in the form of a call does not use up the questions. Bounded all the same: after
        # MAX_REFUSALS such slips, each one uses a round. A tool that does not exist always does.
        refusals = state.get("refusals", 0)
        if agent is not None and entry["outcome"] == "refused" and refusals < MAX_REFUSALS:
            update["refusals"] = refusals + 1
        else:
            update["rounds"] = state["rounds"] + 1
        return update

    def _goes_back(gaps: list[str], rounds: int, gap_passes: int) -> bool:
        return bool(gaps) and rounds < MAX_ROUNDS and gap_passes <= MAX_GAP_RETURNS

    def look_for_gaps(state: CaseState) -> dict:
        asked = state.get("asked", [])
        # Code looks first. What it finds goes back as it is, and the model's gap review is not called.
        found = needs.code_gaps(state.get("guidelines", []), list(facts.ledger(state).values()), asked)
        dropped = 0
        if found:
            gaps, by = [gap["text"] for gap in found], "code"
        else:
            tested = {cause for entry in asked if entry["outcome"] != "refused" for cause in entry.get("tests", [])}
            brief = prompts.investigation_brief(
                state["signal"]["review_reasons"],
                _own_facts(state),
                prompts.questions(asked, state.get("answers", [])),
                [cause for cause in CAUSES if cause not in tested],
            )
            try:
                reply = gap_reviewer.invoke([SystemMessage(prompts.GAPS), HumanMessage(brief)])
            except OutputParserException as error:  # prose, from a provider that raises on it
                reply = {"raw": None, "parsed": None, "parsing_error": error}
            review = None if _cut_off(reply["raw"]) else reply["parsed"]
            if review is None:
                # The case goes on: step 6 still checks that the report addresses every review reason.
                why = "it was cut off" if _cut_off(reply["raw"]) else reply["parsing_error"]
                log.warning("case %s: the gap review gave no usable answer: %s", state["case_id"], why)
            written = review.gaps if review else []
            # A gap that states a figure goes nowhere. The nudge is a message code writes, so a figure the gap
            # reviewer worked out would reach the investigator in the Orchestrator's own voice.
            gaps = [gap for gap in written if not figures.not_backed(gap, set(), loose=True)]
            dropped, by = len(written) - len(gaps), "model"
            if dropped:
                log.warning("case %s: %d gaps that state a figure were dropped", state["case_id"], dropped)
        gap_passes = state["gap_passes"] + 1
        logged = {
            "by": by,
            "gaps": gaps,
            "asks": [{"tool": gap["ask"]["tool"], **gap["ask"]["args"]} for gap in found],
            "dropped": dropped,
            "at": len(asked),
            "ended_by": _ended_by(state["messages"][-1]),
        }
        update = {"gaps": gaps, "gap_passes": gap_passes, "gap_log": [logged]}
        if _goes_back(gaps, state["rounds"], gap_passes):
            # A reply whose tool call could not be read ended the questions. It is answered before the nudge.
            nudge = HumanMessage("Still unexplained:\n" + "\n".join(f"- {gap}" for gap in gaps))
            update["messages"] = [*_unread(state["messages"][-1]), nudge]
        return update

    def after_gaps(state: CaseState) -> str:
        goes_back = _goes_back(state["gaps"], state["rounds"], state["gap_passes"])
        return "investigate" if goes_back else "fetch_guidelines"

    def fetch_guidelines(state: CaseState) -> dict:
        return {"guidelines": load_guidelines()}

    def draft_report(state: CaseState) -> dict:
        signal = state["signal"]
        # What code could not close in the last gap pass. A gap the model wrote reaches the investigator only.
        passes = state.get("gap_log", [])
        still_unexplained = passes[-1]["gaps"] if passes and passes[-1]["by"] == "code" else []
        brief = prompts.report_brief(
            _own_facts(state),
            prompts.questions(state.get("asked", []), state.get("answers", [])),
            state["guidelines"],
            still_unexplained,
            state.get("check", {}).get("problems", []),
        )
        try:
            reply = report_writer.invoke([SystemMessage(prompts.REPORT), HumanMessage(brief)])
        except OutputParserException as error:  # prose, from a provider that raises on it
            log.warning("case %s: draft %d could not be parsed: %s", state["case_id"], state["drafts"] + 1, error)
            reply = {"raw": None, "parsed": None, "parsing_error": error.__cause__}
        cut_off = _cut_off(reply["raw"])
        draft = None if cut_off else reply["parsed"]
        if draft is None:
            if cut_off:
                error = "It was cut off before it was complete. Write a shorter report."
            else:
                error = _not_a_report(reply["parsing_error"])
            # The parser's own message goes to the log only.
            why = reply["parsing_error"] or error
            log.warning("case %s: draft %d is not a report: %s", state["case_id"], state["drafts"] + 1, why)
            # The redraft is told what did not fit.
            return {"report": None, "draft_error": error.replace("\x00", "")[:1000], "drafts": state["drafts"] + 1}
        report = {
            "case_id": state["case_id"],
            "customer_id": signal["customer_id"],
            **draft.model_dump(mode="json"),
            "engine_limit_cents": signal["engine_limit_cents"],
        }
        return {"report": report, "draft_error": None, "drafts": state["drafts"] + 1}

    def check_report(state: CaseState) -> dict:
        found = _report_problems(state)
        problems = [sentence for _, sentence in found]
        report = state["report"]
        ledger = facts.ledger(state)
        out_of_drafts = state["drafts"] >= MAX_DRAFTS
        if problems:
            result = "gave_up" if out_of_drafts else "refused"
        else:
            result = "passed_low_confidence" if report["confidence"] == "low" else "passed"
        codes = [code for problem_codes, _ in found for code in problem_codes]
        known = {*ledger, *(guideline["id"] for guideline in state["guidelines"])}
        logged = [_logged(state["drafts"], codes, report, result, known)]
        if problems and not out_of_drafts:
            return {"check": {"passed": False, "problems": problems}, "draft_log": logged}

        if problems:
            # Two drafts failed. A person decides from what the specialists found, without the draft's own figures.
            said = [FIGURES_NOT_SHOWN if "figure" in problem_codes else sentence for problem_codes, sentence in found]
            report = {
                "case_id": state["case_id"],
                "customer_id": state["signal"]["customer_id"],
                "likely_cause": "no_clear_cause",
                "summary": "The report could not be completed. " + " ".join(said),
                "evidence": [],
                "ruled_out": [],
                "other_findings": _found(state.get("answers", []), ledger),
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
            "draft_log": logged,
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

    def act(state: CaseState, runtime: Runtime[RunContext]) -> dict:
        # Its own step: a resumed approval step runs again from its first line, this one does not.
        recommendation = state["report"]["recommendation"]
        action = None
        if recommendation == "reduce_limit":
            action = {"type": "reduce_limit", "limit_cents": state["report"]["engine_limit_cents"]}
        elif recommendation == "pause_card":
            action = {"type": "pause_card"}
        if action is not None:
            approval = state["decisions"][-1]
            action |= {
                "case_id": state["case_id"],
                "customer_id": state["signal"]["customer_id"],
                "approved_by": approval["by"],
                # The same if this step runs again, so Card can apply each approval once.
                "approval_id": f"{state['case_id']}:{approval['step']}",
            }
            token = (runtime.context or {}).get("approver_token")
            action["result"] = carry_out(action, token)
        return {"action": action, "status": "done"}

    graph = StateGraph(CaseState, context_schema=RunContext)
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
    return graph.compile(checkpointer=checkpointer, name="credit-review-case")
