import copy
import pickle
from itertools import count

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents import mock
from app.graph import build_graph
from app.graph.build import MAX_ROUNDS
from tests.test_signals import SIGNAL  # c_010: a large cut and a failed repayment

GUIDELINES = [
    {"id": "G-03", "when": "The fall is explained by payroll or tax.", "allows": ["keep_limit"]},
    {"id": "G-07", "when": "A repayment failed.", "allows": ["reduce_limit", "request_documents"]},
    {"id": "G-11", "when": "Cash cannot cover the balance owed.", "allows": ["pause_card", "reduce_limit"]},
    {"id": "G-12", "when": "The limit is very large.", "allows": ["needs_human"]},
]
CONFIG = {"configurable": {"thread_id": "case_test"}}
APPROVE = {"decision": "approve", "by": "u_analyst"}
MANAGER_APPROVES = {"decision": "approve", "by": "u_manager"}
REJECT = {"decision": "reject", "by": "u_analyst"}


class ScriptedModel(GenericFakeChatModel):
    """Plays back the messages it is given, one per call, whatever it is asked."""

    def bind_tools(self, tools, **kwargs):
        return self


def scripted(script, model=ScriptedModel):
    # A message gets an id the first time it is used, and the graph's history replaces a message
    # that has the id of an earlier one. So a message used twice goes out as a new message each time.
    return model(messages=(message.model_copy(update={"id": None}) for message in script))


_ids = count()


def call(name: str, **args) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": f"call_{next(_ids)}"}])


def ask_bank(question: str, context: str = "") -> AIMessage:
    return call("AskCashFlow", question=question, context=context)


def ask_card(question: str, context: str = "") -> AIMessage:
    return call("AskCreditBehavior", question=question, context=context)


DONE = AIMessage(content="DONE")


def gaps(*found: str) -> AIMessage:
    return call("GapReview", gaps=list(found))


REPORT = {
    "likely_cause": "payroll_or_tax",
    "summary": "Payroll and tax explain the fall. A repayment failed although the account held enough cash.",
    "evidence": [
        {
            "claim": "Payroll and a tax payment account for $220,000 of the fall.",
            "evidence_ids": ["txn_c_010_01", "txn_c_010_02"],
        }
    ],
    "ruled_out": [],
    "other_findings": [
        {"claim": "The account held $310,000 on the due date.", "evidence_ids": ["bal_c_010"]}
    ],
    "review_reasons": [
        {
            "reason": "large_cut",
            "finding": "The fall is payroll and tax.",
            "evidence_ids": ["txn_c_010_01", "txn_c_010_02"],
        },
        {
            "reason": "failed_repayment",
            "finding": "The Nov 5 repayment failed. Why is not determined.",
            "evidence_ids": ["rep_c_010_3"],
        },
    ],
    "confidence": "medium",
    "recommendation": "reduce_limit",
    "guideline_ids": ["G-07"],
    "next_action": None,
}


def report(**changes) -> AIMessage:
    return call("ReportDraft", **{**copy.deepcopy(REPORT), **changes})


PAUSE = {"recommendation": "pause_card", "guideline_ids": ["G-11"]}


# A report that rests only on what the bank side returned.
BANK_ONLY = {
    "evidence": [{"claim": "Payroll and tax.", "evidence_ids": ["txn_c_010_01", "txn_c_010_02"]}],
    "other_findings": [],
    "review_reasons": [
        {"reason": "large_cut", "finding": "Payroll and tax.", "evidence_ids": ["txn_c_010_01"]},
        {"reason": "failed_repayment", "finding": "Not determined.", "evidence_ids": []},
    ],
}

# The model's side of a full investigation of c_010. The third question follows from the second answer.
INVESTIGATION = [
    ask_bank("Where did the money go?"),
    ask_card("Did any repayment fail?"),
    ask_bank(
        "What was the paying account balance on the due date?",
        context="A $50,000 repayment due Nov 5 failed.",
    ),
    DONE,
    gaps(),
]


class Run:
    """One case run against a scripted model, with what was sent to Card."""

    def __init__(self, script, *, specialists=None, signal=SIGNAL):
        self.sent = []
        self.tokens = []
        self.card_is_down = False
        self.saver = InMemorySaver()
        self.graph = build_graph(
            model=scripted(script),
            specialists=specialists or {"cash_flow": mock.cash_flow, "credit_behavior": mock.credit_behavior},
            load_guidelines=lambda: GUIDELINES,
            carry_out=self._carry_out,
            checkpointer=self.saver,
        )
        self.result = self.graph.invoke({"case_id": "case_test", "signal": signal}, CONFIG)

    def _carry_out(self, action: dict, token: str | None) -> dict:
        if self.card_is_down:
            raise ConnectionError("the Card Service did not answer")
        self.sent.append(dict(action))
        self.tokens.append(token)
        return {"applied": True}

    @property
    def state(self) -> dict:
        return self.graph.get_state(CONFIG).values

    @property
    def saved(self) -> bytes:
        """Everything the checkpointer holds, to search for what must not be in it."""
        return pickle.dumps((dict(self.saver.storage), dict(self.saver.writes), dict(self.saver.blobs)))

    def decide(self, decision: dict, token: str | None = None) -> dict:
        self.result = self.graph.invoke(Command(resume=decision), CONFIG, context={"approver_token": token})
        return self.result

    @property
    def waiting_at(self) -> tuple:
        return self.graph.get_state(CONFIG).next

    @property
    def shown(self) -> dict:
        """What the paused step put in front of the person."""
        return self.result["__interrupt__"][0].value


def signal_with(**changes) -> dict:
    return {**copy.deepcopy(SIGNAL), **changes}


def test_a_case_runs_from_the_signal_to_the_analyst():
    run = Run([*INVESTIGATION, report()])

    assert run.waiting_at == ("analyst_approval",)
    assert run.result["status"] == "needs_approval"
    assert [(a["agent"], a["question"]) for a in run.result["answers"]] == [
        ("cash_flow", "Where did the money go?"),
        ("credit_behavior", "Did any repayment fail?"),
        ("cash_flow", "What was the paying account balance on the due date?"),
    ]
    assert run.result["check"] == {"passed": True, "problems": []}
    assert run.shown["step"] == "analyst"
    assert run.shown["report"] == {
        "case_id": "case_test",
        "customer_id": "c_010",
        **REPORT,
        "engine_limit_cents": 5800000,
    }
    assert run.sent == []


def test_the_specialist_is_asked_about_the_week_before_the_signal():
    asked = []

    def cash_flow(request):
        asked.append(request)
        return mock.cash_flow(request)

    Run(
        [
            ask_bank("Where did the money go?", context="Cash fell by $230,000."),
            DONE,
            gaps(),
            report(**BANK_ONLY),
        ],
        specialists={"cash_flow": cash_flow, "credit_behavior": mock.credit_behavior},
    )

    assert asked[0].model_dump(mode="json") == {
        "case_id": "case_test",
        "customer_id": "c_010",
        "question": "Where did the money go?",
        "window_start": "2026-11-03",
        "window_end": "2026-11-10",
        "context": "Cash fell by $230,000.",
        "topic": None,
        "account_id": None,
        "record_id": None,
    }


def test_an_approved_reduction_is_sent_to_card_once():
    run = Run([*INVESTIGATION, report()])

    result = run.decide(APPROVE)

    assert run.sent == [
        {
            "type": "reduce_limit",
            "limit_cents": 5800000,
            "case_id": "case_test",
            "customer_id": "c_010",
            "approved_by": "u_analyst",
            "approval_id": "case_test:analyst",
        }
    ]
    assert result["status"] == "done"
    assert result["action"]["result"] == {"applied": True}
    assert run.waiting_at == ()


def test_the_approvers_token_reaches_card_and_is_never_saved():
    run = Run([*INVESTIGATION, report(**PAUSE)])

    run.decide(APPROVE, token="token-of-the-analyst")
    run.decide(MANAGER_APPROVES, token="token-of-the-manager")

    # Card gets the token of the person whose approval let the action through.
    assert run.tokens == ["token-of-the-manager"]
    assert run.sent[0]["approval_id"] == "case_test:manager"
    assert b"case_test" in run.saved  # the search does find what is saved
    assert b"token-of-the" not in run.saved


def test_a_rejected_report_changes_nothing():
    run = Run([*INVESTIGATION, report()])

    result = run.decide(REJECT)

    assert run.sent == []
    assert result["status"] == "rejected"
    assert run.waiting_at == ()


@pytest.mark.parametrize(
    ("recommendation", "guideline"), [("keep_limit", "G-03"), ("request_documents", "G-07")]
)
def test_an_approved_report_with_nothing_to_carry_out_closes_the_case(recommendation, guideline):
    run = Run([*INVESTIGATION, report(recommendation=recommendation, guideline_ids=[guideline])])

    result = run.decide(APPROVE)

    assert run.sent == []
    assert result["status"] == "done"
    assert result["action"] is None


def test_a_failed_call_to_card_leaves_the_case_approved_and_open():
    run = Run([*INVESTIGATION, report()])
    run.card_is_down = True

    with pytest.raises(ConnectionError):
        run.decide(APPROVE, token="token-of-the-analyst")

    assert run.waiting_at == ("act",)
    assert b"token-of-the" not in run.saved  # not even with the error that was saved
    assert run.state["status"] != "done"
    assert run.state.get("action") is None

    run.card_is_down = False
    result = run.graph.invoke(None, CONFIG)

    # The approval is not asked for again, and the action goes out under the same approval id.
    assert [action["approval_id"] for action in run.sent] == ["case_test:analyst"]
    assert [d["by"] for d in result["decisions"]] == ["u_analyst"]
    assert result["status"] == "done"


def test_a_card_pause_also_needs_a_manager():
    run = Run([*INVESTIGATION, report(**PAUSE)])

    result = run.decide(APPROVE)

    assert run.waiting_at == ("manager_approval",)
    assert result["status"] == "needs_manager"
    assert run.shown["step"] == "manager"
    assert run.sent == []

    result = run.decide(MANAGER_APPROVES)

    assert [action["type"] for action in run.sent] == ["pause_card"]
    assert run.sent[0]["approved_by"] == "u_manager"
    assert result["status"] == "done"
    assert [d["step"] for d in result["decisions"]] == ["analyst", "manager"]


def test_a_manager_can_stop_what_the_analyst_approved():
    run = Run([*INVESTIGATION, report(**PAUSE)])
    run.decide(APPROVE)

    result = run.decide({"decision": "reject", "by": "u_manager"})

    assert run.sent == []
    assert result["status"] == "rejected"


@pytest.mark.parametrize(
    ("engine_limit_cents", "waits_for"),
    [
        pytest.param(5000000, "act", id="a cut of exactly half goes ahead"),
        pytest.param(4999999, "manager", id="a cut over half needs a manager"),
    ],
)
def test_a_cut_over_half_the_limit_needs_a_manager(engine_limit_cents, waits_for):
    run = Run([*INVESTIGATION, report()], signal=signal_with(engine_limit_cents=engine_limit_cents))

    run.decide(APPROVE)

    if waits_for == "manager":
        assert run.waiting_at == ("manager_approval",)
        assert run.sent == []
    else:
        assert run.waiting_at == ()
        assert len(run.sent) == 1


@pytest.mark.parametrize(
    "not_a_decision",
    [
        pytest.param({"decision": "fine by me", "by": "u_analyst"}, id="neither approve nor reject"),
        pytest.param("approve", id="not an object"),
        pytest.param({"decision": "approve"}, id="nobody named"),
        pytest.param({"decision": "approve", "by": ""}, id="an empty name"),
        pytest.param({"decision": "approve", "by": 7}, id="a name that is not text"),
    ],
)
def test_an_answer_that_is_not_a_decision_keeps_the_case_waiting_for_a_real_one(not_a_decision):
    run = Run([*INVESTIGATION, report()])

    run.decide(not_a_decision)

    assert run.waiting_at == ("analyst_approval",)
    assert "A decision must look like" in run.shown["problem"]
    assert run.shown["report"]["recommendation"] == "reduce_limit"
    assert run.sent == []

    # The bad answer must not block the case: a real decision still goes through.
    result = run.decide(APPROVE)

    assert len(run.sent) == 1
    assert result["decisions"] == [{"step": "analyst", "decision": "approve", "by": "u_analyst"}]
    assert result["status"] == "done"


def test_a_bad_answer_at_the_manager_step_does_not_block_it_either():
    run = Run([*INVESTIGATION, report(**PAUSE)])
    run.decide(APPROVE)

    run.decide({"decision": "ok"})

    assert run.waiting_at == ("manager_approval",)
    assert run.sent == []

    result = run.decide(MANAGER_APPROVES)

    assert [d["step"] for d in result["decisions"]] == ["analyst", "manager"]
    assert len(run.sent) == 1


def test_a_decision_cannot_say_which_step_it_is_for():
    run = Run([*INVESTIGATION, report(**PAUSE)])

    result = run.decide({**APPROVE, "step": "manager"})

    assert result["decisions"] == [{"step": "analyst", "decision": "approve", "by": "u_analyst"}]
    assert run.waiting_at == ("manager_approval",)


def test_a_gap_sends_the_case_back_for_another_question():
    run = Run(
        [
            ask_bank("Where did the money go?"),
            DONE,
            gaps("The card side was never asked about the failed repayment."),
            ask_card("Did any repayment fail?"),
            DONE,
            gaps(),
            report(other_findings=[]),
        ]
    )

    assert [a["agent"] for a in run.result["answers"]] == ["cash_flow", "credit_behavior"]
    nudge = [m for m in run.result["messages"] if isinstance(m, HumanMessage)][-1]
    assert "The card side was never asked" in nudge.content
    assert run.waiting_at == ("analyst_approval",)


def test_gaps_cannot_keep_a_case_going_forever():
    still = gaps("Something is still unexplained.")

    run = Run([ask_bank("Where did the money go?"), DONE, still, DONE, still, DONE, still, report(**BANK_ONLY)])

    assert run.result["gap_passes"] == 3
    history = [m.content for m in run.result["messages"]]
    assert history.count("DONE") == 3
    assert sum(text.startswith("Still unexplained") for text in history) == 2
    assert run.waiting_at == ("analyst_approval",)


def test_the_investigation_stops_at_the_round_limit():
    questions = [ask_bank(f"Question {n}?") for n in range(MAX_ROUNDS)]

    # After the last allowed question the model is not asked for another: the next message is the gap review.
    run = Run([*questions, gaps("Still unexplained."), report(**BANK_ONLY)])

    assert len(run.result["answers"]) == MAX_ROUNDS
    assert run.result["rounds"] == MAX_ROUNDS
    assert run.waiting_at == ("analyst_approval",)


def test_only_the_first_of_two_questions_in_one_turn_is_asked():
    both = AIMessage(
        content="",
        tool_calls=[
            {"name": "AskCashFlow", "args": {"question": "Where did the money go?"}, "id": "first"},
            {"name": "AskCreditBehavior", "args": {"question": "Did any repayment fail?"}, "id": "second"},
        ],
    )

    run = Run([both, ask_card("Did any repayment fail?"), DONE, gaps(), report(other_findings=[])])

    assert [a["agent"] for a in run.result["answers"]] == ["cash_flow", "credit_behavior"]
    not_asked = next(m for m in run.result["messages"] if isinstance(m, ToolMessage) and m.tool_call_id == "second")
    assert not_asked.content == "Not asked. Ask one question per turn."


def test_a_tool_that_does_not_exist_counts_as_a_question_with_no_answer():
    run = Run(
        [
            call("AskTheAuditor", question="Is this fraud?"),
            ask_bank("Where did the money go?"),
            DONE,
            gaps(),
            report(**BANK_ONLY),
        ]
    )

    first, second = run.result["answers"]
    assert first == {"agent": None, "question": "Is this fraud?", "error": "No usable answer from AskTheAuditor."}
    assert "response" in second
    assert run.result["rounds"] == 2


def broken(request):
    raise TimeoutError("the Bank Service did not answer")


def cites_nothing_real(request):
    return {
        "agent": "cash_flow",
        "findings": [{"claim": "Cash fell.", "confidence": "high", "evidence_ids": ["txn_made_up"]}],
        "evidence": [],
        "could_not_determine": [],
    }


def edited_after_it_was_built(request):
    answer = mock.cash_flow(request)
    answer.findings[0].evidence_ids.clear()  # a finding with nothing behind it
    return answer


@pytest.mark.parametrize(
    "cash_flow",
    [
        pytest.param(broken, id="the specialist fails"),
        pytest.param(cites_nothing_real, id="the answer cites evidence it does not carry"),
        pytest.param(edited_after_it_was_built, id="the answer was changed after it was built"),
        pytest.param(mock.credit_behavior, id="the other specialist answers"),
    ],
)
def test_a_specialist_with_no_usable_answer_does_not_stop_the_case(cash_flow):
    run = Run(
        [
            ask_bank("Where did the money go?"),
            ask_card("Did any repayment fail?"),
            DONE,
            gaps(),
            report(
                likely_cause="insufficient_data",
                evidence=[],
                other_findings=[],
                review_reasons=[
                    {"reason": "large_cut", "finding": "The bank side gave no answer.", "evidence_ids": []},
                    {"reason": "failed_repayment", "finding": "The Nov 5 repayment failed.", "evidence_ids": ["rep_c_010_3"]},
                ],
                recommendation="needs_human",
            ),
        ],
        specialists={"cash_flow": cash_flow, "credit_behavior": mock.credit_behavior},
    )

    first, second = run.result["answers"]
    assert first == {
        "agent": "cash_flow",
        "question": "Where did the money go?",
        "error": "No usable answer from cash_flow.",
    }
    assert "response" in second
    assert run.waiting_at == ("analyst_approval",)


@pytest.mark.parametrize(
    ("bad_draft", "problem"),
    [
        pytest.param(
            report(evidence=[{"claim": "A wire left.", "evidence_ids": ["txn_0000"]}]),
            "It cites evidence no specialist returned: txn_0000.",
            id="evidence nobody returned",
        ),
        pytest.param(
            report(review_reasons=REPORT["review_reasons"][:1]),
            "It does not address the review reason: failed_repayment.",
            id="a review reason left out",
        ),
        pytest.param(report(guideline_ids=[]), "It cites no guideline.", id="no guideline"),
        pytest.param(
            report(guideline_ids=["G-99"]),
            "It cites a guideline that does not exist: G-99.",
            id="a guideline that does not exist",
        ),
        pytest.param(
            report(recommendation="needs_human", guideline_ids=["G-99"]),
            "It cites a guideline that does not exist: G-99.",
            id="a guideline that does not exist, with nothing recommended",
        ),
        pytest.param(
            report(guideline_ids=["G-03"]),
            "No guideline it cites allows reduce_limit.",
            id="an action its guideline does not allow",
        ),
        pytest.param(
            report(evidence=[]),
            "It recommends reduce_limit without citing evidence.",
            id="an action with no evidence",
        ),
        pytest.param(
            report(guideline_ids=["G-12", "G-07"]),
            "It cites G-12, under which a person decides, so it may only recommend needs_human.",
            id="an action next to a guideline that leaves the decision to a person",
        ),
        pytest.param(
            call("ReportDraft", likely_cause="aliens"),
            "The output did not fit the report shape.",
            id="not a report",
        ),
        pytest.param(
            call("ReportDraft", likely_cause="aliens"),
            "likely_cause",  # the redraft is told which part did not fit
            id="not a report, and why",
        ),
        pytest.param(
            AIMessage(content="The customer looks fine to me."),
            "The output did not fit the report shape. No report was returned.",
            id="prose with no report",
        ),
        pytest.param(
            report().model_copy(update={"response_metadata": {"finish_reason": "length"}}),
            "It was cut off before it was complete. Write a shorter report.",
            id="cut off, as another provider reports it",
        ),
    ],
)
def test_a_report_that_fails_the_check_is_redrafted_once(bad_draft, problem):
    seen_by_the_model = []

    class Recording(ScriptedModel):
        def _generate(self, messages, *args, **kwargs):
            seen_by_the_model.append(messages)
            return super()._generate(messages, *args, **kwargs)

    sent = []
    graph = build_graph(
        model=scripted([*INVESTIGATION, bad_draft, report()], Recording),
        specialists={"cash_flow": mock.cash_flow, "credit_behavior": mock.credit_behavior},
        load_guidelines=lambda: GUIDELINES,
        carry_out=lambda action, token: sent.append(action),
        checkpointer=InMemorySaver(),
    )
    result = graph.invoke({"case_id": "case_test", "signal": SIGNAL}, CONFIG)

    assert result["drafts"] == 2
    assert result["check"] == {"passed": True, "problems": []}
    assert result["__interrupt__"][0].value["report"]["recommendation"] == "reduce_limit"
    # The second draft was told what was wrong with the first.
    assert problem in seen_by_the_model[-1][-1].content


def test_two_failed_drafts_hand_the_case_to_a_person():
    bad = report(evidence=[{"claim": "A wire left.", "evidence_ids": ["txn_0000"]}])

    run = Run([*INVESTIGATION, bad, bad])

    assert run.waiting_at == ("analyst_approval",)
    assert run.result["check"]["passed"] is False
    assert run.shown["report"]["recommendation"] == "needs_human"
    assert run.shown["report"]["confidence"] == "low"
    assert "txn_0000" in run.shown["report"]["summary"]

    result = run.decide(APPROVE)

    assert run.sent == []
    assert result["status"] == "done"


def cut_off(message: AIMessage) -> AIMessage:
    """The same answer, but the model ran out of room while writing it."""
    return message.model_copy(update={"response_metadata": {"stop_reason": "max_tokens"}})


def test_a_report_that_was_cut_off_is_not_taken_even_though_it_reads_as_complete():
    # What arrived parses as a full report. It may be missing whatever came after the cut.
    run = Run([*INVESTIGATION, cut_off(report(other_findings=[])), report()])

    assert run.result["drafts"] == 2
    assert run.result["check"] == {"passed": True, "problems": []}
    assert run.shown["report"]["other_findings"] == REPORT["other_findings"]


def test_a_gap_review_that_was_cut_off_is_not_taken_as_nothing_missing_or_as_a_full_list():
    partial = cut_off(gaps("The card side was never asked."))

    run = Run([ask_bank("Where did the money go?"), DONE, partial, report(**BANK_ONLY)])

    # Not sent back on half a list, and the case still reaches a person.
    assert run.result["gaps"] == []
    assert len(run.result["answers"]) == 1
    assert run.waiting_at == ("analyst_approval",)


def test_two_outputs_that_are_not_reports_hand_the_case_to_a_person():
    not_a_report = call("ReportDraft", likely_cause="aliens")

    run = Run([*INVESTIGATION, not_a_report, not_a_report])

    assert run.waiting_at == ("analyst_approval",)
    assert run.shown["report"]["recommendation"] == "needs_human"
    assert "did not fit the report shape" in run.shown["report"]["summary"]


def test_a_reduction_that_would_raise_the_limit_never_reaches_card():
    # The engine wants a higher limit here, so setting the engine's limit is not a reduction.
    raise_it = signal_with(current_limit_cents=10000000, engine_limit_cents=20000000)

    run = Run([*INVESTIGATION, report(), report()], signal=raise_it)

    assert "the engine's limit is not below the current limit" in run.result["check"]["problems"][0]
    assert run.shown["report"]["recommendation"] == "needs_human"

    run.decide(APPROVE)

    assert run.sent == []


def test_a_report_that_recommends_nothing_needs_no_guideline():
    nothing_fits = report(
        likely_cause="no_clear_cause", recommendation="needs_human", guideline_ids=[], evidence=[]
    )

    run = Run([*INVESTIGATION, nothing_fits])

    assert run.result["check"] == {"passed": True, "problems": []}
    assert run.result["drafts"] == 1


def test_a_low_confidence_report_becomes_needs_human_whatever_the_model_recommended():
    run = Run([*INVESTIGATION, report(confidence="low", **PAUSE)])

    assert run.shown["report"]["recommendation"] == "needs_human"

    run.decide(APPROVE)

    assert run.sent == []
    assert run.waiting_at == ()
