"""The 90K incident (spec 9.1).

In the pilot a report on c_010 said that weekly inflows of about $80,000 would make true outflows
about $310,000, so roughly $90,000 was not accounted for. The model had worked both figures out
itself, no record held them, and the analyst read them as findings. These tests are the part the
report check can stop on the figures alone, on the test guidelines. What stops the incident where
its figure happens to be a record value is the guideline, cause and gap rules, tested apart.
"""

import functools
import sys
from pathlib import Path

import pytest

from app import history, policy
from app.agents import mock
from app.db import Base, SessionLocal, engine
from app.graph import facts, figures, prompts
from contracts import Finding
# Run uses the test signal of c_010, whose repayments are $41,000, $46,000 and $50,000.
from tests.test_graph import (
    APPROVE,
    DONE,
    INVESTIGATION,
    PAUSE_UNDER_G08B,
    REPORT,
    Run,
    ask_bank,
    ask_card,
    call,
    gaps,
    nudges,
    on_the_services_guidelines,
    report,
    signal_of,
    with_both_files,
)

EVAL = Path(__file__).resolve().parents[3] / "eval"
sys.path.insert(0, str(EVAL))
import signals  # noqa: E402

PILOT = (
    "Weekly inflows of about $80,000 would make true outflows about $310,000, "
    "so roughly $90,000 is not accounted for. Records and balances do not match."
)
INFLOWS = {"claim": "Weekly inflows are steady at about $80,000 over the last 13 weeks.", "evidence_ids": ["agg_c_010_inflows"]}
# The pilot's summary in a report that is otherwise in order, citing the inflows its $80,000 comes from.
PILOT_REPORT = {"summary": PILOT, "other_findings": [*REPORT["other_findings"], INFLOWS]}
HAND_OVER = {
    **PILOT_REPORT,
    "likely_cause": "insufficient_data",
    "recommendation": "needs_human",
    "guideline_ids": [],
    "evidence": [],
}
FIGURES_REFUSED = "It states figures that are not in the evidence it cites for them: "


def redraft_brief(run: Run) -> str:
    """What the second draft was told: the last message of the last call to the model."""
    return run.seen[-1][-1].content


def figure_line(run: Run) -> str:
    return next(line for line in redraft_brief(run).splitlines() if line.startswith(f"- {FIGURES_REFUSED}"))


def test_a_figure_the_model_worked_out_is_refused_whatever_else_the_report_says():
    # The hand-over gives insufficient_data and cites no record that shows it, so the cause rule refuses it too.
    for first_draft, codes in ((report(**PILOT_REPORT), ["figure"]), (report(**HAND_OVER), ["cause", "figure"])):
        run = Run([*INVESTIGATION, first_draft, report()])

        assert run.result["drafts"] == 2
        assert figure_line(run) == (
            f"- {FIGURES_REFUSED}summary: $310,000, $90,000. "
            "State only figures the cited evidence shows, exactly as shown, or leave the figure out."
        )
        assert [entry["codes"] for entry in run.result["draft_log"]] == [codes, []]
        assert run.result["check"] == {"passed": True, "problems": []}


def test_a_weekly_figure_scaled_to_another_period_is_refused():
    scaled = {"claim": "Inflows came to $160,000 over two weeks.", "evidence_ids": ["agg_c_010_inflows"]}

    run = Run([*INVESTIGATION, report(other_findings=[*REPORT["other_findings"], scaled]), report()])

    # No record of the case holds $160,000, so none is named.
    assert figure_line(run).startswith(f"- {FIGURES_REFUSED}other_findings 2: $160,000. ")


def test_a_figure_from_a_record_the_sentence_does_not_cite_is_refused_and_the_record_is_named():
    short = {
        "claim": "Only $220,000 of $230,000 is explained, so $10,000 is missing.",
        "evidence_ids": ["txn_c_010_01", "txn_c_010_02"],
    }

    run = Run([*INVESTIGATION, report(evidence=[short]), report()])

    assert "$10,000 (agg_c_010_payments shows it and is not cited there)" in figure_line(run)
    # Each figure is true of the case, and none is in the two payments the sentence rests on.
    for figure in ("$220,000", "$230,000"):
        assert f"{figure} (" in figure_line(run)


def test_an_amount_written_in_cents_is_refused():
    for written in ("$23,000,000", "23,000,000"):
        in_cents = {"claim": f"Linked cash fell by {written}.", "evidence_ids": ["bal_c_010"]}

        run = Run([*INVESTIGATION, report(other_findings=[in_cents]), report()])

        assert figure_line(run).startswith(f"- {FIGURES_REFUSED}other_findings 1: {written}. ")


def test_on_the_evaluations_signal_only_the_figure_no_record_holds_is_refused():
    """On the evaluation's signal c_010's two paid repayments are $90,000 each, and so is the
    monthly card spend. So $90,000 is a record value there, and once the report cites one of those
    records the figure rule has nothing to say against the incident's sentence. Only $310,000 is
    refused. For these cases the figure rule does not stop the incident: the guideline, cause and
    gap rules do, and test_the_90K_draft_is_refused_for_its_figures_its_guideline_and_its_cause
    (P7) asserts that.
    """
    today, customers = signals.customers()
    signal = signals.signal_for(customers["c_010"], today)
    cites_repayments = [
        REPORT["review_reasons"][0],
        {**REPORT["review_reasons"][1], "evidence_ids": ["rep_c_010_3", "sig_repayments"]},
    ]
    investigation = [*INVESTIGATION[:3], ask_card("How is the card used?", topic="card_use"), DONE, gaps()]

    run = Run([*investigation, report(**PILOT_REPORT, review_reasons=cites_repayments), report()], signal=signal)

    assert {"due": "2026-09-05", "amount_cents": 9000000, "status": "paid"} in signal["snapshot"]["card"]["repayments"]
    assert figure_line(run).startswith(f"- {FIGURES_REFUSED}summary: $310,000. ")


def says_too_much(request):
    """The stand-in, with a finding whose figure no record holds."""
    answer = mock.cash_flow(request)
    if request.topic == "transactions":
        answer.findings.append(Finding(claim="About $90,000 is not accounted for.", confidence="low", evidence_ids=["bal_c_010"]))
    return answer


def test_two_refused_drafts_hand_the_case_over_with_what_the_specialists_found_and_without_the_made_up_figure():
    run = Run(
        [*INVESTIGATION, report(**PILOT_REPORT), report(**PILOT_REPORT)],
        specialists={"cash_flow": says_too_much, "credit_behavior": mock.credit_behavior},
    )

    given_up = run.shown["report"]
    assert given_up["recommendation"] == "needs_human"
    assert given_up["summary"] == (
        "The report could not be completed. It stated figures that are not in the evidence it cited."
    )
    found = given_up["other_findings"]
    assert {"claim": "A payroll run of $150,000 went out in the window.", "evidence_ids": ["txn_c_010_01"]} in found
    assert {"claim": "The last 3 repayments, oldest first: paid, paid, failed.",
            "evidence_ids": ["rep_c_010_1", "rep_c_010_2", "rep_c_010_3"]} in found  # fmt: skip
    assert INFLOWS in found
    # The analyst reads neither the draft's made-up figures nor a specialist's.
    for figure in ("$90,000", "$310,000"):
        assert figure not in str(given_up)
    assert run.result["draft_log"][-1]["result"] == "gave_up"


def test_on_the_evaluations_signal_a_finding_is_handed_over_only_when_the_records_it_cites_show_its_figures():
    """$90,000 is a record value on the evaluation's signal (sig_repayments), and a date can be one too. A
    finding is kept only when the records it cites itself show its figures, checked as strictly as a report."""
    today, customers = signals.customers()
    unbacked = [
        Finding(claim="About $90,000 is not accounted for.", confidence="low", evidence_ids=["bal_c_010"]),
        Finding(claim="The fall began on 2026-10-01.", confidence="low", evidence_ids=["bal_c_010"]),
    ]

    def says_too_much_twice(request):
        answer = mock.cash_flow(request)
        if request.topic == "transactions":
            answer.findings += unbacked
        return answer

    run = Run(
        [*INVESTIGATION, report(**PILOT_REPORT), report(**PILOT_REPORT)],
        specialists={"cash_flow": says_too_much_twice, "credit_behavior": mock.credit_behavior},
        signal=signals.signal_for(customers["c_010"], today),
    )

    assert "m:90000" in figures.allowed_figures(facts.ledger(run.state).values())
    assert run.result["draft_log"][-1]["result"] == "gave_up"
    kept = [finding["claim"] for finding in run.shown["report"]["other_findings"]]
    assert "A payroll run of $150,000 went out in the window." in kept
    for finding in unbacked:
        assert finding.claim not in kept


# What stops the incident where its figure is no help: the guideline, cause and gap rules (spec 9.1, P7).

# The pilot's draft as it was: records that do not match, handed to a person under G-10b.
PILOT_HAND_OVER = {**HAND_OVER, "guideline_ids": ["G-10b"]}


def test_the_90K_draft_is_refused_for_its_figures_its_guideline_and_its_cause():
    """The regression. The spec records that on commit 072ba7e this first draft passed the check. On the code
    of wave 2 (62604b7) it was refused for its figures alone (codes ["figure"]), and the same draft without
    its figures passed the check: both were run on that code."""
    run = on_the_services_guidelines([*INVESTIGATION, report(**PILOT_HAND_OVER), report(**PAUSE_UNDER_G08B)])

    first, second = run.result["draft_log"]
    assert first["codes"] == ["guideline_need", "cause", "figure"]
    told = [line for line in redraft_brief(run).splitlines() if line.startswith("- ")]
    assert told == [
        "- G-10b needs a reconciliation that says balances and transactions do not match. "
        "The report cites no record that shows it.",
        "- It gives insufficient_data as the likely cause, but cites no record that shows it. One of these would: "
        "a linked account that is not reporting; a reconciliation that says balances and transactions do not match.",
        f"- {FIGURES_REFUSED}summary: $310,000, $90,000. "
        "State only figures the cited evidence shows, exactly as shown, or leave the figure out.",
    ]
    assert second["result"] == "passed"
    assert run.result["check"] == {"passed": True, "problems": []}
    assert run.shown["report"]["recommendation"] == "pause_card"


def test_on_the_evaluations_signal_the_90K_draft_is_refused_for_its_guideline_and_its_cause_though_90K_is_a_record_there():
    today, customers = signals.customers()
    signal = signals.signal_for(customers["c_010"], today)
    # sig_repayments holds the two paid repayments of $90,000 each, so citing it backs the incident's figure.
    cites_repayments = [
        REPORT["review_reasons"][0],
        {**REPORT["review_reasons"][1], "evidence_ids": ["rep_c_010_3", "sig_repayments"]},
    ]
    investigation = [*INVESTIGATION[:3], ask_card("How is the card used?", topic="card_use"), DONE, gaps()]
    pilot = {**PILOT_HAND_OVER, "review_reasons": cites_repayments}
    only_90K = {**pilot, "summary": "Roughly $90,000 is not accounted for. Records and balances do not match."}

    pilot_run, only_90K_run = (
        on_the_services_guidelines([*investigation, report(**draft), report(**PAUSE_UNDER_G08B)], signal=signal)
        for draft in (pilot, only_90K)
    )

    # The pilot's summary: of its two figures only the one no record holds is refused.
    assert pilot_run.result["draft_log"][0]["codes"] == ["guideline_need", "cause", "figure"]
    assert figure_line(pilot_run).startswith(f"- {FIGURES_REFUSED}summary: $310,000. ")
    assert "$90,000" not in figure_line(pilot_run)
    # With only the figure a record holds, the figure rule has nothing to say. The draft is refused all the same.
    assert only_90K_run.result["draft_log"][0]["codes"] == ["guideline_need", "cause"]
    for run in (pilot_run, only_90K_run):
        assert run.result["draft_log"][1]["result"] == "passed"
        assert run.shown["report"]["recommendation"] == "pause_card"


PILOT_GAP = "With inflows of $80,000 outflows were about $310,000, so roughly $90,000 is not accounted for."


def test_a_gap_that_states_a_figure_is_dropped_and_reaches_neither_the_investigator_nor_the_report_writer():
    kept = "Why the repayment failed is not determined."

    run = on_the_services_guidelines(
        [*INVESTIGATION[:3], DONE, gaps(PILOT_GAP, kept), DONE, gaps(), report(**PAUSE_UNDER_G08B)]
    )

    # The gap that states no figure goes back. The other goes nowhere.
    assert nudges(run) == [f"Still unexplained:\n- {kept}"]
    assert run.result["gap_log"][0] == {
        "by": "model", "gaps": [kept], "asks": [], "dropped": 1, "at": 3, "ended_by": "done"
    }  # fmt: skip
    review = next(n for n, messages in enumerate(run.seen) if messages[0].content == prompts.GAPS)
    for messages in run.seen[review + 1 :]:
        for message in messages:
            assert "$90,000" not in message.content and "$310,000" not in message.content
    assert run.result["check"] == {"passed": True, "problems": []}


def test_on_the_evaluations_signal_a_gap_with_the_incidents_figure_is_dropped_though_90K_is_a_record_there():
    """The rule is "no figure", not "no figure the ledger lacks" (2.D.3): here a record holds $90,000."""
    only_90K = "Roughly $90,000 is not accounted for."

    run = on_the_services_guidelines(
        [*INVESTIGATION[:3], DONE, gaps(only_90K), report(**PAUSE_UNDER_G08B)], signal=signal_of("c_010")
    )

    assert "m:90000" in figures.allowed_figures(facts.ledger(run.state).values())
    assert nudges(run) == []
    assert run.result["gap_log"] == [
        {"by": "model", "gaps": [], "asks": [], "dropped": 1, "at": 3, "ended_by": "done"}
    ]  # fmt: skip
    review = next(n for n, messages in enumerate(run.seen) if messages[0].content == prompts.GAPS)
    for messages in run.seen[review + 1 :]:
        assert not any("not accounted for" in message.content for message in messages)


def test_a_passing_report_with_low_confidence_reaches_the_analyst_as_a_hand_over_and_the_path_says_how():
    """Nothing checks why confidence is low, so this hands a case over past every rule of the check: the
    shape of the incident without its figure. It stays open on purpose until the owner rules (decision 16),
    and it is made visible instead: the draft log says the report passed with low confidence."""
    run = on_the_services_guidelines([*INVESTIGATION, report(**PAUSE_UNDER_G08B, confidence="low")])

    assert run.result["check"] == {"passed": True, "problems": []}
    assert run.shown["report"]["recommendation"] == "needs_human"
    assert [(entry["result"], entry["recommendation"]) for entry in run.result["draft_log"]] == [
        ("passed_low_confidence", "pause_card")
    ]
    assert run.decide(APPROVE)["status"] == "done"
    assert run.sent == []


# The c_007 seed every stack has, and the evaluation's own (spec 0.3, item 1).
HISTORY_FILES = (EVAL / "history.yaml", EVAL / "history_eval.yaml")


@pytest.fixture
def earlier_reviews(monkeypatch):
    """The lookup the service wires, on the evaluation's seeded history, with the stand-ins on its customers.

    The tables are made again before and after, so no review is left behind for a later test.
    """
    with_both_files(monkeypatch)
    today, _ = signals.customers()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        with SessionLocal() as session:
            history.seed(session, history.read_seeds(HISTORY_FILES), expect_clean=True, clock=today)
            session.commit()
        yield functools.partial(history.earlier_reviews, limit=None)
    finally:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)


# c_314: documents were asked for twice since June for the same weak income (spec 8.1, 8.2).
NOTHING_FITS = {
    "likely_cause": "no_clear_cause",
    "summary": "No guideline fits. Documents were asked for twice in the last six months.",
    "evidence": [],
    "ruled_out": [],
    "other_findings": [{"claim": "Documents were asked for twice in the window.", "evidence_ids": ["hist_c_314_summary"]}],
    "review_reasons": [{"reason": "large_cut", "finding": "Not determined.", "evidence_ids": ["sig_limit"]}],
    "confidence": "medium",
    "recommendation": "needs_human",
    "guideline_ids": ["G-11"],
    "next_action": None,
}


def test_a_hand_over_because_nothing_fits_is_refused_while_a_guideline_is_still_unsettled(earlier_reviews):
    hand_over = call("ReportDraft", **NOTHING_FITS)
    signal = signal_of("c_314")

    # DONE at once, and both nudges ignored.
    thin = on_the_services_guidelines(
        [DONE, DONE, DONE, hand_over, hand_over], signal=signal, earlier_reviews=earlier_reviews
    )
    on_its_path = on_the_services_guidelines(
        [ask_bank("Where did the money go?"), ask_bank("Is income steady?", topic="income"),
         ask_bank("Do the books agree?", topic="reconciliation"), DONE, gaps(), hand_over],
        signal=signal, earlier_reviews=earlier_reviews,
    )  # fmt: skip

    ledger = facts.ledger(thin.state)
    assert ledger["hist_c_314_summary"]["data"]["document_requests_for_income_in_window"] == 2
    assert {"hist_seed_c_314_a", "hist_seed_c_314_b"} <= set(ledger)
    assert len(nudges(thin)) == 2
    not_settled = (
        "G-11 says no guideline fits, but that is not settled. A guideline that allows an action needs a record "
        "nobody asked for: AskCashFlow, topic transactions; AskCashFlow, topic income. "
        "The report cannot say that none fits."
    )
    assert [entry["codes"] for entry in thin.result["draft_log"]] == [["not_settled"], ["not_settled"]]
    assert thin.result["draft_log"][-1]["result"] == "gave_up"
    assert thin.shown["report"]["summary"] == f"The report could not be completed. {not_settled}"
    assert thin.shown["report"]["recommendation"] == "needs_human"

    assert [entry["outcome"] for entry in on_its_path.result["asked"]] == ["answered"] * 3
    # An unknown customer is answered too, so this is what shows the stand-ins read c_314's records.
    assert {"txn_c_314_01", "agg_c_314_inflows"} <= set(facts.ledger(on_its_path.state))
    assert on_its_path.result["check"] == {"passed": True, "problems": []}
    assert on_its_path.shown["report"]["guideline_ids"] == ["G-11"]


def no_answer_on_income(request):
    """The bank side, with no answer on the income topic only."""
    if request.topic == "income":
        raise TimeoutError("the Bank Service did not answer")
    return mock.cash_flow(request)


def test_a_question_sent_without_an_answer_settles_what_rests_on_it_so_nothing_fits_can_pass(earlier_reviews):
    # The same path, but income gets no answer. It was asked, so the needs that rest on it count as failed (2.D.3).
    run = on_the_services_guidelines(
        [ask_bank("Where did the money go?"), ask_bank("Is income steady?", topic="income"),
         ask_bank("Do the books agree?", topic="reconciliation"), DONE, gaps(), call("ReportDraft", **NOTHING_FITS)],
        signal=signal_of("c_314"), earlier_reviews=earlier_reviews,
        specialists={"cash_flow": no_answer_on_income, "credit_behavior": mock.credit_behavior},
    )  # fmt: skip

    assert [entry["outcome"] for entry in run.result["asked"]] == ["answered", "no_answer", "answered"]
    assert {"txn_c_314_01", "agg_c_314_reconciliation"} <= set(facts.ledger(run.state))
    assert run.result["draft_log"][0]["codes"] == []
    assert run.result["check"] == {"passed": True, "problems": []}
    assert run.shown["report"]["guideline_ids"] == ["G-11"]


def test_the_same_hand_over_without_its_figures_is_still_refused_because_the_records_meet_a_guideline():
    without_figures = {
        **PILOT_HAND_OVER, "summary": "Records and balances do not match.", "likely_cause": "no_clear_cause",
        "guideline_ids": ["G-11"],
    }  # fmt: skip

    run = on_the_services_guidelines([*INVESTIGATION, report(**without_figures), report(**PAUSE_UNDER_G08B)])

    told = [line for line in redraft_brief(run).splitlines() if line.startswith("- ")]
    assert told == [
        "- G-11 says no guideline fits, but the records of this case meet every need of a guideline that allows an "
        "action. Recommend what that guideline allows."
    ]
    assert run.result["draft_log"][0]["codes"] == ["guideline_need"]
    # It names neither the guideline that fits nor its action: the model has the list and what each one needs.
    assert "G-08b" not in told[0] and "pause_card" not in told[0]
    assert run.shown["report"]["recommendation"] == "pause_card"


def records_disagree(signal: dict) -> dict:
    """A hand-over for records that disagree, citing the case's reconciliation, in the same words for any case."""
    reconciliation = f"agg_{signal['customer_id']}_reconciliation"
    return {
        "likely_cause": "insufficient_data",
        "summary": "Records and balances do not match.",
        "evidence": [{"claim": "Balances and transactions do not match.", "evidence_ids": [reconciliation]}],
        "ruled_out": [],
        "other_findings": [],
        "review_reasons": [
            {"reason": reason, "finding": "Records and balances do not match.", "evidence_ids": [reconciliation]}
            for reason in signal["review_reasons"]
        ],
        "confidence": "medium",
        "recommendation": "needs_human",
        "guideline_ids": ["G-10b"],
        "next_action": None,
    }


def test_a_case_is_handed_over_for_records_that_disagree_only_when_the_reconciliation_says_so():
    reconciliation = ask_bank("Do the books agree?", topic="reconciliation")
    runs = {}
    for case, questions in (
        ("c_012", [ask_bank("Where did the money go?"), ask_bank("Is income steady?", topic="income")]),
        ("c_010", INVESTIGATION[:3]),
    ):
        signal = signal_of(case)
        draft = call("ReportDraft", **records_disagree(signal))
        runs[case] = on_the_services_guidelines([*questions, reconciliation, DONE, gaps(), draft, draft], signal=signal)

    assert runs["c_012"].result["check"] == {"passed": True, "problems": []}
    assert runs["c_012"].shown["report"]["guideline_ids"] == ["G-10b"]
    # c_010's books agree: its reconciliation is no record of a disagreement.
    assert [entry["codes"] for entry in runs["c_010"].result["draft_log"]] == [["guideline_need", "cause"]] * 2
    assert runs["c_010"].result["check"]["problems"][0] == (
        "G-10b needs a reconciliation that says balances and transactions do not match. "
        "The report cites no record that shows it."
    )


def test_a_short_list_of_payments_is_not_a_hole_in_the_books():
    short_list = {
        **records_disagree(signal_of("c_009")),
        "summary": "70% of the fall is not in the list.",
        "evidence": [{"claim": "70% of the fall is not in the list.", "evidence_ids": ["agg_c_009_payments"]}],
        "review_reasons": [
            {"reason": "large_cut", "finding": "70% of the fall is not in the list.", "evidence_ids": ["agg_c_009_payments"]}
        ],
    }  # fmt: skip
    draft = call("ReportDraft", **short_list)

    run = on_the_services_guidelines(
        [ask_bank("Where did the money go?"),
         ask_bank("Who is this payee?", topic="payee_history", record_id="txn_c_009_01"),
         ask_bank("Is income steady?", topic="income"), DONE, gaps(), draft, draft],
        signal=signal_of("c_009"),
    )  # fmt: skip

    # The figure is backed: agg_c_009_payments shows the part not listed. The books are not said to disagree.
    assert "p:70" in figures.allowed_figures([facts.ledger(run.state)["agg_c_009_payments"]])
    assert [entry["codes"] for entry in run.result["draft_log"]] == [["guideline_need", "cause"]] * 2
    assert run.result["draft_log"][-1]["result"] == "gave_up"


def test_with_the_strict_reading_of_unpaid_c_010_goes_to_a_person_because_nothing_fits(monkeypatch):
    monkeypatch.setattr(policy, "UNPAID_MEANS", ("no",))
    nothing_fits = {**PAUSE_UNDER_G08B, "recommendation": "needs_human", "guideline_ids": ["G-11"]}

    run = on_the_services_guidelines([*INVESTIGATION, report(**PAUSE_UNDER_G08B), report(**nothing_fits)])

    # rep_c_010_3 failed and is not recorded as paid since. Under this reading that is not "has not been paid".
    told = [line for line in redraft_brief(run).splitlines() if line.startswith("- ")]
    assert told == [
        "- G-08b needs a repayment that failed and has not been paid. The report cites no record that shows it."
    ]
    assert run.result["check"] == {"passed": True, "problems": []}
    assert run.shown["report"]["recommendation"] == "needs_human"
    assert run.shown["report"]["guideline_ids"] == ["G-11"]
