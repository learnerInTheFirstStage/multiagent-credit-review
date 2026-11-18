"""The report check on the guidelines a report cites and on its cause (spec 2.A rules 5 to 8, 2.D.4, 6.2, 6.3).

On the service's own guidelines unless a test says otherwise. Run uses the test signal of c_010: a large cut and
a failed repayment that its records carry as a pause under G-08b. The model proposes, and code refuses with the
reason: no refusal names the guideline that fits or its action.
"""

import copy

import pytest

from app import policy
from app.agents import mock
from tests.test_graph import (
    APPROVE,
    DONE,
    FROM_THE_FACTS,
    INVESTIGATION,
    PAUSE_UNDER_G08B,
    REPORT,
    Run,
    ask_bank,
    ask_card,
    broken,
    call,
    gaps,
    on_the_services_guidelines,
    report,
    signal_of,
)
from tests.test_signals import SIGNAL

INFLOWS = {"claim": "Weekly inflows are steady at about $80,000 over the last 13 weeks.", "evidence_ids": ["agg_c_010_inflows"]}
# Keeping the limit under G-01, with a record for each of its needs, past the failed repayment.
KEEP = {"recommendation": "keep_limit", "guideline_ids": ["G-01"], "other_findings": [*REPORT["other_findings"], INFLOWS]}
HAND_OVER = {"likely_cause": "no_clear_cause", "recommendation": "needs_human"}
NOT_COVERED = "A repayment failed (sig_repayments). No guideline the report cites covers it. Cite one whose needs include it."


def told(run: Run) -> list[str]:
    """What the second draft was told about the first: one problem per line."""
    assert run.result["drafts"] == 2
    return [line[2:] for line in run.seen[-1][-1].content.splitlines() if line.startswith("- ")]


def first_draft(draft: dict, **run_args) -> Run:
    """c_010 after transactions, repayments and income, with this first draft and a pause under G-08b after it."""
    return on_the_services_guidelines([*INVESTIGATION, report(**draft), report(**PAUSE_UNDER_G08B)], **run_args)


@pytest.mark.parametrize(
    ("draft", "sentence", "guidelines"),
    [
        pytest.param(
            {"recommendation": "pause_card", "guideline_ids": ["G-01", "G-08b"]},
            "It cites G-01, which does not allow pause_card. Cite only the guidelines the recommendation rests on.",
            None, id="G-01 next to G-08b",
        ),
        pytest.param(
            {"guideline_ids": ["G-07", "G-03"]},
            "It cites G-03, which does not allow reduce_limit. Cite only the guidelines the recommendation rests on.",
            "fixture", id="on guidelines that declare no needs too",
        ),
        pytest.param(
            {"recommendation": "request_documents", "guideline_ids": ["G-11", "G-07", "G-03"]},
            "It cites G-11, G-03, which do not allow request_documents. "
            "Cite only the guidelines the recommendation rests on.",
            "fixture", id="two of three",
        ),
    ],
)  # fmt: skip
def test_a_guideline_cited_next_to_one_that_does_not_allow_the_action_is_refused(draft, sentence, guidelines):
    if guidelines == "fixture":
        run = Run([*INVESTIGATION, report(**draft), report()])
    else:
        run = first_draft(draft)

    assert told(run) == [sentence]
    assert run.result["draft_log"][0]["codes"] == ["guideline_allows"]
    assert run.result["check"] == {"passed": True, "problems": []}


@pytest.mark.parametrize(
    ("cited", "codes"),
    [
        pytest.param([], ["hand_over"], id="none"),
        pytest.param(["G-01"], ["hand_over"], id="one that allows an action"),
        # G-11 is checked as well: the records carry an action.
        pytest.param(["G-11", "G-01"], ["hand_over", "guideline_need"], id="one that hands over, and one that does not"),
    ],
)
def test_a_hand_over_must_cite_a_guideline_that_allows_one_and_only_that(cited, codes):
    run = first_draft({**HAND_OVER, "guideline_ids": cited})

    [line] = told(run)
    assert line.startswith(
        "It hands the case to a person. Cite the guideline that says so, and only that: G-10b, G-11, G-12."
    )
    assert "G-01" not in line  # rule 6 told the draft to drop it, so its needs are not asked for
    assert run.result["draft_log"][0]["codes"] == codes


def test_a_failed_repayment_cannot_be_passed_over_by_citing_only_a_bank_side_guideline():
    run = first_draft(KEEP)

    assert told(run) == [NOT_COVERED]
    assert run.result["draft_log"][0]["codes"] == ["guideline_cover"]
    assert run.shown["report"]["recommendation"] == "pause_card"


def to_new_payees(cited: list[str]) -> dict:
    """c_008: documents asked for under G-09, citing these payments."""
    return {
        **copy.deepcopy(REPORT),
        "likely_cause": "unexplained_outflow",
        "summary": "Payments went to payees never paid before.",
        "evidence": [{"claim": "Payments went to payees never paid before.", "evidence_ids": cited}],
        "other_findings": [],
        "review_reasons": [{"reason": "large_cut", "finding": "The fall went to new payees.", "evidence_ids": cited}],
        "recommendation": "request_documents",
        "guideline_ids": ["G-09"],
    }


def test_citing_one_payment_of_three_does_not_meet_a_need_the_three_meet_together(monkeypatch):
    payments = ["txn_c_008_01", "txn_c_008_02", "txn_c_008_03"]
    # The three are c_008's whole fall, so each alone is a quarter of it or more: txn_c_008_02 is 32%. A line of 40%
    # keeps one payment short of the need, as a quarter did when the fall was more than the three. Read when used.
    monkeypatch.setattr(policy, "LARGE_PART_BP", 4000)

    run = on_the_services_guidelines(
        [ask_bank("Where did the money go?"), DONE, gaps(), call("ReportDraft", **to_new_payees(["txn_c_008_02"])),
         call("ReportDraft", **to_new_payees(payments))],
        signal=signal_of("c_008"),
    )  # fmt: skip

    # txn_c_008_02 alone is 32% of the fall. The three together are all of it.
    assert told(run) == [
        "G-09 needs payments to payees never paid before that together are a large part of the fall. "
        "The report cites no record that shows it. txn_c_008_01, txn_c_008_02, txn_c_008_03 would: cite them."
    ]
    assert run.result["check"] == {"passed": True, "problems": []}


def test_a_refusal_names_the_record_that_would_meet_the_need():
    cites_the_signal = [REPORT["review_reasons"][0], {**REPORT["review_reasons"][1], "evidence_ids": ["sig_repayments"]}]

    run = first_draft({**PAUSE_UNDER_G08B, "review_reasons": cites_the_signal})

    assert told(run) == [
        "G-08b needs a repayment that failed and has not been paid. The report cites no record that shows it. "
        "rep_c_010_3 would: cite it."
    ]
    assert run.result["draft_log"][0]["codes"] == ["guideline_need"]


# The limit kept with neither the payroll and tax payments nor the inflows cited.
CITES_NO_PAYMENT = {
    "recommendation": "keep_limit",
    "likely_cause": "no_clear_cause",
    "evidence": [{"claim": "Linked cash fell.", "evidence_ids": ["bal_c_010"]}],
    "review_reasons": [{"reason": "large_cut", "finding": "Cash fell.", "evidence_ids": ["bal_c_010"]},
                       REPORT["review_reasons"][1]],
}  # fmt: skip
G01_UNMET = (
    "G-01 needs payroll or tax payments that together make up most of the fall and weekly inflows that are steady. "
    "The report cites no record that shows them. txn_c_010_01, txn_c_010_02, agg_c_010_inflows would: cite them."
)


def test_every_unmet_need_of_a_cited_guideline_is_told_in_one_sentence():
    run = first_draft({**CITES_NO_PAYMENT, "guideline_ids": ["G-01"]})

    assert told(run) == [f"{G01_UNMET} {NOT_COVERED}"]
    assert run.result["draft_log"][0]["codes"] == ["guideline_need", "guideline_cover"]


@pytest.mark.parametrize("cited", [["G-04", "G-01"], ["G-01", "G-04"]])
def test_the_guidelines_a_draft_cites_are_told_in_the_order_it_cites_them(cited):
    g04_unmet = (
        "G-04 needs a transfer to the customer's own account elsewhere that makes up most of the fall and weekly "
        "inflows that are steady and every repayment paid on time. The report cites no record that shows them. "
        "agg_c_010_inflows would: cite it."
    )
    unmet = {"G-01": G01_UNMET, "G-04": g04_unmet}

    run = first_draft({**CITES_NO_PAYMENT, "guideline_ids": cited})

    assert told(run) == [" ".join([*(unmet[guideline] for guideline in cited), NOT_COVERED])]
    assert run.result["draft_log"][0]["codes"] == ["guideline_need", "guideline_need", "guideline_cover"]


def test_a_record_is_named_only_when_citing_it_would_meet_the_need():
    # c_002's one payroll or tax payment is 28% of the fall, under G-01's line: citing it cannot meet the need.
    signal = signal_of("c_002")
    keep = {
        **CITES_NO_PAYMENT, "guideline_ids": ["G-01"], "summary": "Cash fell.", "other_findings": [],
        "evidence": [{"claim": "Linked cash fell.", "evidence_ids": ["sig_cash"]}],
        "review_reasons": [{"reason": reason, "finding": "Cash fell.", "evidence_ids": ["sig_cash"]}
                           for reason in signal["review_reasons"]],
    }  # fmt: skip

    run = on_the_services_guidelines(
        [ask_bank("Where did the money go?"), ask_bank("Is income steady?", topic="income"), DONE, gaps(),
         report(**keep), report(**keep)],
        signal=signal,
    )  # fmt: skip

    assert "txn_c_002_02" in {record["id"] for record in run.result["answers"][0]["response"]["evidence"]}
    assert told(run) == [
        "G-01 needs payroll or tax payments that together make up most of the fall and weekly inflows that are steady. "
        "The report cites no record that shows them. agg_c_002_inflows would: cite it."
    ]


def test_a_guideline_the_draft_is_told_to_drop_covers_nothing():
    # G-08b allows only a pause. Once it is dropped, nothing the draft cites covers the failed repayment: told at once.
    run = first_draft({**KEEP, "guideline_ids": ["G-01", "G-08b"]})

    assert told(run) == [
        f"It cites G-08b, which does not allow keep_limit. Cite only the guidelines the recommendation rests on. "
        f"{NOT_COVERED}"
    ]
    assert run.result["draft_log"][0]["codes"] == ["guideline_allows", "guideline_cover"]


def test_a_report_gets_at_most_one_guideline_line_per_draft_and_it_holds_every_guideline_defect():
    run = first_draft({**KEEP, "other_findings": REPORT["other_findings"]})

    # Told of both at once: with one redraft, the second told alone would be a give-up.
    assert told(run) == [
        "G-01 needs weekly inflows that are steady. The report cites no record that shows it. "
        f"agg_c_010_inflows would: cite it. {NOT_COVERED}"
    ]
    assert run.result["draft_log"][0]["codes"] == ["guideline_need", "guideline_cover"]


def test_each_trigger_a_draft_passes_over_is_told_in_its_own_sentence():
    # The test signal with its one account behind: a failed repayment and an account behind, two triggers.
    behind = copy.deepcopy(SIGNAL)
    behind["snapshot"]["bank"]["accounts"] = [{"id": "acct_c010_a", "last_synced": "2026-11-01"}]

    run = on_the_services_guidelines(
        [*INVESTIGATION[:3], ask_bank("Which accounts report?", topic="accounts"), DONE, gaps(), report(**KEEP),
         report(**KEEP)],
        signal=behind,
    )  # fmt: skip

    assert told(run) == [
        f"{NOT_COVERED} A linked account is behind (sig_accounts). No guideline the report cites covers it. "
        "Cite one whose needs include it."
    ]
    assert run.result["draft_log"][0]["codes"] == ["guideline_cover", "guideline_cover"]


@pytest.mark.parametrize(
    "draft",
    [
        pytest.param({**HAND_OVER, "guideline_ids": ["G-11"]}, id="nothing fits, while something does"),
        pytest.param({**HAND_OVER, "guideline_ids": []}, id="a hand-over under no guideline"),
        pytest.param({**HAND_OVER, "likely_cause": "insufficient_data", "guideline_ids": ["G-10b"]},
                     id="records that disagree, without the record"),
        pytest.param(KEEP, id="the failed repayment passed over"),
        pytest.param({**KEEP, "other_findings": []}, id="the failed repayment passed over, and a need unmet"),
        pytest.param({"recommendation": "request_documents", "guideline_ids": ["G-14"]}, id="a guideline that fails"),
    ],
)  # fmt: skip
def test_no_refusal_names_a_guideline_that_fits_or_its_action(draft):
    run = on_the_services_guidelines([*INVESTIGATION, report(**draft), report(**draft)])

    said = [*told(run), *run.result["check"]["problems"], run.shown["report"]["summary"]]
    assert run.result["draft_log"][-1]["result"] == "gave_up"
    for text in said:
        assert "G-08b" not in text and "pause_card" not in text


def test_no_refusal_names_a_guideline_that_fits_or_its_action_on_another_case():
    hand_over = {**to_new_payees(["txn_c_008_01"]), **HAND_OVER, "guideline_ids": ["G-11"]}

    run = on_the_services_guidelines(
        [ask_bank("Where did the money go?"), DONE, gaps(), call("ReportDraft", **hand_over),
         call("ReportDraft", **hand_over)],
        signal=signal_of("c_008"),
    )  # fmt: skip

    assert told(run) == [
        "G-11 says no guideline fits, but the records of this case meet every need of a guideline that allows an "
        "action. Recommend what that guideline allows."
    ]
    for text in (*told(run), run.shown["report"]["summary"]):
        assert "G-09" not in text and "request_documents" not in text


def test_nothing_fits_is_judged_on_every_record_of_the_case_not_only_on_those_the_report_cites():
    hand_over = {**FROM_THE_FACTS, "guideline_ids": ["G-11"]}

    run = first_draft(hand_over)

    # It cites the case's own facts only. The records that meet G-08b came back from the card side and are not cited.
    assert {i for item in hand_over["review_reasons"] + hand_over["evidence"] for i in item["evidence_ids"]} == {
        "sig_cash", "sig_limit", "sig_repayments"
    }  # fmt: skip
    assert told(run) == [
        "G-11 says no guideline fits, but the records of this case meet every need of a guideline that allows an "
        "action. Recommend what that guideline allows."
    ]
    assert run.result["draft_log"][0]["codes"] == ["guideline_need"]


@pytest.mark.parametrize(
    "draft",
    [
        pytest.param(report(likely_cause="no_clear_cause", recommendation="needs_human", guideline_ids=[], evidence=[]),
                     id="a hand-over under no guideline"),
        pytest.param(report(likely_cause="no_clear_cause", recommendation="needs_human", guideline_ids=["G-03"]),
                     id="a hand-over under a guideline that allows an action"),
        pytest.param(report(recommendation="keep_limit", guideline_ids=["G-03"]),
                     id="the limit kept past a failed repayment"),
    ],
)  # fmt: skip
def test_guidelines_that_declare_nothing_are_checked_as_before(draft):
    run = Run([*INVESTIGATION, draft])

    assert run.result["check"] == {"passed": True, "problems": []}
    assert run.result["drafts"] == 1


# Each reading of the policy, and a first draft that only that reading refuses.
REFUSED_BY = {
    "every_cited_guideline_allows": (
        {**PAUSE_UNDER_G08B, "guideline_ids": ["G-08b", "G-01"], "other_findings": KEEP["other_findings"]},
        "guideline_allows",
    ),
    "hand_over_cites_a_guideline": ({**HAND_OVER, "guideline_ids": []}, "hand_over"),
    "none_fits_is_checked": ({**HAND_OVER, "guideline_ids": ["G-11"]}, "guideline_need"),
    "must_address": (KEEP, "guideline_cover"),
}


@pytest.mark.parametrize("switch", sorted(policy.RULES))
def test_each_reading_can_be_switched_off_on_its_own(monkeypatch, switch):
    assert set(REFUSED_BY) == set(policy.RULES)
    monkeypatch.setitem(policy.RULES, switch, False)

    for reading, (draft, code) in REFUSED_BY.items():
        run = first_draft(draft)

        refused = run.result["draft_log"][0]
        if reading == switch:
            assert (refused["result"], refused["codes"]) == ("passed", []), reading
        else:
            assert (refused["result"], refused["codes"]) == ("refused", [code]), reading


@pytest.mark.parametrize(
    ("changes", "sentence"),
    [
        pytest.param(
            {"likely_cause": "revenue_loss", "other_findings": [INFLOWS]},
            "It gives revenue_loss as the likely cause, but cites no record that shows it. "
            "One of these would: weekly inflows that are down.",
            id="steady inflows do not show revenue loss",
        ),
        pytest.param(
            {"likely_cause": "funds_moved_out_of_view"},
            "It gives funds_moved_out_of_view as the likely cause, but cites no record that shows it. One of these "
            "would: a transfer to the customer's own account elsewhere; a change of the account that pays the card.",
            id="any one of two records would do",
        ),
        pytest.param(
            {"evidence": [{"claim": "Linked cash fell.", "evidence_ids": ["bal_c_010"]}],
             "review_reasons": [{**REPORT["review_reasons"][0], "evidence_ids": ["bal_c_010"]},
                                REPORT["review_reasons"][1]]},
            "It gives payroll_or_tax as the likely cause, but cites no record that shows it. "
            "One of these would: a payroll or tax payment.",
            id="the record is in the case but not cited",
        ),
    ],
)  # fmt: skip
def test_a_cause_with_no_record_behind_it_is_refused(changes, sentence):
    # On guidelines that declare no needs: the cause rule does not rest on them.
    run = Run([*INVESTIGATION, report(**changes), report()])

    assert told(run) == [sentence]
    assert run.result["draft_log"][0]["codes"] == ["cause"]


def test_the_cause_is_told_after_the_guideline_and_before_the_figures():
    draft = {"likely_cause": "revenue_loss", "guideline_ids": ["G-07", "G-03"], "summary": "About $310,000 went out."}

    run = Run([*INVESTIGATION, report(**draft), report()])

    assert run.result["draft_log"][0]["codes"] == ["guideline_allows", "cause", "figure"]
    assert [line.split(" ")[1] for line in told(run)] == ["cites", "gives", "states"]


GAVE_NOTHING = report(
    likely_cause="insufficient_data", recommendation="needs_human", guideline_ids=[], evidence=[], other_findings=[],
    review_reasons=[
        {"reason": "large_cut", "finding": "The bank side gave no answer.", "evidence_ids": []},
        {"reason": "failed_repayment", "finding": "The latest repayment failed.", "evidence_ids": ["sig_repayments"]},
    ],
)  # fmt: skip


def fails_on_income(request):
    """The bank side, with no answer on the income topic only."""
    if request.topic == "income":
        raise TimeoutError("the Bank Service did not answer")
    return mock.cash_flow(request)


@pytest.mark.parametrize(
    ("questions", "cash_flow", "credit_behavior", "accepted"),
    [
        pytest.param([ask_bank("Where did the money go?"), ask_card("Did any repayment fail?")], broken, None, True,
                     id="the bank side gave nothing"),
        pytest.param([ask_card("Did any repayment fail?"), ask_card("How is the card used?", topic="card_use")],
                     None, broken, True, id="the card side gave nothing, asked twice"),
        pytest.param([ask_bank("Where did the money go?"), ask_bank("Is income steady?", topic="income")],
                     fails_on_income, None, False, id="a side that answered once"),
        pytest.param([ask_bank("Where did the money go?"), ask_card("Did any repayment fail?", topic="bills")],
                     None, None, False, id="the only call to the card side was refused"),
        pytest.param([call("AskTheAuditor", question="Is this fraud?"), ask_bank("Where did the money go?")],
                     None, None, False, id="a tool that does not exist"),
        pytest.param([ask_card("Did any repayment fail?", topic="bills"), ask_card("Did any repayment fail?")],
                     None, broken, True, id="a refused call, then the same side gave nothing"),
    ],
)  # fmt: skip
def test_insufficient_data_is_accepted_when_a_specialist_gave_nothing(questions, cash_flow, credit_behavior, accepted):
    specialists = {"cash_flow": cash_flow or mock.cash_flow, "credit_behavior": credit_behavior or mock.credit_behavior}

    run = Run([*questions, DONE, gaps(), GAVE_NOTHING, GAVE_NOTHING], specialists=specialists)

    assert run.result["draft_log"][0]["codes"] == ([] if accepted else ["cause"])


@pytest.mark.parametrize(("cause", "codes"), [("insufficient_data", []), ("payroll_or_tax", ["cause"])])
def test_a_side_that_gave_nothing_excuses_insufficient_data_and_no_other_cause(cause, codes):
    cites_cash_only = report(
        likely_cause=cause,
        evidence=[{"claim": "Linked cash fell.", "evidence_ids": ["bal_c_010"]}],
        other_findings=[],
        review_reasons=[
            {"reason": "large_cut", "finding": "Cash fell.", "evidence_ids": ["bal_c_010"]},
            {"reason": "failed_repayment", "finding": "The latest repayment failed.", "evidence_ids": ["sig_repayments"]},
        ],
    )  # fmt: skip

    run = Run(
        [*INVESTIGATION, cites_cash_only, cites_cash_only],
        specialists={"cash_flow": mock.cash_flow, "credit_behavior": broken},
    )

    assert run.result["asked"][1]["outcome"] == "no_answer"
    assert run.result["draft_log"][0]["codes"] == codes


def test_a_report_that_passes_with_low_confidence_is_turned_into_a_hand_over_and_logged_as_one():
    run = first_draft({**PAUSE_UNDER_G08B, "confidence": "low"})

    [logged] = run.result["draft_log"]
    assert (logged["result"], logged["recommendation"], logged["codes"]) == ("passed_low_confidence", "pause_card", [])
    assert run.shown["report"]["recommendation"] == "needs_human"

    result = run.decide(APPROVE)

    assert result["status"] == "done"
    assert run.sent == []
