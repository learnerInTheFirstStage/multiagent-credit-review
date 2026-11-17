"""The 90K incident (spec 9.1).

In the pilot a report on c_010 said that weekly inflows of about $80,000 would make true outflows
about $310,000, so roughly $90,000 was not accounted for. The model had worked both figures out
itself, no record held them, and the analyst read them as findings. These tests are the part the
report check can stop on the figures alone, on the test guidelines. What stops the incident where
its figure happens to be a record value is the guideline, cause and gap rules, tested apart.
"""

import sys
from pathlib import Path

from app.agents import mock
from app.graph import facts, figures
from contracts import Finding
# Run uses the test signal of c_010, whose repayments are $41,000, $46,000 and $50,000.
from tests.test_graph import DONE, INVESTIGATION, REPORT, Run, ask_card, gaps, report

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "eval"))
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
    for first_draft in (report(**PILOT_REPORT), report(**HAND_OVER)):
        run = Run([*INVESTIGATION, first_draft, report()])

        assert run.result["drafts"] == 2
        assert figure_line(run) == (
            f"- {FIGURES_REFUSED}summary: $310,000, $90,000. "
            "State only figures the cited evidence shows, exactly as shown, or leave the figure out."
        )
        assert [entry["codes"] for entry in run.result["draft_log"]] == [["figure"], []]
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
