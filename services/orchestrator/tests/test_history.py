"""The review store: what a closed case leaves behind, what a later case reads back, and the seeds."""

import json
import logging
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import get_args

import pytest
import yaml
from sqlalchemy import create_engine, select, text

from app import history
from app.db import Base, SessionLocal, engine
from app.db.models import Case, Review
from app.graph import facts
from app.graph.report import LikelyCause, Recommendation
from tests.test_signals import SIGNAL  # c_010, as of 2026-11-10

ORCHESTRATOR = Path(__file__).resolve().parents[1]
SEED_FILE = ORCHESTRATOR.parents[1] / "eval" / "history.yaml"
CLOCK = date(2026, 11, 10)

RECORDS = {
    "sig_cash": {"id": "sig_cash", "kind": "cash_change", "data": {"fall_cents": 23000000}},
    "txn_c_010_01": {"id": "txn_c_010_01", "kind": "bank_transaction", "data": {"category": "payroll"}},
    "rep_c_010_3": {"id": "rep_c_010_3", "kind": "card_repayment", "data": {"status": "failed"}},
    "agg_c_010_inflows": {"id": "agg_c_010_inflows", "kind": "weekly_inflow_series", "data": {"weeks": 13}},
    "acct_c010_a": {"id": "acct_c010_a", "kind": "linked_account", "data": {"is_stale": False}},
}
REPORT = {
    "case_id": "case_a",
    "customer_id": "c_010",
    "likely_cause": "payroll_or_tax",
    "summary": "Every report says something like this.",
    "evidence": [{"claim": "A payroll run went out in the week.", "evidence_ids": ["txn_c_010_01", "sig_cash"]}],
    "ruled_out": [{"cause": "revenue_loss", "reason": "Inflows are steady.", "evidence_ids": ["agg_c_010_inflows"]}],
    "other_findings": [],
    "review_reasons": [
        {"reason": "large_cut", "finding": "Payroll explains it.", "evidence_ids": ["txn_c_010_01"]},
        {"reason": "failed_repayment", "finding": "The latest repayment failed.", "evidence_ids": ["rep_c_010_3"]},
    ],
    "confidence": "high",
    "recommendation": "pause_card",
    "guideline_ids": ["G-08b"],
    "next_action": None,
    "engine_limit_cents": 5800000,
}
# A report whose two drafts failed: code wrote it, and it was never checked.
GIVE_UP = {
    **REPORT,
    "likely_cause": "no_clear_cause",
    "summary": "The report could not be completed. It cites no guideline.",
    "evidence": [],
    "ruled_out": [],
    "review_reasons": [],
    "confidence": "low",
    "recommendation": "needs_human",
    "guideline_ids": [],
}


@pytest.fixture(autouse=True)
def tables():
    # A review left behind would give a later test's customer an earlier review.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def closed(status: str = "done", *, as_of: str = "2026-11-10", **changes) -> dict:
    """The graph state of a case that has closed."""
    return {
        "case_id": "case_a",
        "signal": {**SIGNAL, "as_of": as_of},
        "status": status,
        "report": REPORT,
        "check": {"passed": True, "problems": []},
        "decisions": [
            {"step": "analyst", "decision": "approve", "by": "u_analyst"},
            {"step": "manager", "decision": "approve", "by": "u_manager"},
        ],
        "action": {"type": "pause_card", "result": {"status": "applied"}},
        "asked": [{"tool": "AskCashFlow", "agent": "cash_flow", "topic": "transactions", "outcome": "answered"}],
        "policy_version": "2026-11-16",
        **changes,
    }


def keep(state: dict, *, case_id: str = "case_a", customer_id: str = "c_010", status: str | None = None,
         ledger=lambda state: RECORDS) -> None:
    """Close a case as the call sites do: lock the row, write the status, record, commit.

    status is what the table gets, when it is not what the graph says.
    """
    with SessionLocal() as session:
        if session.get(Case, case_id) is None:
            session.add(Case(case_id=case_id, customer_id=customer_id, status="investigating"))
            session.commit()
        case = session.scalar(select(Case).where(Case.case_id == case_id).with_for_update())
        case.status = status or state["status"]
        history.record(session, case, state, ledger=ledger)
        session.commit()


def reviews() -> list[Review]:
    with SessionLocal() as session:
        return list(session.scalars(select(Review).order_by(Review.case_id)))


def status_of(case_id: str) -> str:
    with SessionLocal() as session:
        return session.get(Case, case_id).status


def everything_stored() -> str:
    """Every row of every table of the service, as one string."""
    with engine.connect() as connection:
        return json.dumps(
            [
                [str(value) for value in found]
                for table in Base.metadata.sorted_tables
                for found in connection.execute(table.select())
            ]
        )


def entry(case_id: str = "seed_x", customer_id: str = "c_900", **changes) -> dict:
    """One flat seed entry, as in eval/history.yaml."""
    return {
        "case_id": case_id,
        "customer_id": customer_id,
        "as_of": "2025-11-10",
        "outcome": "done",
        "current_limit_cents": 13000000,
        "engine_limit_cents": 8000000,
        "cash_7d_ago_cents": 68000000,
        "cash_now_cents": 40000000,
        "review_reasons": ["large_cut"],
        "likely_cause": "seasonal_dip",
        "recommendation": "keep_limit",
        "confidence": "medium",
        "guideline_ids": ["G-03"],
        "claims": ["The yearly stocking payment went out."],
        "decisions": [{"step": "analyst", "decision": "approve", "by": "u_seed_analyst", "note": "Keep."}],
        **changes,
    }


def seed(entries: list[dict], **options) -> dict:
    with SessionLocal() as session:
        counts = history.seed(session, entries, expect_clean=options.pop("expect_clean", False), **options)
        session.commit()
        return counts


def seed_file(folder: Path, name: str, entries: list[dict]) -> Path:
    path = folder / name
    path.write_text(yaml.safe_dump({"schema_version": 1, "reviews": entries}))
    return path


# Writing at closing


def test_a_run_that_broke_leaves_no_review_and_its_unchecked_draft_is_stored_nowhere():
    # run_case marks the table failed when the graph raised. The graph still says investigating.
    draft = {**REPORT, "summary": "An unchecked draft that says $999,999 went missing."}
    keep(closed("investigating", report=draft, check=None), status="failed")

    assert reviews() == []
    assert status_of("case_a") == "failed"
    assert "999,999" not in everything_stored()


def test_a_closed_table_status_the_graph_does_not_share_leaves_no_review():
    keep(closed("needs_approval"), status="done")

    assert reviews() == []


@pytest.mark.parametrize("status", ["queued", "investigating", "needs_approval", "needs_manager"])
def test_a_case_that_is_still_open_leaves_no_review(status):
    keep(closed(status))

    assert reviews() == []


def test_a_closed_case_with_no_signal_or_no_check_leaves_no_review(caplog):
    with caplog.at_level(logging.ERROR, logger="app.history"):
        keep(closed(signal=None))
        keep(closed(check=None), case_id="case_b")

    assert reviews() == []
    # Passed over, not failed: that error is the operator's one sign of a review that was lost.
    assert "could not be kept" not in caplog.text


@pytest.mark.parametrize("status", ["done", "rejected", "failed"])
def test_each_way_a_case_closes_leaves_one_review(status):
    keep(closed(status))

    (review,) = reviews()
    assert (review.case_id, review.outcome, review.source, review.check_passed) == ("case_a", status, "case", True)


def test_recording_a_closed_case_twice_keeps_one_review(caplog):
    with caplog.at_level(logging.ERROR, logger="app.history"):
        keep(closed())
        keep(closed(report={**REPORT, "likely_cause": "revenue_loss"}))  # a second call, from another call site
        with SessionLocal() as session:
            case = session.get(Case, "case_a")
            history.record(session, case, closed(), ledger=lambda state: RECORDS)
            history.record(session, case, closed(), ledger=lambda state: RECORDS)  # twice in one transaction too
            session.commit()

    (review,) = reviews()
    assert review.likely_cause == "payroll_or_tax"  # the first one is kept
    assert "could not be kept" not in caplog.text  # the second is passed over, not a failed insert


def test_recording_and_seeding_commit_nothing_the_caller_does():
    # The review belongs to the transaction that writes the status: the caller commits both, or neither.
    # The status is written first, as at every call site. (Before any write, pysqlite sends no BEGIN, so on
    # SQLite the savepoint would be the outermost transaction and its release a commit. Postgres has no such case.)
    with SessionLocal() as session:
        session.add(Case(case_id="case_a", customer_id="c_010", status="investigating"))
        session.commit()
        case = session.get(Case, "case_a")
        case.status = "done"
        history.record(session, case, closed(), ledger=lambda state: RECORDS)
        history.seed(session, [entry()], expect_clean=False)
        session.rollback()

    assert reviews() == []
    assert status_of("case_a") == "investigating"


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param({"signal": {**SIGNAL, "as_of": "not a date"}}, id="the row cannot be built"),
        pytest.param({"signal": {key: value for key, value in SIGNAL.items() if key != "current_limit_cents"}},
                     id="the insert fails"),
    ],
)
def test_a_review_that_cannot_be_built_does_not_undo_the_status_written_with_it(broken, caplog):
    with caplog.at_level(logging.ERROR, logger="app.history"):
        keep(closed(**broken))

    assert status_of("case_a") == "done"
    assert reviews() == []
    assert "its review could not be kept" in caplog.text


def test_a_review_that_failed_is_written_when_it_is_recorded_again():
    keep(closed(signal={key: value for key, value in SIGNAL.items() if key != "current_limit_cents"}))
    keep(closed())  # the repair at the next start

    assert [review.case_id for review in reviews()] == ["case_a"]


def test_a_ledger_that_cannot_be_read_does_not_undo_the_status_either(caplog):
    def broken_ledger(state):
        raise KeyError("answers")

    with caplog.at_level(logging.ERROR, logger="app.history"):
        keep(closed(), ledger=broken_ledger)

    assert status_of("case_a") == "done"
    assert reviews() == []
    assert "its review could not be kept" in caplog.text


def test_a_missing_reviews_table_does_not_undo_the_status_either():
    Review.__table__.drop(engine)

    keep(closed())

    assert status_of("case_a") == "done"


def test_a_review_never_holds_the_approvers_token_or_the_investigators_messages():
    state = closed(
        messages=[{"role": "human", "content": "MESSAGE-MARKER the investigator's brief"}],
        approver_token="TOKEN-MARKER",
        draft_error="DRAFT-MARKER",
        gaps=["GAP-MARKER"],
        answers=[{"agent": "cash_flow", "question": "QUESTION-MARKER", "response": {}}],
    )

    keep(state)

    stored = everything_stored()
    assert reviews()
    for marker in ("MESSAGE-MARKER", "TOKEN-MARKER", "DRAFT-MARKER", "GAP-MARKER", "QUESTION-MARKER"):
        assert marker not in stored


def test_a_review_copies_the_records_its_report_cites():
    # One cited id is not in the ledger: it is skipped, not invented.
    report = {**REPORT, "other_findings": [{"claim": "Something else.", "evidence_ids": ["nowhere_1"]}]}

    keep(closed(report=report))

    (review,) = reviews()
    # Each cited record once, in the order the report first cites it. The uncited account is not copied.
    assert review.evidence == [RECORDS[key] for key in ("txn_c_010_01", "sig_cash", "agg_c_010_inflows", "rep_c_010_3")]


def test_a_review_copies_the_cross_side_match_when_the_report_cites_it():
    # As for c_106: the match is in neither the signal facts nor any answer. Only the ledger has it.
    match = {
        "id": "xs_account_match",
        "kind": "account_match",
        "data": {
            "account": "acct_c106_x",
            "listed_transfers": 1,
            "total_cents": 40000000,
            "transaction_ids": ["txn_c_106_01"],
        },
    }
    finding = {"claim": "The card is now paid from that account.", "evidence_ids": ["xs_account_match"]}
    report = {**REPORT, "other_findings": [finding]}

    keep(closed(report=report), ledger=lambda state: {**RECORDS, "xs_account_match": match})

    (review,) = reviews()
    assert match in review.evidence


def test_a_review_keeps_the_facts_a_later_case_reads():
    state = closed(
        decisions=[
            {"step": "analyst", "decision": "approve", "by": "u_analyst"},
            {"step": "analyst", "decision": "approve", "by": "u_analyst", "decided": "request_documents"},
        ]
    )

    keep(state)

    (review,) = reviews()
    assert review.as_of == date(2026, 11, 10)
    assert (review.likely_cause, review.recommendation, review.confidence) == ("payroll_or_tax", "pause_card", "high")
    assert (review.guideline_ids, review.review_reasons) == (["G-08b"], ["large_cut", "failed_repayment"])
    assert (review.current_limit_cents, review.engine_limit_cents) == (10000000, 5800000)
    assert (review.cash_7d_ago_cents, review.cash_now_cents) == (52000000, 29000000)
    assert review.human_decision == "request_documents"
    assert review.policy_version == "2026-11-16"
    assert review.report == REPORT
    assert review.action == state["action"]
    assert review.asked == state["asked"]
    assert review.embedder is None and review.embedding is None  # the close path never embeds


def test_the_review_text_is_the_claims_and_the_notes_and_never_the_summary():
    decisions = [
        {"step": "analyst", "decision": "approve", "by": "u_analyst", "note": "Payroll week.\x00 Keep an eye on it."},
        {"step": "manager", "decision": "approve", "by": "u_manager"},
    ]

    keep(closed(decisions=decisions))

    (review,) = reviews()
    assert review.text == "A payroll run went out in the week. Payroll week. Keep an eye on it."
    assert "Every report says" not in review.text


def test_the_review_text_is_cut_to_two_thousand_characters():
    report = {**REPORT, "evidence": [{"claim": "word " * 1000, "evidence_ids": ["sig_cash"]}]}

    keep(closed(report=report))

    assert len(reviews()[0].text) == 2000


def test_every_label_fits_its_column():
    # SQLite does not enforce String lengths. Postgres refuses the insert, and the review is lost.
    columns = Review.__table__.columns
    assert max(map(len, get_args(LikelyCause))) <= columns["likely_cause"].type.length
    assert max(map(len, get_args(Recommendation))) <= columns["recommendation"].type.length
    assert max(map(len, get_args(Recommendation))) <= columns["human_decision"].type.length
    assert max(map(len, ("high", "medium", "low"))) <= columns["confidence"].type.length
    assert max(map(len, history.CLOSED)) <= columns["outcome"].type.length
    assert max(map(len, ("case", "seed"))) <= columns["source"].type.length
    assert len("fastembed:BAAI/bge-small-en-v1.5") <= columns["embedder"].type.length
    # Every seed in the repo, the evaluation's too once its file is there.
    for found in history.read_seeds(sorted(SEED_FILE.parent.glob("history*.yaml"))):
        assert len(found["case_id"]) <= Case.__table__.columns["case_id"].type.length, found["case_id"]
        for key in ("customer_id", "outcome", "likely_cause", "recommendation", "confidence"):
            assert len(found[key]) <= columns[key].type.length, (found["case_id"], key)


# Reading at opening


def test_a_report_the_service_gave_up_on_is_kept_as_not_passed_and_is_never_an_earlier_review():
    check = {"passed": False, "problems": ["It cites no guideline."]}
    give_up = closed(report=GIVE_UP, check=check, as_of="2026-10-01")

    keep(give_up)

    (review,) = reviews()
    assert review.check_passed is False
    assert history.earlier_reviews("c_010", "2026-11-10") == []


def test_only_reviews_dated_strictly_before_the_signal_count_as_earlier():
    keep(closed(as_of="2026-11-09"), case_id="case_day_before")
    keep(closed(as_of="2026-11-10"), case_id="case_same_day")
    keep(closed(as_of="2026-11-11"), case_id="case_day_after")

    assert [review["case_id"] for review in history.earlier_reviews("c_010", "2026-11-10")] == ["case_day_before"]


def test_another_customers_review_is_never_among_a_customers_earlier_reviews():
    keep(closed(as_of="2026-10-01"), case_id="case_mine")
    keep(closed(as_of="2026-10-01"), case_id="case_theirs", customer_id="c_011")

    assert [review["case_id"] for review in history.earlier_reviews("c_010", "2026-11-10")] == ["case_mine"]
    assert [review["case_id"] for review in history.earlier_reviews("c_011", "2026-11-10")] == ["case_theirs"]
    assert history.earlier_reviews("c_012", "2026-11-10") == []


def test_a_rejected_report_is_an_earlier_review_that_was_not_approved():
    keep(closed("rejected", as_of="2026-10-01"), case_id="case_rejected")
    keep(closed("failed", as_of="2026-09-01"), case_id="case_failed")
    keep(closed("done", as_of="2026-08-01"), case_id="case_done")

    found = {review["case_id"]: review["approved"] for review in history.earlier_reviews("c_010", "2026-11-10")}
    assert found == {"case_rejected": False, "case_failed": False, "case_done": True}


def test_earlier_reviews_are_the_ten_newest_with_typed_facts_and_no_free_text():
    for month in range(1, 13):
        keep(closed(as_of=f"2025-{month:02d}-15"), case_id=f"case_{month:02d}")

    found = history.earlier_reviews("c_010", "2026-11-10")

    assert [review["case_id"] for review in found] == [f"case_{month:02d}" for month in range(12, 2, -1)]
    assert found[0] == {
        "case_id": "case_12",
        "as_of": "2025-12-15",
        "outcome": "done",
        "approved": True,
        "likely_cause": "payroll_or_tax",
        "recommendation": "pause_card",
        "guideline_ids": ["G-08b"],
        "review_reasons": ["large_cut", "failed_repayment"],
        "human_decision": None,
        "cash_7d_ago_cents": 52000000,
        "cash_now_cents": 29000000,
    }


def test_the_history_summary_counts_every_earlier_review_and_only_the_newest_get_a_record_of_their_own():
    # Twelve reviews in the last six months. The two oldest asked for documents because income was down.
    for days in range(1, 13):
        report = {**REPORT, "likely_cause": "payroll_or_tax", "recommendation": "keep_limit", "guideline_ids": ["G-01"]}
        if days > 10:
            report |= {"likely_cause": "revenue_loss", "recommendation": "request_documents", "guideline_ids": ["G-06"]}
        as_of = (CLOCK - timedelta(days=10 * days)).isoformat()
        keep(closed(as_of=as_of, report=report), case_id=f"case_{days:02d}")

    records = facts.history_facts(history.earlier_reviews("c_010", "2026-11-10", limit=None), SIGNAL)

    assert records[0] == {
        "id": "hist_c_010_summary",
        "kind": "review_history",
        "data": {
            "earlier_reviews": 12,
            "approved_reviews": 12,
            "window_days": 183,
            "reviews_in_window": 12,
            "document_requests_in_window": 2,
            "document_requests_for_income_in_window": 2,
            "last_review_as_of": "2026-10-31",
        },
    }
    assert [record["id"] for record in records[1:]] == ["hist_case_01", "hist_case_02", "hist_case_03"]
    # The rows a case reads for itself stay the ten newest.
    assert len(history.earlier_reviews("c_010", "2026-11-10")) == history.EARLIER_MAX == 10


def test_two_earlier_reviews_on_one_day_come_in_a_fixed_order():
    keep(closed(as_of="2026-10-01"), case_id="case_a")
    keep(closed(as_of="2026-10-01"), case_id="case_b")

    assert [review["case_id"] for review in history.earlier_reviews("c_010", "2026-11-10")] == ["case_b", "case_a"]


def test_a_missing_reviews_table_is_an_error_and_not_a_first_review():
    Review.__table__.drop(engine)

    with pytest.raises(Exception):
        history.earlier_reviews("c_010", "2026-11-10")


# Seeding


def test_seeding_twice_changes_nothing_and_every_seed_is_a_closed_case():
    entries = [entry("seed_a"), entry("seed_b", outcome="rejected"), entry("seed_c", customer_id="c_901")]

    assert seed(entries) == {"seeded": 3, "already_there": 0}
    first = everything_stored()
    assert seed(entries) == {"seeded": 0, "already_there": 3}

    assert everything_stored() == first
    with SessionLocal() as session:
        cases = {case.case_id: case.status for case in session.scalars(select(Case))}
    assert cases == {"seed_a": "done", "seed_b": "rejected", "seed_c": "done"}
    assert {(review.case_id, review.source, review.check_passed) for review in reviews()} == {
        ("seed_a", "seed", True), ("seed_b", "seed", True), ("seed_c", "seed", True),
    }


def test_a_seed_is_stored_as_the_review_its_entry_describes():
    decision = {"step": "analyst", "decision": "approve", "by": "u_seed_analyst", "note": "Keep."}
    decision["decided"] = "keep_limit"
    seed([entry(decisions=[decision])])

    (review,) = reviews()
    assert (review.customer_id, review.as_of, review.outcome) == ("c_900", date(2025, 11, 10), "done")
    assert (review.current_limit_cents, review.engine_limit_cents) == (13000000, 8000000)
    assert (review.cash_7d_ago_cents, review.cash_now_cents) == (68000000, 40000000)
    assert (review.likely_cause, review.recommendation, review.confidence) == ("seasonal_dip", "keep_limit", "medium")
    assert (review.guideline_ids, review.review_reasons) == (["G-03"], ["large_cut"])
    assert review.human_decision == "keep_limit"
    assert review.evidence == [] and review.asked == [] and review.action is None
    assert review.text == "The yearly stocking payment went out. Keep."


def test_a_seed_whose_case_is_there_gets_its_review_only():
    with SessionLocal() as session:
        session.add(Case(case_id="seed_x", customer_id="c_900", status="done"))
        session.commit()

    assert seed([entry()]) == {"seeded": 1, "already_there": 0}
    assert [review.case_id for review in reviews()] == ["seed_x"]


def test_a_seed_that_is_not_a_closed_case_is_refused():
    with pytest.raises(ValueError, match="not a closed case"):
        seed([entry(outcome="needs_approval")])

    assert reviews() == []


def without(key: str) -> dict:
    found = entry("seed_b")
    del found[key]
    return found


@pytest.mark.parametrize(
    "broken",
    [
        # Each would be stored as it is, and a later case would read the string one character at a time.
        pytest.param(entry("seed_b", guideline_ids="G-03"), id="guideline ids not a list"),
        pytest.param(entry("seed_b", review_reasons="large_cut"), id="reasons not a list"),
        pytest.param(entry("seed_b", claims="The yearly stocking payment went out."), id="claims not a list"),
        pytest.param(entry("seed_b", cash_now_cents="40,000,000"), id="an amount that is not a number"),
        pytest.param(entry("seed_b", decisions=None), id="decisions left empty"),
        pytest.param(without("decisions"), id="no decisions"),
        pytest.param(entry("seed_b", decisions=[{"step": "analyst", "decision": "approve", "note": 2025}]),
                     id="a note that is not text"),
        pytest.param(entry("seed_b", as_of="10 November 2025"), id="a date that is not ISO"),
    ],
)
def test_a_seed_entry_with_a_slip_is_refused_by_its_id_before_anything_is_inserted(broken):
    with pytest.raises(ValueError, match="seed seed_b"):
        seed([entry("seed_a"), broken])

    assert reviews() == []


def test_a_seed_date_yaml_reads_without_quotes_is_the_same_date(tmp_path):
    # YAML reads as_of: 2025-11-10 without quotes as a date, not as a string.
    path = seed_file(tmp_path, "one.yaml", [entry(as_of=date(2025, 11, 10))])
    assert "as_of: 2025-11-10" in path.read_text()

    seed(history.read_seeds([path]))

    assert reviews()[0].as_of == date(2025, 11, 10)


@pytest.mark.parametrize(
    "text",
    ["", "schema_version: 1\n", "schema_version: 1\nreviews:\n", "- seed_a\n", "reviews: [seed_a]\n"],
    ids=["empty", "no reviews", "reviews left empty", "a list", "reviews with no fields"],
)
def test_a_seed_file_with_no_list_of_reviews_is_refused_by_its_name(tmp_path, text):
    path = tmp_path / "broken.yaml"
    path.write_text(text)

    with pytest.raises(ValueError, match="broken.yaml"):
        history.read_seeds([path])


def test_the_seed_file_in_the_repo_loads_and_gives_c_007_the_past_review_its_story_names():
    seed(history.read_seeds([SEED_FILE]))

    (earlier,) = history.earlier_reviews("c_007", "2026-11-10")
    assert earlier == {
        "case_id": "seed_c_007_2025",
        "as_of": "2025-11-10",
        "outcome": "done",
        "approved": True,
        "likely_cause": "seasonal_dip",
        "recommendation": "keep_limit",
        "guideline_ids": ["G-03"],
        "review_reasons": ["large_cut"],
        "human_decision": None,
        "cash_7d_ago_cents": 68000000,
        "cash_now_cents": 40000000,
    }
    # The same weeks a year before: what the recovery record of G-03 is made from.
    assert (CLOCK - date.fromisoformat(earlier["as_of"])).days == 365


def test_the_seed_file_in_the_repo_holds_the_c_007_seed_and_nothing_else():
    # The image copies this file and the main stack seeds it. The evaluation's seeds have a file of their own.
    assert [found["case_id"] for found in history.read_seeds([SEED_FILE])] == ["seed_c_007_2025"]


def test_seeds_are_read_from_several_files_in_order(tmp_path):
    first = seed_file(tmp_path, "one.yaml", [entry("seed_a"), entry("seed_b")])
    second = seed_file(tmp_path, "two.yaml", [entry("seed_c")])

    assert [found["case_id"] for found in history.read_seeds([first, second])] == ["seed_a", "seed_b", "seed_c"]


def test_a_seed_id_in_two_files_or_twice_in_one_is_refused(tmp_path):
    first = seed_file(tmp_path, "one.yaml", [entry("seed_a")])
    second = seed_file(tmp_path, "two.yaml", [entry("seed_a", note="other")])
    twice = seed_file(tmp_path, "twice.yaml", [entry("seed_b"), entry("seed_b")])

    with pytest.raises(ValueError, match="seed_a"):
        history.read_seeds([first, second])
    with pytest.raises(ValueError, match="seed_b"):
        history.read_seeds([twice])


def test_a_missing_seed_file_is_skipped_with_one_warning_and_the_others_are_read(tmp_path, caplog):
    # HISTORY_SEED_FILE may name a file a stack does not have (0.3, items 1 and 9).
    first = seed_file(tmp_path, "one.yaml", [entry("seed_a")])
    second = seed_file(tmp_path, "two.yaml", [entry("seed_b")])

    with caplog.at_level(logging.WARNING, logger="app.history"):
        found = history.read_seeds([first, tmp_path / "nowhere.yaml", second])

    assert [entry["case_id"] for entry in found] == ["seed_a", "seed_b"]
    warnings = [record for record in caplog.records if record.name == "app.history"]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert "nowhere.yaml" in warnings[0].getMessage()
    # A missing file in between does not hide an id listed twice.
    again = seed_file(tmp_path, "again.yaml", [entry("seed_a")])
    with pytest.raises(ValueError, match="seed_a"):
        history.read_seeds([first, tmp_path / "nowhere.yaml", again])


def test_a_seed_file_that_is_not_yaml_is_refused_by_its_name(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("reviews: [seed_a\n")

    with pytest.raises(ValueError, match="broken.yaml"):
        history.read_seeds([path])


@pytest.mark.parametrize("between", ["a folder", "an empty name"])
def test_a_listed_path_that_is_not_a_file_is_skipped_with_one_warning_and_the_others_are_read(
    tmp_path, caplog, between
):
    # Docker makes a folder for a bind-mount source that is not there, and "a.yaml::b.yaml" lists an empty name.
    first = seed_file(tmp_path, "one.yaml", [entry("seed_a")])
    second = seed_file(tmp_path, "two.yaml", [entry("seed_b")])
    folder = tmp_path / "history_eval.yaml"
    folder.mkdir()

    with caplog.at_level(logging.WARNING, logger="app.history"):
        found = history.read_seeds([first, folder if between == "a folder" else "", second])

    assert [entry["case_id"] for entry in found] == ["seed_a", "seed_b"]
    assert [record.levelno for record in caplog.records if record.name == "app.history"] == [logging.WARNING]


def test_a_seed_file_that_is_not_utf_8_is_refused_by_its_name(tmp_path):
    path = tmp_path / "latin.yaml"
    path.write_bytes("reviews:\n  - case_id: seed_café\n".encode("latin-1"))

    with pytest.raises(ValueError, match="latin.yaml"):
        history.read_seeds([path])


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a file this user cannot read")
def test_a_seed_file_that_cannot_be_read_is_refused_by_its_name(tmp_path):
    path = seed_file(tmp_path, "locked.yaml", [entry("seed_a")])
    path.chmod(0)

    try:
        with pytest.raises(ValueError, match="locked.yaml"):
            history.read_seeds([path])
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize("case_id", [123, None, ["seed_a"]], ids=["a number", "empty", "a list"])
def test_a_seed_whose_case_id_is_not_text_is_refused_by_the_file_s_name(tmp_path, case_id):
    path = seed_file(tmp_path, "odd.yaml", [entry("seed_a"), entry(case_id)])

    with pytest.raises(ValueError, match="odd.yaml"):
        history.read_seeds([path])


@pytest.mark.parametrize(
    "earlier",
    [
        pytest.param(closed(as_of="2026-11-09"), id="approved"),
        pytest.param(closed("rejected", as_of="2026-11-09"), id="rejected, as the evaluation closes each run"),
        pytest.param(closed(as_of="2026-11-09", report=GIVE_UP, check={"passed": False, "problems": []}),
                     id="a give-up approved by hand"),
    ],
)
def test_seeding_refuses_a_database_that_holds_earlier_reviews_that_are_not_seeds(earlier):
    keep(earlier, case_id="case_earlier_run")

    with pytest.raises(ValueError, match="not seeds"):
        seed([entry()], expect_clean=True, clock=CLOCK)

    assert [review.case_id for review in reviews()] == ["case_earlier_run"]  # nothing seeded


def test_a_clean_database_may_hold_seeds_and_reviews_of_the_clock_date():
    # The evaluation's own runs are dated on the clock date: strictly-before keeps them out of history.
    seed([entry("seed_a")])
    keep(closed(as_of="2026-11-10"), case_id="case_this_run")

    assert seed([entry("seed_a"), entry("seed_b")], expect_clean=True, clock=CLOCK) == {"seeded": 1, "already_there": 1}


def test_expecting_a_clean_database_needs_the_clock():
    with pytest.raises(ValueError, match="clock"):
        seed([entry()], expect_clean=True)


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"decisions": [{**entry()["decisions"][0], "note": "Other."}]}, id="note"),
        pytest.param({"recommendation": "reduce_limit"}, id="recommendation"),
        pytest.param({"customer_id": "c_901"}, id="customer"),
        pytest.param({"outcome": "rejected"}, id="outcome"),
    ],
)
def test_a_seed_id_that_exists_with_other_content_is_refused(changes, caplog):
    seed([entry("seed_a"), entry("seed_b")])
    before = everything_stored()
    changed = [entry("seed_a"), entry("seed_b", **changes), entry("seed_c")]

    with pytest.raises(ValueError, match="seed_b"):
        seed(changed, expect_clean=True, clock=CLOCK)
    assert everything_stored() == before  # refused before anything was inserted

    # Seeding at start does not stop on it: the stored seed stays as it is, with a warning.
    with caplog.at_level(logging.WARNING, logger="app.history"):
        assert seed(changed) == {"seeded": 1, "already_there": 2}
    assert "seed_b is already there with other content" in caplog.text
    assert [review.case_id for review in reviews()] == ["seed_a", "seed_b", "seed_c"]


def test_a_case_of_another_customer_under_a_seed_id_is_refused_and_never_given_the_seed(caplog):
    with SessionLocal() as session:
        session.add(Case(case_id="seed_x", customer_id="c_901", status="done"))
        session.commit()

    with pytest.raises(ValueError, match="seed_x"):
        seed([entry()], expect_clean=True, clock=CLOCK)
    with caplog.at_level(logging.WARNING, logger="app.history"):
        assert seed([entry()]) == {"seeded": 0, "already_there": 1}

    assert reviews() == []
    assert "seed_x is already there with other content" in caplog.text


def test_a_seed_with_a_vector_is_still_the_same_content():
    seed([entry()])
    with SessionLocal() as session:
        review = session.get(Review, "seed_x")
        review.embedder, review.embedding = "hash:4", [1.0, 0.0, 0.0, 0.0]
        session.commit()

    assert seed([entry()], expect_clean=True, clock=CLOCK) == {"seeded": 0, "already_there": 1}


def run_seed_command(*args: str, embedder: str | None = "hash:384",
                     database_url: str | None = None) -> subprocess.CompletedProcess:
    # The child must use this process's database. Not os.environ's URL: a test module that imports
    # tests.conftest runs conftest a second time, which points DATABASE_URL at another file.
    environment = {key: value for key, value in os.environ.items() if key != "EMBEDDINGS_MODEL"}
    environment["DATABASE_URL"] = database_url or engine.url.render_as_string(hide_password=False)
    if embedder:
        environment["EMBEDDINGS_MODEL"] = embedder
    return subprocess.run(
        [sys.executable, "-m", "app.seed_history", *map(str, args)],
        cwd=ORCHESTRATOR, env=environment, capture_output=True, text=True, timeout=60,
    )


def test_the_seed_command_takes_several_files_and_a_second_run_changes_nothing(tmp_path):
    first = seed_file(tmp_path, "one.yaml", [entry("seed_a")])
    second = seed_file(tmp_path, "two.yaml", [entry("seed_b"), entry("seed_c")])

    done = run_seed_command(first, second)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"seeded": 3, "already_there": 0, "embedded": 3}
    stored = everything_stored()

    again = run_seed_command(first, second, "--expect-clean")
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout) == {"seeded": 0, "already_there": 3, "embedded": 0}
    assert everything_stored() == stored
    assert {review.embedder for review in reviews()} == {"hash:384"}


def test_the_seed_command_seeds_with_no_embedder_and_embeds_nothing(tmp_path):
    # Seeding needs no model. Search is then off, and the reviews are still kept for the facts at open.
    done = run_seed_command(seed_file(tmp_path, "one.yaml", [entry("seed_a")]), embedder=None)

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {"seeded": 1, "already_there": 0, "embedded": 0}
    assert [(review.case_id, review.embedder) for review in reviews()] == [("seed_a", None)]


def test_the_seed_command_reads_its_clock_from_the_customer_files(tmp_path):
    # The evaluation's own runs are dated on the clock date, 2026-11-10, and are not history.
    # With the refusal of a run dated the day before (below), this pins the clock to that date.
    keep(closed(as_of="2026-11-10"), case_id="case_this_run")

    done = run_seed_command(seed_file(tmp_path, "one.yaml", [entry("seed_a")]), "--expect-clean")

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["seeded"] == 1


def test_the_seed_command_refuses_a_database_that_is_not_clean(tmp_path):
    keep(closed(as_of="2026-11-09"), case_id="case_earlier_run")

    done = run_seed_command(seed_file(tmp_path, "one.yaml", [entry("seed_a")]), "--expect-clean")

    assert done.returncode == 1
    assert "not seeds" in done.stderr
    assert [review.case_id for review in reviews()] == ["case_earlier_run"]


def test_the_seed_command_names_the_seed_it_refuses_and_seeds_nothing(tmp_path):
    path = seed_file(tmp_path, "one.yaml", [entry("seed_a"), entry("seed_b", guideline_ids="G-03")])

    done = run_seed_command(path)

    assert done.returncode == 1
    assert "Not seeded: seed seed_b" in done.stderr
    assert reviews() == []


def test_the_seed_command_refuses_a_folder_it_was_given_without_a_traceback(tmp_path):
    # A file a person names on the command line is meant to be read: unlike a listed setting, it is not skipped.
    folder = tmp_path / "history_eval.yaml"
    folder.mkdir()

    done = run_seed_command(seed_file(tmp_path, "one.yaml", [entry("seed_a")]), folder)

    assert done.returncode == 1
    assert "Not seeded: " in done.stderr and "history_eval.yaml" in done.stderr
    assert "Traceback" not in done.stderr
    assert reviews() == []


def test_the_seed_command_on_a_database_with_no_tables_says_not_seeded_in_one_line(tmp_path):
    # The service creates the tables at start. Run before that, the command names the driver's error only.
    empty = tmp_path / "empty.db"

    done = run_seed_command(
        seed_file(tmp_path, "one.yaml", [entry("seed_a")]), "--expect-clean", database_url=f"sqlite:///{empty}"
    )

    assert done.returncode == 1
    assert done.stderr.startswith("Not seeded: ")
    assert len(done.stderr.splitlines()) == 1
    assert "no such table" in done.stderr
    # str(error) would carry the SQL and a link. The database URL is never shown.
    for leak in ("Traceback", "[SQL", "sqlalche.me", "sqlite:", str(empty)):
        assert leak not in done.stderr
    assert done.stdout == ""


def test_the_seed_command_commits_nothing_when_the_database_refuses_the_vectors(tmp_path):
    # The reviews are flushed before their vectors are written. A refusal then must leave no half-seeded rows,
    # and SQLite raises it as an IntegrityError, not an OperationalError.
    database_url = f"sqlite:///{tmp_path / 'refusing.db'}"
    refusing = create_engine(database_url)
    Base.metadata.create_all(refusing)
    with refusing.begin() as connection:
        connection.execute(
            text("CREATE TRIGGER no_vectors BEFORE UPDATE ON reviews BEGIN SELECT RAISE(ABORT, 'no vectors'); END")
        )

    done = run_seed_command(seed_file(tmp_path, "one.yaml", [entry("seed_a")]), database_url=database_url)

    assert done.returncode == 1
    assert done.stderr == "Not seeded: database error: no vectors\n"
    with refusing.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM cases")) == 0
        assert connection.scalar(text("SELECT count(*) FROM reviews")) == 0
    refusing.dispose()
