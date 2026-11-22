"""Closed cases, kept so that later cases can look them up.

A review is written once, when a case closes, from what the graph saved. It is
read back as typed facts when a later case of the same customer opens. This
module imports the database and nothing from app.graph: the call sites pass the
ledger in.
"""

import logging
from collections.abc import Callable
from datetime import date
from pathlib import Path

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.db.models import Case, Review

log = logging.getLogger(__name__)

CLOSED = ("done", "rejected", "failed")
EARLIER_MAX = 10
TEXT_MAX = 2000
# The report parts whose items cite evidence ids.
_CITING_PARTS = ("evidence", "ruled_out", "other_findings", "review_reasons")
# What says nothing about the case itself, so two copies of one review may differ in it.
_NOT_CONTENT = ("closed_at", "embedder", "embedding")
# The seed fields whose YAML type matters. "guideline_ids: G-03" would be stored as a string,
# and a later case would read it one character at a time.
_SEED_TYPES = {
    "current_limit_cents": int,
    "engine_limit_cents": int,
    "cash_7d_ago_cents": int,
    "cash_now_cents": int,
    "review_reasons": list,
    "guideline_ids": list,
    "claims": list,
    "decisions": list,
}


def row(case_id: str, customer_id: str, state: dict, *, records: dict[str, dict], source: str = "case") -> Review:
    """Pure: no session, no embedding. The one constructor, used by record and by the seeder.

    Takes named keys of the state only, so the investigator's messages, a draft
    that was not checked and anything else in the state never reach the table.
    """
    signal = state.get("signal") or {}
    bank = (signal.get("snapshot") or {}).get("bank") or {}
    report = state.get("report") or {}
    decisions = state.get("decisions") or []
    cited = dict.fromkeys(
        evidence_id
        for part in _CITING_PARTS
        for item in report.get(part) or []
        for evidence_id in item.get("evidence_ids") or []
    )
    # Not the summary: the wording every report shares would drown what tells one case from another.
    words = [item["claim"] for item in report.get("evidence") or []]
    words += [decision["note"] for decision in decisions if decision.get("note")]
    return Review(
        case_id=case_id,
        customer_id=customer_id,
        as_of=date.fromisoformat(signal.get("as_of")),
        outcome=state.get("status"),
        source=source,
        check_passed=bool((state.get("check") or {}).get("passed")),
        likely_cause=report.get("likely_cause"),
        recommendation=report.get("recommendation"),
        confidence=report.get("confidence"),
        # The analyst's, not the last decision's: a manager, a retry or a documents step may follow it. A request for
        # documents stays one whatever they showed.
        human_decision=next(
            (decision["decided"] for decision in decisions
             if decision.get("step") == "analyst" and decision.get("decided")),
            None,
        ),
        guideline_ids=report.get("guideline_ids") or [],
        review_reasons=signal.get("review_reasons") or [],
        policy_version=state.get("policy_version"),
        current_limit_cents=signal.get("current_limit_cents"),
        engine_limit_cents=signal.get("engine_limit_cents"),
        cash_7d_ago_cents=bank.get("cash_7d_ago_cents"),
        cash_now_cents=bank.get("cash_now_cents"),
        report=state.get("report"),
        evidence=[records[evidence_id] for evidence_id in cited if evidence_id in records],
        decisions=decisions,
        action=state.get("action"),
        asked=state.get("asked") or [],
        text=" ".join(words).replace("\x00", "")[:TEXT_MAX],
    )


def record(session: Session, case: Case, state: dict, *, ledger: Callable[[dict], dict[str, dict]]) -> None:
    """Keep a closed case. Safe to call twice. Never raises and never commits.

    The status of the case is written in the same transaction. If this raised,
    the status would roll back with it. So every failure is logged and swallowed,
    and the insert runs in a savepoint: a failed one leaves the outer transaction usable.
    """
    try:
        if case.status not in CLOSED or state.get("status") != case.status:
            return  # still open, or a run that broke: its report was never checked
        if not state.get("signal") or state.get("check") is None:
            return
        with session.begin_nested():
            if session.get(Review, case.case_id) is not None:
                return
            # Inside the try: reading the ledger must not raise either.
            session.add(row(case.case_id, case.customer_id, state, records=ledger(state)))
    except Exception:
        log.exception("case %s: its review could not be kept", case.case_id)


def earlier_reviews(customer_id: str, as_of: str, *, limit: int | None = EARLIER_MAX) -> list[dict]:
    """This customer's reviews dated strictly before as_of whose report passed the check.

    Newest first, at most limit. limit=None gives every one: what a case opens with, since the counts of
    facts.history_facts are taken over all of them (spec 0.3, item 7). Opens its own session. A missing
    table raises: a broken lookup must not read as a first review.
    """
    with SessionLocal() as session:
        reviews = session.scalars(
            select(Review)
            .where(
                Review.customer_id == customer_id,
                Review.as_of < date.fromisoformat(as_of),
                Review.check_passed.is_(True),
            )
            .order_by(Review.as_of.desc(), Review.case_id.desc())
            .limit(limit)
        )
        return [
            {
                "case_id": review.case_id,
                "as_of": review.as_of.isoformat(),
                "outcome": review.outcome,
                "approved": review.outcome == "done",
                # The customer was asked: only an approved request for documents reaches the documents step, and
                # the case may still close rejected or failed after it (G-14, facts.history_facts).
                "requested_documents": any(decision.get("step") == "documents" for decision in review.decisions or []),
                "likely_cause": review.likely_cause,
                "recommendation": review.recommendation,
                "guideline_ids": review.guideline_ids,
                "review_reasons": review.review_reasons,
                "human_decision": review.human_decision,
                "cash_7d_ago_cents": review.cash_7d_ago_cents,
                "cash_now_cents": review.cash_now_cents,
            }
            for review in reviews
        ]


def read_seeds(paths: list[str | Path]) -> list[dict]:
    """The seed entries of every file, in order. An id listed twice, in one file or in two, is refused.

    A listed path that is not a file is skipped with one warning: HISTORY_SEED_FILE may name a file a
    stack does not have, Docker makes a folder for a bind-mount source that is not there, and "a::b"
    lists an empty name. A file that cannot be read, or is not valid UTF-8 YAML, is refused by its name.
    """
    entries, seen = [], set()
    for path in paths:
        if not Path(path).is_file():
            log.warning("seed file %s is not a file. It is skipped.", path)
            continue
        try:
            # Bytes, so that text that is not UTF-8 is a YAML error naming the file, not a UnicodeDecodeError.
            data = yaml.safe_load(Path(path).read_bytes())
        except (OSError, yaml.YAMLError) as error:
            raise ValueError(f"{path} could not be read as YAML: {error}") from error
        found = data.get("reviews") if isinstance(data, dict) else None
        if not isinstance(found, list) or not all(
            isinstance(entry, dict) and isinstance(entry.get("case_id"), str) for entry in found
        ):
            raise ValueError(f"{path} does not hold a list of reviews, each with its case_id as text")
        for entry in found:
            if entry["case_id"] in seen:
                raise ValueError(f"{entry['case_id']} is listed more than once in the seed files")
            seen.add(entry["case_id"])
            entries.append(entry)
    return entries


def seed(session: Session, entries: list[dict], *, expect_clean: bool, clock: date | None = None) -> dict:
    """Insert each entry as a closed case with its review, where it is not there yet. Never commits.

    Running it twice changes nothing. With expect_clean it refuses, before it
    inserts anything, a database that holds a review that is not a seed dated
    before clock, or a seed id that is there with other content. An evaluation
    must not start from somebody's earlier runs. Without it, a seed that is there
    with other content is left as it is, with a warning.
    """
    reviews = [_seed_review(entry) for entry in entries]
    differ = [review.case_id for review in reviews if _differs(session, review)]
    if expect_clean:
        if clock is None:
            raise ValueError("expect_clean needs the data clock")
        others = session.scalar(
            select(func.count()).select_from(Review).where(Review.source != "seed", Review.as_of < clock)
        )
        if others:
            raise ValueError(f"the database holds {others} reviews that are not seeds, dated before {clock}")
        if differ:
            raise ValueError(f"seeds already there with other content: {', '.join(differ)}")
    for case_id in differ:
        log.warning("seed %s is already there with other content. It is left as it is.", case_id)

    counts = {"seeded": 0, "already_there": 0}
    for review in reviews:
        if review.case_id in differ or session.get(Review, review.case_id) is not None:
            counts["already_there"] += 1
            continue
        if session.get(Case, review.case_id) is None:
            session.add(Case(case_id=review.case_id, customer_id=review.customer_id, status=review.outcome))
        session.add(review)
        counts["seeded"] += 1
    session.flush()
    return counts


def _seed_review(entry: dict) -> Review:
    """The review a flat seed entry describes. A slip in the entry is refused by its id."""
    try:
        return row(entry["case_id"], entry["customer_id"], _seed_state(entry), records={}, source="seed")
    except KeyError as error:
        raise ValueError(f"seed {entry.get('case_id')}: no {error.args[0]}") from error
    except (TypeError, AttributeError, ValueError) as error:
        raise ValueError(f"seed {entry.get('case_id')}: {error}") from error


def _seed_state(entry: dict) -> dict:
    """The state row() reads, built from a flat seed entry."""
    if entry["outcome"] not in CLOSED:
        raise ValueError(f"outcome {entry['outcome']} is not a closed case")
    wrong = [key for key, kind in _SEED_TYPES.items() if not isinstance(entry[key], kind)]
    if wrong:
        raise ValueError(f"{', '.join(wrong)} not of the right type")
    as_of = entry["as_of"]
    return {
        "signal": {
            # YAML reads a date written without quotes as a date.
            "as_of": as_of.isoformat() if isinstance(as_of, date) else as_of,
            "current_limit_cents": entry["current_limit_cents"],
            "engine_limit_cents": entry["engine_limit_cents"],
            "review_reasons": entry["review_reasons"],
            "snapshot": {
                "bank": {"cash_7d_ago_cents": entry["cash_7d_ago_cents"], "cash_now_cents": entry["cash_now_cents"]}
            },
        },
        # The records behind a seed's claims were not kept, so its claims cite nothing.
        "report": {
            "likely_cause": entry["likely_cause"],
            "recommendation": entry["recommendation"],
            "confidence": entry["confidence"],
            "guideline_ids": entry["guideline_ids"],
            "evidence": [{"claim": claim, "evidence_ids": []} for claim in entry["claims"]],
        },
        "check": {"passed": True, "problems": []},
        "decisions": entry["decisions"],
        "status": entry["outcome"],
    }


def _differs(session: Session, review: Review) -> bool:
    """Whether a case or review with this id is there with other content than the seed's."""
    case = session.get(Case, review.case_id)
    if case is not None and (case.customer_id, case.status) != (review.customer_id, review.outcome):
        return True
    stored = session.get(Review, review.case_id)
    return stored is not None and _content(stored) != _content(review)


def _content(review: Review) -> dict:
    return {
        column.key: getattr(review, column.key)
        for column in Review.__table__.columns
        if column.key not in _NOT_CONTENT
    }
