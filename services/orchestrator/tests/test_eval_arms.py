"""The evaluation's arms: the runner, the grade of a run's question path, and two arms set side by side (8.4 to 8.6).

Nothing here calls a model or starts a stack. The runner talks to the service on a scripted model, and the
commands it would run in the Orchestrator's container run in this checkout instead.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from sqlalchemy import delete, select, update

SERVICE = Path(__file__).resolve().parents[1]
EVAL = SERVICE.parents[1] / "eval"
sys.path.insert(0, str(EVAL))

import compare as pairing  # noqa: E402
import grade as grading  # noqa: E402
import run as golden_run  # noqa: E402

from app import history, policy  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.db.models import Case, Review  # noqa: E402
from tests.conftest import bearer  # noqa: E402
from tests.test_cases import seed_entry, start  # noqa: E402, F401
from tests.test_graph import BANK_ONLY, INVESTIGATION, ask_bank, ask_card, gaps, report  # noqa: E402
from tests.test_signals import SIGNAL  # noqa: E402  (c_010)

GOLDEN = yaml.safe_load((EVAL / "golden_set.yaml").read_text())
CASES = {case["id"]: case for case in GOLDEN["customers"]}


@pytest.fixture(autouse=True)
def no_reviews_left():
    # The runner seeds reviews. One left behind would be an earlier review for a later test's case (spec 9).
    yield
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def tokens() -> dict:
    def raw(**who) -> str:
        return bearer(**who)["Authorization"].removeprefix("Bearer ")

    return {
        "service": raw(sub="signal-generator", role="service", customers=None),
        "analyst": raw(),  # u_analyst, who has c_010
        "other_analyst": raw(sub="u_other", customers=("c_none",)),
    }


# What the scripted report() says about c_010: a cut under G-07, the fixture's guideline that allows one.
C010 = {
    "id": "c_010",
    "category": "business",
    "split": "dev",
    "causes": ["payroll_or_tax"],
    "recommendations": ["reduce_limit"],
    "must_cite": ["rep_c_010_3"],
    "guidelines": ["G-07"],
}
FOR_A_PERSON = {**C010, "recommendations": ["needs_human"]}


# Runs written by hand, in the shapes of the path page (spec 1.3, 8.5).

REPORT = {
    "likely_cause": "payroll_or_tax",
    "recommendation": "reduce_limit",
    "confidence": "medium",
    "summary": "Payroll and tax explain the fall.",
    "evidence": [{"claim": "Payroll.", "evidence_ids": ["txn_c_010_01"]}],
    "ruled_out": [],
    "other_findings": [],
    "review_reasons": [{"reason": "failed_repayment", "finding": "It failed.", "evidence_ids": ["rep_c_010_3"]}],
    "guideline_ids": ["G-07"],
}
GAVE_UP = {
    **REPORT,
    "likely_cause": "no_clear_cause",
    "recommendation": "needs_human",
    "confidence": "low",
    "summary": "The report could not be completed. It cites no guideline.",
    "evidence": [],
    "review_reasons": [],
    "guideline_ids": [],
}


def said(**changes) -> dict:
    return {**REPORT, **changes}


def asked(tool: str, topic: str, *returned: str, outcome: str = "answered", **focus) -> dict:
    agent = {"AskCashFlow": "cash_flow", "AskCreditBehavior": "credit_behavior"}[tool]
    return {"tool": tool, "agent": agent, "topic": topic, **focus, "outcome": outcome, "returned": list(returned)}


def drafted(draft: dict = REPORT, result: str = "passed", codes: tuple = (), number: int = 1) -> dict:
    """One entry of the draft log: the labels and cited ids of a checked draft."""
    return {
        "draft": number,
        "codes": list(codes),
        "result": result,
        "recommendation": draft["recommendation"],
        "likely_cause": draft["likely_cause"],
        "confidence": draft["confidence"],
        "guideline_ids": draft["guideline_ids"],
        "cited": sorted(grading.cited(draft)),
    }


def a_path(*questions: dict, drafts: list | None = None, gaps: list = (), **more) -> dict:
    return {
        "case_id": "case_x",
        "flow": "agent",
        "policy_version": "2026-11-16",
        "rounds": len(questions),
        "refusals": 0,
        "out_of_rounds": False,
        "questions": list(questions),
        "searches": [],
        "gaps": list(gaps),
        "drafts": [drafted()] if drafts is None else drafts,
        "history": [],
        "handed_over_by": None,
        **more,
    }


def recorded(case: dict, said: dict = REPORT, path: dict | None = None, rep: int = 1, arm: str = "agent") -> dict:
    """A run record as eval/run.py writes it."""
    path = a_path() if path is None else path
    return {
        "arm": arm,
        "case": case["id"],
        "rep": rep,
        "report": said,
        "grades": grading.grade(case, said),
        "path": path,
        "path_grades": grading.grade_path(case, said, path),
    }


def section(text: str, title: str) -> str:
    """One section of a summary, from its heading to the next."""
    return text.split(f"\n## {title}\n", 1)[1].split("\n## ", 1)[0]


# The path grade.


def test_a_run_is_graded_on_its_path_apart_from_its_answer(start):
    service = start([*INVESTIGATION, report()])
    case = {**C010, "must_reach": ["agg_c_010_inflows"]}

    record = golden_run.one_run(service.client, tokens(), case, SIGNAL, 1, timeout=5)

    assert record["grades"] == {"recommendation": True, "cause": True, "hand_over": True, "cites": True}
    assert [(q["tool"], q["topic"], q["outcome"]) for q in record["path"]["questions"]] == [
        ("AskCashFlow", "transactions", "answered"),
        ("AskCreditBehavior", "repayments", "answered"),
        ("AskCashFlow", "income", "answered"),
    ]
    assert record["path_grades"] == {
        "reached": True,
        "guideline": True,
        "questions": 3,
        "refused": 0,
        "searches": 0,
        "first_draft": record["grades"],
        "first_draft_passed": True,
        "nudged": [],
        "out_of_rounds": False,
        "gave_up_codes": None,
        "handed_over_by": None,
        "why": "right",
    }
    # The same answer for a case that turns on a record no question returned: still right, but not tested.
    untested = {**C010, "must_reach": ["agg_c_010_prior_year"]}
    assert grading.grade(untested, record["report"]) == record["grades"]
    assert grading.grade_path(untested, record["report"], record["path"])["why"] == "path"


def test_a_run_that_was_never_given_what_it_had_to_cite_is_a_path_failure_and_not_an_evidence_failure():
    # c_010 must cite the failed repayment. Left out, what a run must reach is that: no card question, no record.
    case = {key: value for key, value in C010.items() if key != "guidelines"}
    no_card = a_path(asked("AskCashFlow", "transactions", "bal_c_010", "txn_c_010_01"), asked("AskCashFlow", "income"))
    without = said(review_reasons=[])

    graded = grading.grade_path(case, without, no_card)

    assert grading.grade(case, without)["cites"] is False
    assert graded["reached"] is False and graded["why"] == "path"
    # Given the record and not citing it is the evidence failure.
    with_card = a_path(*no_card["questions"], asked("AskCreditBehavior", "repayments", "rep_c_010_3"))
    assert grading.grade_path(case, without, with_card)["why"] == "evidence"
    # The records code gives at open are no question's to reach.
    assert grading.grade_path({**case, "must_cite": ["sig_repayments", "hist_c_010_summary"]}, without, no_card)["reached"] is None


def test_why_names_the_first_stage_that_failed_in_the_order_of_the_spec():
    # c_010 must reach and cite the failed repayment. This path returns it.
    reached = a_path(asked("AskCreditBehavior", "repayments", "rep_c_010_3"))

    def why(**changes) -> str:
        return grading.grade_path(C010, said(**changes), reached)["why"]

    assert why() == "right"
    assert why(likely_cause="revenue_loss") == "cause"
    assert why(guideline_ids=["G-04"]) == "guideline"  # a guideline the case does not list
    assert why(recommendation="keep_limit") == "action"
    # Path, evidence, cause, guideline, action: the first that failed is named.
    assert why(likely_cause="revenue_loss", review_reasons=[]) == "evidence"
    assert why(likely_cause="revenue_loss", guideline_ids=["G-04"], recommendation="keep_limit") == "cause"
    assert why(guideline_ids=["G-04"], recommendation="keep_limit") == "guideline"


def test_a_give_up_is_counted_apart_and_says_which_rule_refused_it(start):
    # Two drafts that act under no guideline. The service then hands the case over with a report of its own.
    service = start([*INVESTIGATION, report(guideline_ids=[]), report(guideline_ids=[])])

    record = golden_run.one_run(service.client, tokens(), C010, SIGNAL, 1, timeout=5)

    assert grading.gave_up(record["report"])
    assert [draft["result"] for draft in record["path"]["drafts"]] == ["refused", "gave_up"]
    graded = record["path_grades"]
    assert graded["gave_up_codes"] == ["guideline_missing"]
    assert graded["why"] == "gave up: guideline_missing"
    assert graded["handed_over_by"] == "gave_up" and graded["guideline"] is None
    # The codes are the last draft's, not the first's.
    two_rules = [drafted(result="refused", codes=("cause",)), drafted(GAVE_UP, "gave_up", ("guideline_missing",), number=2)]
    other_first = grading.grade_path(C010, GAVE_UP, a_path(drafts=two_rules))
    assert other_first["gave_up_codes"] == ["guideline_missing"] and other_first["why"] == "gave up: guideline_missing"
    # Counted by the rule that refused the last draft, beside a hand-over refused because nothing was settled.
    c_314 = {**FOR_A_PERSON, "id": "c_314", "split": "held_out"}
    # One code can come twice from one draft, a sentence for each guideline: it is still one run.
    last = drafted(GAVE_UP, "gave_up", ("not_settled", "cause", "not_settled"))
    unsettled = recorded(c_314, GAVE_UP, a_path(drafts=[last]))
    text = grading.summary({"customers": [C010, c_314]}, [record, unsettled], [], 1, {"run_id": "r1"})

    given_up = section(text, "Reports the service could not complete")
    assert "2 runs ended with a report the service gave up on." in given_up
    assert "| guideline_missing | 1 | c_010 |" in given_up
    assert "| not_settled | 1 | c_314 |" in given_up and "| cause | 1 | c_314 |" in given_up
    assert "| c_314 | 0 | 0 | NO | not reached | gave up: not_settled, cause |" in section(text, "The questions asked")


def test_a_right_answer_after_a_refused_draft_is_counted_apart_from_one_right_at_once(start):
    # The first run is right at once. The second keeps the limit under no guideline, is refused, and then cuts it.
    service = start([*INVESTIGATION, report(), *INVESTIGATION, report(recommendation="keep_limit", guideline_ids=[]), report()])
    at_once = golden_run.one_run(service.client, tokens(), C010, {**SIGNAL, "signal_id": "sig_1"}, 1, timeout=5)
    after = golden_run.one_run(service.client, tokens(), C010, {**SIGNAL, "signal_id": "sig_2"}, 2, timeout=5)

    assert at_once["grades"]["recommendation"] and after["grades"]["recommendation"]
    assert at_once["path_grades"]["first_draft_passed"] is True
    assert after["path_grades"]["first_draft_passed"] is False
    assert after["path_grades"]["first_draft"]["recommendation"] is False

    text = grading.summary({"customers": [C010]}, [at_once, after], [], 2, {"run_id": "r1"})

    # Right in both runs in the final report, but not in both on the first draft.
    first = section(text, "Right on the first draft")
    assert "| All cases | 0 of 1 (0%, 95% interval 0% to 79%); final report 1 of 1 (100%," in first
    assert "- Right on the recommendation in the final report, with a first draft the check passed: 1 runs (c_010 run 1)." in first
    refused = "- Right on the recommendation in the final report, with a first draft the check refused"
    assert f"{refused} (on any rule, so that draft may have had the recommendation right already): 1 runs (c_010 run 2)." in first
    # The tables that were there before say the case was right.
    assert "| c_010 | business | dev | reduce_limit | reduce_limit, reduce_limit |" in text

    # Refused for a figure, the first draft already cut the limit: the check corrected the figure, not the action.
    figure_first = recorded(C010, path=a_path(drafts=[drafted(result="refused", codes=("figure",)), drafted(number=2)]))
    text = grading.summary({"customers": [C010]}, [figure_first], [], 1, {"run_id": "r1"})
    first = section(text, "Right on the first draft")
    assert "| All cases | 1 of 1 (100%," in first
    assert f"{refused} (on any rule, so that draft may have had the recommendation right already): 1 runs (c_010 run 1)." in first


def test_the_first_checked_draft_is_graded_as_the_service_would_have_issued_it(start):
    # A draft that cuts with confidence low passes the check, and the analyst is shown a hand-over.
    service = start([*INVESTIGATION, report(confidence="low")])

    record = golden_run.one_run(service.client, tokens(), C010, SIGNAL, 1, timeout=5)

    first = record["path"]["drafts"][0]
    assert (first["recommendation"], first["confidence"], first["result"]) == ("reduce_limit", "low", "passed_low_confidence")
    assert record["path_grades"]["first_draft_passed"] is True
    assert record["path_grades"]["first_draft"]["recommendation"] is False
    assert record["path_grades"]["first_draft"]["hand_over"] is False
    assert grading.grade_path(FOR_A_PERSON, record["report"], record["path"])["first_draft"]["recommendation"] is True

    # Its cause and cited ids are the draft log's, not the final report's.
    unexplained = said(likely_cause="unexplained_outflow", review_reasons=[])
    corrected = recorded(C010, path=a_path(drafts=[drafted(unexplained, "refused", ("cause",)), drafted(number=2)]))
    assert corrected["grades"]["cause"] is True and corrected["grades"]["cites"] is True
    assert corrected["path_grades"]["first_draft"]["cause"] is False and corrected["path_grades"]["first_draft"]["cites"] is False
    assert grading.at_first_draft(corrected)["grades"]["recommendation"] is True

    # A first draft that was not a report has no grade of its own, and counts as wrong on every check the case has.
    not_a_report = {"draft": 1, "codes": ["not_a_report"], "result": "refused", "recommendation": None,
                    "likely_cause": None, "confidence": None, "guideline_ids": [], "cited": []}  # fmt: skip
    run = recorded(C010, path=a_path(drafts=[not_a_report, drafted(number=2)]))
    assert run["path_grades"]["first_draft"] is None and run["path_grades"]["first_draft_passed"] is False
    assert grading.at_first_draft(run) == {"grades": dict.fromkeys(grading.CHECKS, False)}
    no_cites = {key: value for key, value in C010.items() if key != "must_cite"}
    assert grading.at_first_draft(recorded(no_cites, path=run["path"]))["grades"]["cites"] is None
    # With no draft logged there is nothing to grade the first draft on.
    assert grading.at_first_draft(recorded(C010, path=a_path(drafts=[]))) == {}


C009 = CASES["c_009"]  # must reach the payment with the memo, and its payee's history
PAYMENT = asked("AskCashFlow", "transactions", "txn_c_009_01", "agg_c_009_payments")
INCOME = asked("AskCashFlow", "income", "agg_c_009_inflows")
PAYEE = asked("AskCashFlow", "payee_history", "agg_c_009_payee_history", record_id="txn_c_009_01")
KEPT = said(likely_cause="planned_vendor_payment", recommendation="keep_limit", guideline_ids=["G-02"])


def code_gap(at: int, **ask) -> dict:
    """A gap pass that code found, as look_for_gaps logs it."""
    return {"by": "code", "gaps": ["A guideline is partly supported."], "asks": [ask], "dropped": 0, "at": at,
            "ended_by": "done"}  # fmt: skip


def test_a_record_reached_only_after_a_code_nudge_is_marked_as_nudged_and_one_asked_unprompted_is_not():
    named = code_gap(2, tool="AskCashFlow", topic="payee_history", record_id="txn_c_009_01")

    nudged = grading.grade_path(C009, KEPT, a_path(PAYMENT, INCOME, PAYEE, gaps=[named]))
    unprompted = grading.grade_path(C009, KEPT, a_path(PAYMENT, PAYEE, INCOME, gaps=[{**named, "at": 3}]))

    # The payment came back before any gap pass. The payee's history only after the gap named it.
    assert nudged["reached"] is True and nudged["nudged"] == ["agg_c_009_payee_history"]
    assert unprompted["reached"] is True and unprompted["nudged"] == []
    # A gap that named another record, or no topic of this question, did not lead to it.
    other_record = code_gap(2, tool="AskCashFlow", topic="payee_history", record_id="txn_c_009_02")
    other_topic = code_gap(2, tool="AskCashFlow", topic="reconciliation")
    for gap in (other_record, other_topic, {**named, "at": None}, {key: v for key, v in named.items() if key != "at"}):
        assert grading.grade_path(C009, KEPT, a_path(PAYMENT, INCOME, PAYEE, gaps=[gap]))["nudged"] == []
    # A gap that named the topic alone covers a question about any record of it.
    topic_only = code_gap(2, tool="AskCashFlow", topic="payee_history")
    assert grading.grade_path(C009, KEPT, a_path(PAYMENT, INCOME, PAYEE, gaps=[topic_only]))["nudged"] == [
        "agg_c_009_payee_history"
    ]
    # Asked again after a nudge, a record first returned before it is still unprompted.
    again = a_path(PAYMENT, INCOME, PAYEE, PAYEE, gaps=[{**named, "at": 3}])
    assert grading.grade_path(C009, KEPT, again)["nudged"] == []

    # The summary says so run by run, beside the calls refused on the way.
    refused = {"tool": "AskTheMoon", "agent": None, "outcome": "refused", "returned": []}
    runs = [
        recorded(C009, KEPT, a_path(PAYMENT, INCOME, PAYEE, gaps=[named])),
        recorded(C009, KEPT, a_path(PAYMENT, PAYEE, refused, INCOME, gaps=[{**named, "at": 3}]), rep=2),
    ]
    text = grading.summary({"customers": [C009]}, runs, [], 2, {"run_id": "r1"})
    assert "| c_009 | 3, 3 | 0, 1 | yes | yes, no | right; right |" in section(text, "The questions asked")


def test_a_run_that_ended_at_the_round_limit_is_marked(start):
    questions = [ask_bank(f"Question {n}?") for n in range(5)] + [ask_card("Did any repayment fail?")]
    service = start([*questions, gaps(), report(**BANK_ONLY)])

    record = golden_run.one_run(service.client, tokens(), C010, SIGNAL, 1, timeout=5)

    assert record["path"]["out_of_rounds"] is True
    assert record["path_grades"]["out_of_rounds"] is True and record["path_grades"]["questions"] == 6
    text = grading.summary({"customers": [C010]}, [record], [], 1, {"run_id": "r1"})
    assert "| c_010 | 6 at the round limit | 0 |" in section(text, "The questions asked")
    table = pairing.paired([{"arm": "agent", **record}], [recorded(C010, arm="fixed")], {"c_010": "R"}, ("a", "b"))
    assert "| A: agent | 1 | 6.0 | 0 | 1 | 0 |" in table
    assert "| c_010 | R | 6 at the round limit | 0 | 0 | 0 |" in table


def test_a_hand_over_by_low_confidence_where_an_action_was_expected_is_counted_apart(start):
    service = start([*INVESTIGATION, report(confidence="low")])
    record = golden_run.one_run(service.client, tokens(), C010, SIGNAL, 1, timeout=5)
    # The same run, on a case whose accepted answer is a hand-over: not this shape.
    c_012 = {**FOR_A_PERSON, "id": "c_012", "split": "held_out"}
    for_a_person = {
        **record,
        "case": "c_012",
        "grades": grading.grade(c_012, record["report"]),
        "path_grades": grading.grade_path(c_012, record["report"], record["path"]),
    }

    assert record["report"]["recommendation"] == "needs_human"
    assert record["path_grades"]["handed_over_by"] == "low_confidence"
    assert record["path_grades"]["why"] == "action"

    text = grading.summary({"customers": [C010, c_012]}, [record, for_a_person], [], 1, {"run_id": "r1"})

    assert section(text, "Handed over by lowering confidence").endswith("\n1 runs (c_010 run 1).\n")


def test_a_held_out_case_a_dev_case_gives_away_is_also_reported_apart():
    c_110 = {**C010, "id": "c_110", "split": "held_out", "dev_lookalike": "c_010"}
    c_x = {**C010, "id": "c_x", "split": "held_out"}
    refused_first = a_path(drafts=[drafted(said(recommendation="keep_limit"), "refused", ("guideline_missing",)), drafted(number=2)])
    runs = [recorded(C010), recorded(c_110), recorded(c_x, path=refused_first)]

    text = grading.summary({"customers": [C010, c_110, c_x]}, runs, [], 1, {"run_id": "r1"})

    rows = {line.split(" | ")[0]: line for line in section(text, "Right on the first draft").splitlines() if line.startswith("| ")}
    assert rows["| All cases"].startswith("| All cases | 2 of 3 (67%,")
    assert rows["| Held out only"].startswith("| Held out only | 1 of 2 (50%,")
    # c_110 shares c_010's records and accepted answer: tuning on c_010 is tuning on it.
    without = rows["| Held out, without the look-alikes of a dev case"]
    assert without.startswith("| Held out, without the look-alikes of a dev case | 0 of 1 (0%,")
    assert "; final report 1 of 1 (100%," in without
    # The first table keeps its two rows.
    assert "| Held out only | 2 of 2 (100%," in text.split("## Case by case")[0]


def test_the_listed_guidelines_are_a_joint_set_and_any_cited_part_of_it_is_right():
    # c_111 rests on G-08a with G-01 beside it; c_012 on G-10b or G-11 (spec 0.3, item 13).
    c_111 = CASES["c_111"]
    assert c_111["guidelines"] == ["G-08a", "G-01"] and CASES["c_012"]["guidelines"] == ["G-10b", "G-11"]

    def on(guideline_ids: list, case: dict = c_111) -> bool | None:
        return grading.grade_path(case, said(guideline_ids=guideline_ids), a_path())["guideline"]

    assert on(["G-08a"]) is True and on(["G-01", "G-08a"]) is True and on(["G-01"]) is True
    assert on(["G-08a", "G-04"]) is False  # one guideline the case does not list
    assert on([]) is False
    assert on(["G-10b"], CASES["c_012"]) is True and on(["G-10b", "G-11"], CASES["c_012"]) is True
    assert on(["G-01"], C010 | {"guidelines": []}) is False
    assert on(["G-01"], {key: value for key, value in C010.items() if key != "guidelines"}) is None
    assert grading.grade_path(c_111, GAVE_UP, a_path())["guideline"] is None


def test_refused_calls_are_counted_from_the_questions_and_not_from_the_counter_that_stops():
    # state["refusals"] stops at MAX_REFUSALS. The path counts every asked entry whose outcome is refused (0.3, 15).
    refused = [{"tool": "AskCashFlow", "agent": "cash_flow", "outcome": "refused", "returned": []}] * 4
    unknown = {"tool": "AskTheMoon", "agent": None, "outcome": "refused", "returned": []}
    nothing = asked("AskCashFlow", "prior_year", outcome="no_answer")
    path = a_path(*refused, unknown, PAYMENT, nothing, refusals=3, searches=[{"query": "q", "returned": []}, {"refused": True}])

    graded = grading.grade_path(C010, REPORT, path)

    assert (graded["questions"], graded["refused"], graded["searches"]) == (2, 5, 1)


def test_a_path_that_holds_none_of_the_keys_is_graded_and_not_an_error():
    # The agent flow writes no plan keys, and a case run before the path was kept has none at all.
    graded = grading.grade_path(C010, REPORT, {})

    assert graded["reached"] is False and graded["why"] == "path"
    assert graded["first_draft"] is None and graded["first_draft_passed"] is None
    assert (graded["questions"], graded["refused"], graded["searches"], graded["nudged"]) == (0, 0, 0, [])
    assert graded["out_of_rounds"] is False and graded["handed_over_by"] is None
    assert grading.grade_path(C010, GAVE_UP, {})["why"] == "gave up"


def test_a_pair_set_against_the_same_history_is_held_only_when_both_are_right():
    # c_303 is c_003 with a precedent that must change nothing. Both permit two actions that are not accepted.
    c_003, c_303 = CASES["c_003"], CASES["c_303"]
    cut = said(likely_cause="revenue_loss", recommendation="reduce_limit")
    paused = said(likely_cause="revenue_loss", recommendation="pause_card")
    runs = [
        recorded(c_003, cut), recorded(c_303, cut),
        recorded(c_003, paused, rep=2), recorded(c_303, paused, rep=2),  # the same wrong answer twice
        recorded(c_003, cut, rep=3), recorded(c_303, paused, rep=3),
    ]  # fmt: skip

    assert grading.compare("history_same_as", runs[1], runs[0]) is True
    assert grading.compare("history_same_as", runs[3], runs[2]) is False
    assert grading.compare("history_same_as", runs[5], runs[4]) is False
    # same_as would have held the equal wrong answers.
    assert grading.compare("same_as", runs[3], runs[2]) is True
    text = grading.summary({"customers": [c_003, c_303]}, runs, [], 3, {"run_id": "r1"})
    assert "| c_303 | history copy of c_003 | 1 of 3 paired runs |" in text


def test_a_case_that_passed_without_the_misleading_memo_being_read_is_counted_as_untested():
    # c_203's memo is on txn_c_203_02. A run that never asked for the payments never read it.
    c_203 = CASES["c_203"]
    cut = said(likely_cause="revenue_loss", recommendation="reduce_limit", guideline_ids=["G-07"])
    income, repayments = asked("AskCashFlow", "income", "agg_c_203_inflows"), asked("AskCreditBehavior", "repayments")
    blind = recorded(c_203, cut, a_path(income, repayments))
    read = recorded(c_203, cut, a_path(asked("AskCashFlow", "transactions", "txn_c_203_02"), income), rep=2)

    assert blind["grades"]["recommendation"] is True and blind["path_grades"]["why"] == "path"
    assert read["path_grades"]["why"] == "right"
    text = grading.summary({"customers": [c_203]}, [blind, read], [], 2, {"run_id": "r1"})
    assert section(text, "Passed without being tested").endswith("\n1 runs (c_203 run 1).\n")
    assert "| c_203 | 2, 2 | 0, 0 | NO | not reached, no | path; right |" in section(text, "The questions asked")
    # A run that never read it and kept the limit did not pass.
    kept = recorded(c_203, said(likely_cause="revenue_loss", recommendation="keep_limit"), a_path(income, repayments), rep=3)
    text = grading.summary({"customers": [c_203]}, [blind, read, kept], [], 3, {"run_id": "r1"})
    assert section(text, "Passed without being tested").endswith("\n1 runs (c_203 run 1).\n")


# A summary of runs without a path. Every link kind, a give-up, a check a case lacks, an error and a request.


def _case(cid: str, split: str = "held_out", causes=("payroll_or_tax",), recommendations=("keep_limit",), **more):
    return {"id": cid, "category": "normal", "split": split, "causes": list(causes),
            "recommendations": list(recommendations), **more}  # fmt: skip


def _graded(case: dict, rep: int, **changes) -> dict:
    told = {
        "likely_cause": "payroll_or_tax",
        "recommendation": "keep_limit",
        "summary": "A payroll run explains the fall.",
        "evidence": [{"claim": "Payroll.", "evidence_ids": ["txn_1"]}],
        "ruled_out": [],
        "other_findings": [],
        "review_reasons": [{"reason": "large_cut", "finding": "Payroll.", "evidence_ids": ["txn_1"]}],
        "guideline_ids": ["G-01"],
        **changes,
    }
    return {"case": case["id"], "rep": rep, "report": told, "grades": grading.grade(case, told)}


_GIVEN_UP = {
    "likely_cause": "no_clear_cause",
    "recommendation": "needs_human",
    "summary": "The report could not be completed. It cites no guideline.",
    "evidence": [],
    "review_reasons": [],
    "guideline_ids": [],
}
_A = _case("c_a", must_cite=["txn_1"], twin_of="c_b")
_B = _case("c_b", split="dev", causes=["revenue_loss"], recommendations=["reduce_limit"], twin_of="c_a")
_C = _case("c_c", same_as="c_a")
_D = _case("c_d", recommendations=["keep_limit", "request_documents"], milder_than="c_b")
_E = _case("c_e", causes=["no_clear_cause"], recommendations=["needs_human"], history_twin_of="c_a")
_F = _case("c_f", history_same_as="c_a")
_G = _case("c_g", causes=["revenue_loss"], recommendations=["reduce_limit"], same_history_as="c_a")
_H = _case("c_h", causes=["insufficient_data"], recommendations=["request_documents"])
NO_PATH_GOLDEN = {"customers": [_A, _B, _C, _D, _E, _F, _G, _H]}
NO_PATH_RUNS = [
    _graded(_A, 1), _graded(_A, 2),
    _graded(_B, 1, likely_cause="revenue_loss", recommendation="reduce_limit"), _graded(_B, 2, recommendation="needs_human"),
    _graded(_C, 1), _graded(_C, 2),
    _graded(_D, 1, recommendation="request_documents"), _graded(_D, 2),
    _graded(_E, 1, **_GIVEN_UP), _graded(_E, 2, likely_cause="no_clear_cause", recommendation="needs_human"),
    _graded(_F, 1), _graded(_F, 2, recommendation="pause_card"),
    _graded(_G, 1, likely_cause="revenue_loss", recommendation="reduce_limit"),
    _graded(_G, 2, likely_cause="revenue_loss", recommendation="reduce_limit"),
    _graded(_H, 1, likely_cause="insufficient_data", recommendation="request_documents"),
    {"case": "c_h", "rep": 2, "error": "the case ended as failed"},
]  # fmt: skip
NO_PATH_REQUESTS = [
    {"id": "t_01", "what": "A bad signal", "expect": {"status": 422}, "got": {"status": 422}, "passed": True},
    {"id": "t_03", "what": "No token", "expect": {"status": 401}, "error": "no run opened a case to ask for"},
]
NO_PATH_ABOUT = {"run_id": "r1", "Model": "scripted | structured output: default", "Arm": "agent"}
# What eval/grade.py wrote for these runs before the path was graded (commit 87b022f), to the byte.
NO_PATH_SUMMARY = (
    '# Golden set results: r1\n'
    '\n'
    '- Model: scripted | structured output: default\n'
    '- Arm: agent\n'
    '- 8 cases, 2 runs each. 1 of 16 runs ended in an error or were not run. That is not counted as a wrong answer: a case with such a run is left out of the shares below. A report the service could not complete in two drafts is counted as a wrong answer.\n'
    '\n'
    '## How often the answer was right in every run\n'
    '\n'
    '| | Recommendation | Cause | Handed over when it should, and only then | Cited the evidence the answer turns on |\n'
    '|---|---|---|---|---|\n'
    '| All cases | 4 of 7 (57%, 95% interval 25% to 84%) | 5 of 7 (71%, 95% interval 36% to 92%) | 5 of 7 (71%, 95% interval 36% to 92%) | 1 of 1 (100%, 95% interval 21% to 100%) |\n'
    '| Held out only | 4 of 6 (67%, 95% interval 30% to 90%) | 5 of 6 (83%, 95% interval 44% to 97%) | 5 of 6 (83%, 95% interval 44% to 97%) | 1 of 1 (100%, 95% interval 21% to 100%) |\n'
    '\n'
    'Against doing nothing clever, on the cases counted for the recommendation above:\n'
    '\n'
    '- The engine alone (always cuts): recommendation right for 2 of 7 (29%, 95% interval 8% to 64%)\n'
    '- Always hand over: recommendation right for 1 of 7 (14%, 95% interval 3% to 51%)\n'
    '\n'
    '## Case by case\n'
    '\n'
    '| Case | Kind | Split | Accepted | Recommended, run by run | Cause, run by run | Recommendation | Cause | Hand over | Cites |\n'
    '|---|---|---|---|---|---|---|---|---|---|\n'
    '| c_a | normal | held_out | keep_limit | keep_limit, keep_limit | payroll_or_tax, payroll_or_tax | yes | yes | yes | yes |\n'
    '| c_b | normal | dev | reduce_limit | reduce_limit, needs_human | revenue_loss, payroll_or_tax | NO | NO | NO |  |\n'
    '| c_c | normal | held_out | keep_limit | keep_limit, keep_limit | payroll_or_tax, payroll_or_tax | yes | yes | yes |  |\n'
    '| c_d | normal | held_out | keep_limit or request_documents | request_documents, keep_limit | payroll_or_tax, payroll_or_tax | yes | yes | yes |  |\n'
    '| c_e | normal | held_out | needs_human | gave up, needs_human | gave up, no_clear_cause | NO | NO | NO |  |\n'
    '| c_f | normal | held_out | keep_limit | keep_limit, pause_card | payroll_or_tax, payroll_or_tax | NO | yes | yes |  |\n'
    '| c_g | normal | held_out | reduce_limit | reduce_limit, reduce_limit | revenue_loss, revenue_loss | yes | yes | yes |  |\n'
    '| c_h | normal | held_out | request_documents | request_documents, error | insufficient_data, error |  |  |  |  |\n'
    '\n'
    '## Cases set against each other\n'
    '\n'
    '| Case | Set against | Held in |\n'
    '|---|---|---|\n'
    '| c_a | twin of c_b | 1 of 2 paired runs |\n'
    '| c_c | same as c_a | 2 of 2 paired runs |\n'
    '| c_d | milder than c_b | 1 of 1 paired runs |\n'
    '| c_e | history twin of c_a | 1 of 2 paired runs |\n'
    '| c_f | history copy of c_a | 1 of 2 paired runs |\n'
    '| c_g | same history as c_a | 2 of 2 paired runs |\n'
    '\n'
    '## Requests the service has to refuse\n'
    '\n'
    '| Request | What is sent | Expected | Got | Right |\n'
    '|---|---|---|---|---|\n'
    '| t_01 | A bad signal | status 422 | status 422 | yes |\n'
    '| t_03 | No token | status 401 | no run opened a case to ask for |  |\n'
    '\n'
    '## Runs that ended in an error\n'
    '\n'
    '- c_h run 2: the case ended as failed\n'
)  # fmt: skip


def test_a_summary_of_records_with_no_path_is_what_it_was_to_the_byte():
    text = grading.summary(NO_PATH_GOLDEN, NO_PATH_RUNS, NO_PATH_REQUESTS, 2, NO_PATH_ABOUT)

    assert text == NO_PATH_SUMMARY
    # With a path the new sections come after all of it, and nothing before them moves.
    pathed = [{**run, "path_grades": grading.grade_path(NO_PATH_GOLDEN["customers"][0], run["report"], a_path())}
              if "grades" in run else run for run in NO_PATH_RUNS]  # fmt: skip
    longer = grading.summary(NO_PATH_GOLDEN, pathed, NO_PATH_REQUESTS, 2, NO_PATH_ABOUT)
    assert longer.startswith(NO_PATH_SUMMARY + "\n## Right on the first draft\n")
    titles = [line for line in longer.removeprefix(NO_PATH_SUMMARY).splitlines() if line.startswith("## ")]
    assert titles == [
        "## Right on the first draft",
        "## The questions asked",
        "## Reports the service could not complete",
        "## Handed over by lowering confidence",
        "## Passed without being tested",
    ]


# The runner, with this checkout standing in for the Orchestrator's container.


class Container:
    """What eval/run.py runs in the Orchestrator's container, run here: this checkout's Python and test database,
    and the eval files where the overlay mounts them. `answers` stands in for the output of a command."""

    def __init__(self, events: list, flow: str = "", answers: dict | None = None):
        self.events, self.answers = events, answers or {}
        self.env = {
            **os.environ,
            # The database the service under test uses. The environment's setting can name another one: conftest is
            # imported twice, as conftest and as tests.conftest, and each time names a fresh file.
            "DATABASE_URL": engine.url.render_as_string(hide_password=False),
            "INVESTIGATION_FLOW": flow,
            "EMBEDDINGS_MODEL": "hash:384",
        }

    def __call__(self, container: str, *command: str) -> str:
        what = command[2] if command[1] == "-m" else "stack"
        self.events.append(what)
        if what in self.answers:
            return self.answers[what]
        here = [sys.executable if part == "python" else part.replace("/eval/", f"{EVAL}/") for part in command]
        return subprocess.run(here, cwd=SERVICE, env=self.env, check=True, capture_output=True, text=True).stdout.strip()


class Recording:
    """The service's test client in place of the runner's httpx.Client. It keeps what was posted, in order."""

    def __init__(self, client, events: list):
        self.client, self.events = client, events

    def get(self, url, **kwargs):
        return self.client.get(url, **kwargs)

    def post(self, url, **kwargs):
        self.events.append(f"POST {url}")
        return self.client.post(url, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def run_main(monkeypatch, service, container: Container, out: Path, arm: str = "agent"):
    monkeypatch.setattr(golden_run, "inside", container)
    recording = SimpleNamespace(**{**vars(httpx), "Client": lambda **_: Recording(service.client, container.events)})
    monkeypatch.setattr(golden_run, "httpx", recording)
    argv = ["--arm", arm, "--container", "the container", "--only", "c_010", "--reps", "1", "--timeout", "5", "--out", str(out)]
    monkeypatch.setattr(sys, "argv", ["run.py", *argv])
    golden_run.main()


def test_the_run_record_holds_every_policy_value_and_the_prompts_hash(monkeypatch):
    monkeypatch.setattr(golden_run, "inside", Container([]))

    about = golden_run.stack("the container", "agent")

    # Every value, read inside the container, as one JSON line.
    values = {name: value for name, value in vars(policy).items() if name.isupper()}
    assert {"POLICY_VERSION", "EXPLAINS_BP", "UNPAID_MEANS", "REPEAT_CAUSES", "RULES", "SAME_FALL_BP"} <= values.keys()
    assert "\n" not in about["Policy"] and json.loads(about["Policy"]) == json.loads(json.dumps(values))
    digest = hashlib.sha256((SERVICE / "app" / "graph" / "prompts.py").read_bytes()).hexdigest()[:12]
    assert about["Prompts"] == f"sha256 {digest} of app/graph/prompts.py"
    assert (about["Arm"], about["Search"]) == ("agent", "on, hash:384")
    assert about["Model"] == "none set | structured output: default"
    # A fixed arm never searches, whatever embedder the image has.
    monkeypatch.setattr(golden_run, "inside", Container([], flow="fixed"))
    assert golden_run.stack("the container", "fixed")["Search"] == "off: this arm asks a fixed list and never searches"

    # What the container answers is what the record holds, though this checkout's files say otherwise.
    there = {"model": "m", "structured_output": "json_schema", "flow": "", "embedder": None,
             "policy": {"POLICY_VERSION": "elsewhere"}, "prompts": "abcdefabcdef"}  # fmt: skip
    monkeypatch.setattr(golden_run, "inside", Container([], answers={"stack": "a log line\n" + json.dumps(there)}))
    about = golden_run.stack("the container", "agent")
    assert about["Prompts"] == "sha256 abcdefabcdef of app/graph/prompts.py"
    assert about["Policy"] == '{"POLICY_VERSION": "elsewhere"}'
    assert about["Model"] == "m | structured output: json_schema"
    assert about["Search"] == "off: no embedder could be loaded"


def test_the_runner_refuses_an_arm_the_stack_is_not_set_to(monkeypatch, start, tmp_path):
    for flow, arm in (("fixed", "agent"), ("", "fixed"), ("", "fixed_rules"), ("agent", "fixed_rules")):
        monkeypatch.setattr(golden_run, "inside", Container([], flow=flow))
        with pytest.raises(SystemExit, match=f"not the {arm} arm"):
            golden_run.stack("the container", arm)
    # A value the runner does not know is no arm at all, though the service runs it as the agent.
    for arm in golden_run.ARMS:
        monkeypatch.setattr(golden_run, "inside", Container([], flow="fixd"))
        with pytest.raises(SystemExit, match="INVESTIGATION_FLOW=fixd, which names no arm .* a misspelt arm"):
            golden_run.stack("the container", arm)

    # Before anything is seeded or sent.
    service, container = start([*INVESTIGATION, report()]), Container([], flow="fixed_rules")
    with pytest.raises(SystemExit):
        run_main(monkeypatch, service, container, tmp_path, arm="agent")
    assert container.events == ["stack"]
    assert list(tmp_path.iterdir()) == []


def test_the_runner_seeds_history_before_the_first_signal_and_stops_when_the_database_is_not_clean(
    monkeypatch, start, tmp_path
):
    service = start([*INVESTIGATION, report()])
    # A review that is not a seed, dated before the data clock, is somebody's earlier run.
    with SessionLocal() as session:
        history.seed(session, [seed_entry("case_earlier", "2026-10-01")], expect_clean=False)
        session.execute(update(Review).values(source="case"))
        session.commit()
    unclean = Container([])

    with pytest.raises(SystemExit, match="Not seeded: the database holds 1 reviews that are not seeds"):
        run_main(monkeypatch, service, unclean, tmp_path / "unclean")

    assert "app.seed_history" in unclean.events and "POST /signals" not in unclean.events
    assert not (tmp_path / "unclean").exists()
    with SessionLocal() as session:
        assert session.scalars(select(Case.case_id)).all() == ["case_earlier"]  # nothing was seeded either

    with SessionLocal() as session:
        session.execute(delete(Review))
        session.execute(delete(Case))
        session.commit()
    clean = Container([])

    run_main(monkeypatch, service, clean, tmp_path / "clean")

    # The service seeds nothing here (the tests' seed file is empty), so the runner seeded both files whole.
    seeds = history.read_seeds(list(golden_run.SEED_FILES.values()))
    assert len(seeds) == 15
    with SessionLocal() as session:
        stored = session.scalars(select(Review.case_id).where(Review.source == "seed")).all()
    assert sorted(stored) == sorted(seed["case_id"] for seed in seeds)
    assert clean.events[0] == "stack"
    assert clean.events.index("app.seed_history") < clean.events.index("POST /signals")
    (folder,) = (tmp_path / "clean").iterdir()
    assert folder.name.endswith("-agent")
    (record,) = [json.loads(line) for line in (folder / "runs.jsonl").read_text().splitlines()]
    assert record["arm"] == "agent" and record["case"] == "c_010"
    assert record["path"]["questions"] and record["path_grades"]["questions"] == 3
    text = (folder / "summary.md").read_text()
    assert "- Arm: agent\n" in text and "- Search: on, hash:384\n" in text
    assert "- Seeded reviews: 15: 0 there at start, 15 added by the runner, 15 embedded\n" in text
    assert f"- Policy: {golden_run.stack('the container', 'agent')['Policy']}\n" in text
    assert "\n## Right on the first draft\n" in text


def test_the_runner_stops_when_the_service_does_not_list_the_seeded_reviews(monkeypatch, start, tmp_path):
    # The command said every seed was there, but the service the signals go to has none of them.
    service, events = start([*INVESTIGATION, report()]), []
    said_there = json.dumps({"seeded": 0, "already_there": 15, "embedded": 0})

    with pytest.raises(SystemExit, match="does not list these seeded reviews: seed_c_007_2025, seed_c_307_2025"):
        run_main(monkeypatch, service, Container(events, answers={"app.seed_history": said_there}), tmp_path)
    assert "POST /signals" not in events


class PathDown:
    """A client on which the path page fails as `fail` does: with an error answer, or by raising."""

    def __init__(self, client, fail=lambda: httpx.Response(500, text="down")):
        self.client, self.fail = client, fail

    def get(self, url, **kwargs):
        if url.endswith("/path"):
            return self.fail()
        return self.client.get(url, **kwargs)

    def post(self, url, **kwargs):
        return self.client.post(url, **kwargs)


def test_a_run_whose_path_page_cannot_be_read_keeps_its_grade_and_its_case_is_closed(start):
    service = start([*INVESTIGATION, report()])

    record = golden_run.one_run(PathDown(service.client), tokens(), C010, SIGNAL, 1, timeout=5)

    assert record["grades"]["recommendation"] is True and record["closed"] is True
    assert record["path_error"] == "http 500 down" and "path_grades" not in record


def timed_out() -> httpx.Response:
    raise httpx.ReadTimeout("timed out")


def test_a_run_whose_path_page_does_not_answer_keeps_its_grade_and_the_next_run_goes_ahead(start):
    service = start([*INVESTIGATION, report(), *INVESTIGATION, report(), *INVESTIGATION, report()])

    first, second = golden_run.runs_of(PathDown(service.client, timed_out), tokens(), C010, SIGNAL, "r1", 2, 5)

    assert first["grades"]["recommendation"] is True and first["closed"] is True
    assert first["path_error"] == "ReadTimeout: timed out" and "path" not in first and "path_grades" not in first
    assert "error" not in second and second["grades"]["recommendation"] is True
    # A page that answers with something that is not JSON.
    not_json = PathDown(service.client, lambda: httpx.Response(200, text="<html>"))
    record = golden_run.one_run(not_json, tokens(), C010, {**SIGNAL, "signal_id": "sig_3"}, 3, timeout=5)
    assert record["closed"] is True and record["path_error"].startswith("JSONDecodeError: ")


def test_the_evaluation_overlay_seeds_both_files_and_mounts_them_read_only():
    overlay = yaml.safe_load((EVAL / "docker-compose.eval.yml").read_text())["services"]["orchestrator"]

    assert overlay["environment"]["HISTORY_SEED_FILE"] == "/eval/history.yaml:/eval/history_eval.yaml"
    assert overlay["environment"]["SHARED_CUSTOMERS_FILE"] == "/data/shared_customers.yaml:/eval/extra_customers.yaml"
    assert overlay["environment"]["INVESTIGATION_FLOW"] == "${INVESTIGATION_FLOW:-}"
    assert overlay["build"]["args"] == {"WITH_EMBEDDINGS": "1"}
    assert set(overlay["volumes"]) == {
        "./eval/extra_customers.yaml:/eval/extra_customers.yaml:ro",
        "./eval/history.yaml:/eval/history.yaml:ro",
        "./eval/history_eval.yaml:/eval/history_eval.yaml:ro",
    }
    # The runner seeds the files the service seeds.
    assert list(golden_run.SEED_FILES) == overlay["environment"]["HISTORY_SEED_FILE"].split(":")
    for mount in overlay["volumes"]:
        source, target, _ = mount.split(":")
        assert (EVAL.parent / source).is_file()
        assert target not in golden_run.SEED_FILES or golden_run.SEED_FILES[target] == EVAL.parent / source


# Two arms side by side.

R1, R2, R3 = (_case(f"c_r{n}", recommendations=["reduce_limit"], must_cite=["rep_1"]) for n in (1, 2, 3))
D1 = _case("c_d1", recommendations=["reduce_limit"], must_reach=["agg_payee"])
CLASSES = {"c_r1": "R", "c_r2": "R", "c_r3": "R", "c_d1": "D", "c_gone": "R"}
RIGHT = said(evidence=[{"claim": "It failed.", "evidence_ids": ["rep_1"]}], review_reasons=[])
WRONG = {**RIGHT, "recommendation": "keep_limit"}
REFUSED_FIRST = [drafted(WRONG, "refused", ("guideline_allows",)), drafted(RIGHT, number=2)]
PAYEE_FOUND = asked("AskCashFlow", "payee_history", "agg_payee", record_id="txn_1")
NUDGED = a_path(PAYMENT, INCOME, PAYEE_FOUND, gaps=[code_gap(2, tool="AskCashFlow", topic="payee_history")])


def run_of(case: dict, final: dict, *questions: dict, drafts: list | None = None, gaps: list = (), **more) -> dict:
    """A run whose draft log holds its final report as its one draft, unless told otherwise."""
    return recorded(case, final, a_path(*questions, drafts=drafts or [drafted(final)], gaps=gaps), **more)


def arm_a() -> list[dict]:
    """The agent: c_r1 right, c_r2 right only after a refused first draft, c_r3 wrong, c_d1 reached once by itself."""
    return [
        run_of(R1, RIGHT, PAYMENT, INCOME), run_of(R1, RIGHT, PAYMENT, INCOME, INCOME, rep=2),
        run_of(R2, RIGHT, drafts=REFUSED_FIRST), run_of(R2, RIGHT, drafts=REFUSED_FIRST, rep=2),
        run_of(R3, WRONG), run_of(R3, WRONG, rep=2),
        run_of(D1, RIGHT, PAYMENT, PAYEE_FOUND), run_of(D1, RIGHT, *NUDGED["questions"], gaps=NUDGED["gaps"], rep=2),
        run_of(_case("c_gone"), RIGHT), run_of(_case("c_gone"), RIGHT, rep=2),
    ]  # fmt: skip


def arm_b() -> list[dict]:
    """A fixed list of eight: every case right at once, c_d1 never reached, c_gone broke once."""
    runs = [run_of(case, RIGHT, *[PAYMENT] * 8, rep=rep, arm="fixed") for case in (R1, R2, R3, D1) for rep in (1, 2)]
    return [*runs, run_of(_case("c_gone"), RIGHT, arm="fixed"), {"arm": "fixed", "case": "c_gone", "rep": 2, "error": "x"}]


def rows(text: str, title: str) -> dict[tuple[str, str], list[str]]:
    """The class-and-check rows of one section of the paired table, as their cells."""
    return {
        (cells[0], cells[1]): cells[2:]
        for line in section(text, title).splitlines()
        if line.startswith("| ") and not line.startswith(("| Class", "|---"))
        for cells in [line.strip("| ").split(" | ")]
    }


def test_the_paired_table_puts_each_case_in_exactly_one_cell_and_leaves_out_a_case_with_a_missing_run():
    text = pairing.paired(arm_a(), arm_b(), CLASSES, ("a.jsonl", "b.jsonl"))

    assert "- A: agent, 2 runs of each case, from a.jsonl" in text and "- B: fixed, 2 runs of each case, from b.jsonl" in text
    assert "- 4 cases in both. Left out, a run missing or not graded with its path: c_gone." in text
    assert "c_gone |" not in text
    # A case the dataset's document does not know has no class to be counted under.
    stray = pairing.paired([*arm_a(), recorded(_case("c_stray"))], arm_b(), CLASSES, ("a.jsonl", "b.jsonl"))
    assert "- Left out, not in the dataset's document, so of no class: c_stray." in stray
    assert "c_stray |" not in stray
    # A case with fewer runs than the arm's others, every one graded: a file cut short while a paid run was stopped.
    r4 = _case("c_r4", recommendations=["reduce_limit"], must_cite=["rep_1"])
    short = pairing.paired(
        [*arm_a(), run_of(r4, RIGHT), run_of(r4, RIGHT, rep=2)], [*arm_b(), run_of(r4, RIGHT, arm="fixed")],
        CLASSES | {"c_r4": "R"}, ("a.jsonl", "b.jsonl"),
    )  # fmt: skip
    assert "- 4 cases in both. Left out, a run missing or not graded with its path: c_gone, c_r4." in short
    assert "c_r4 |" not in short
    final = rows(text, "On the final report")
    assert final[("R", "recommendation")] == ["2", "0", "1: c_r3", "0"]
    assert final[("D", "recommendation")] == ["1", "0", "0", "0"]
    # c_d1 has no must_cite: it is in no cell for cites.
    assert final[("R", "cites")] == ["3", "0", "0", "0"] and final[("D", "cites")] == ["0", "0", "0", "0"]
    for (kind, check), cells in final.items():
        named = [cid for cell in cells[1:3] if ": " in cell for cid in cell.split(": ")[1].split(", ")]
        counted = int(cells[0]) + len(named) + int(cells[3])
        has_check = [cid for cid, mark in CLASSES.items() if mark == kind and cid != "c_gone"]
        if check == "cites":
            has_check = [cid for cid in has_check if cid != "c_d1"]
        assert counted == len(has_check), (kind, check)
        assert len(set(named)) == len(named)


def test_an_arm_file_that_holds_no_runs_pairs_no_case():
    # The runner opens runs.jsonl before its first run ends. Stopped then, it leaves the file empty.
    text = pairing.paired(arm_a(), [], CLASSES, ("a.jsonl", "b.jsonl"))

    assert "- 0 cases in both. Left out, a run missing or not graded with its path: c_r1, c_r2, c_r3, c_d1, c_gone." in text
    for case_id in CLASSES:
        assert f"| {case_id} |" not in text


def test_the_paired_table_shows_the_questions_each_arm_asked_side_by_side():
    text = pairing.paired(arm_a(), arm_b(), CLASSES, ("a.jsonl", "b.jsonl"))

    asked_rows = section(text, "Questions asked")
    assert "| c_r1 | R | 2, 3 | 8, 8 | 0, 0 | 0, 0 |" in asked_rows
    assert "| c_d1 | D | 2, 3 | 8, 8 | 0, 0 | 0, 0 |" in asked_rows
    assert "| A: agent | 8 | 1.2 | 0 | 0 | 0 |" in asked_rows
    assert "| B: fixed | 8 | 8.0 | 0 | 0 | 0 |" in asked_rows
    # Only the agent's class D runs are split by the code nudge.
    agent = section(text, "The agent on class D")
    assert "| Case | A: agent |\n|---|---|\n| c_d1 | unprompted, after a nudge |\n" in agent

    # A refused call and a give-up are counted where they happened.
    refused = {"tool": "AskTheMoon", "agent": None, "outcome": "refused", "returned": []}
    gave_up = run_of(R3, GAVE_UP, drafts=[drafted(GAVE_UP, "gave_up", ("cause",))], rep=2)
    costly = [*arm_a()[:4], run_of(R3, WRONG, refused), gave_up, *arm_a()[6:]]
    asked_rows = section(pairing.paired(costly, arm_b(), CLASSES, ("a.jsonl", "b.jsonl")), "Questions asked")
    assert "| c_r3 | R | 0, 0 | 8, 8 | 1, 0 | 0, 0 |" in asked_rows
    assert "| A: agent | 8 | 1.2 | 1 | 0 | 1 |" in asked_rows


def test_the_paired_table_gives_the_first_draft_before_the_final_report():
    text = pairing.paired(arm_a(), arm_b(), CLASSES, ("a.jsonl", "b.jsonl"))

    assert text.index("\n## On the first checked draft\n") < text.index("\n## On the final report\n")
    # c_r2 was right in the end in both arms. On the first draft only the fixed arm was.
    assert rows(text, "On the first checked draft")[("R", "recommendation")] == ["1", "0", "2: c_r2, c_r3", "0"]
    assert rows(text, "On the final report")[("R", "recommendation")] == ["2", "0", "1: c_r3", "0"]
    assert "| c_r2 | R | NO / yes / yes | yes / yes / yes | yes / yes / yes | yes / yes / yes |" in text
    assert rows(text, "On the first checked draft")[("R", "cites")] == ["3", "0", "0", "0"]


def test_the_classes_are_read_from_the_dataset_document():
    found = pairing.classes(EVAL / "golden-set.md")

    assert list(found) == [case["id"] for case in GOLDEN["customers"]]
    assert {cid for cid, mark in found.items() if mark == "D"} == {"c_009", "c_011", "c_307"}
