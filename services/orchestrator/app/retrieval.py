"""Search over closed reviews, for the investigator's SearchPastCases tool.

What it returns is untrusted background: earlier reports and people's notes,
masked so no figure or id crosses from one customer to another. It is never
evidence. Ranking is exact cosine over the JSON copy of each vector, in Python.
"""

import hashlib
import logging
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.db.models import Review

log = logging.getLogger(__name__)

TOP_K = 3
FILL_BATCH = 200
TEXT_MAX = 600
NOTE_MAX = 300
# BGE models are trained with this instruction in front of a query, and nothing in front of a document.
BGE_QUERY = "Represent this sentence for searching relevant passages: "

# The ids of spec 6.1 step 1, with the boundaries of app/graph/figures.py (_IDS there, the same pattern): a letter
# or digit ends an id, an underscore does not, so an id set in emphasis ("_hist_case_x_") is masked too.
# Masked before figures, so "c_007" is not "c_[figure]".
_IDS = re.compile(
    r"(?<![A-Za-z\d])"
    r"(?:(?:txn|acct|bal|agg|rep|ctx|crd|sig|hist|xs|case|seed)_[A-Za-z\d_]+|c_\d{3}|G-\d{2}[a-z]?)"
    r"(?![A-Za-z\d])"
)
# A run of digits with its $, its commas and decimals, and a % or a K or M after it.
_FIGURES = re.compile(r"\$?\d(?:[\d,.]*\d)?(?:\s?%|[KkMm]\b)?")


@dataclass(frozen=True)
class Found:
    """What a search found. Only `results` may reach a model."""

    # The keys of spec 7.5, masked: no case id, no customer id, no score.
    results: list[dict]
    # The case id of each result, in the same order: for state["searches"] and the path page.
    case_ids: list[str]


class Embedder(Protocol):
    name: str  # stored next to every vector
    dims: int

    def document(self, text: str) -> list[float]: ...

    def query(self, text: str) -> list[float]: ...


class _Hashed:
    """Built in: lower-case words hashed into buckets, counts L2-normalised.

    sha256, not hash(), so a vector is the same in every process and on every platform.
    """

    def __init__(self, dims: int):
        self.name = f"hash:{dims}"
        self.dims = dims

    def document(self, text: str) -> list[float]:
        counts = [0.0] * self.dims
        for word in re.findall(r"[a-z]+", text.lower()):
            counts[int.from_bytes(hashlib.sha256(word.encode()).digest()[:8], "big") % self.dims] += 1
        norm = math.sqrt(sum(count * count for count in counts))
        return [count / norm for count in counts] if norm else counts

    query = document


class _FastEmbed:
    def __init__(self, model: str):
        self.name = f"fastembed:{model}"
        # Postgres refuses a longer name beside a vector, and then every search and every seed run fails.
        if len(self.name) > Review.__table__.columns["embedder"].type.length:
            raise ValueError("its name is longer than the reviews table can store")
        from fastembed import TextEmbedding  # optional: requirements-embed.txt

        # Without local_files_only a missing model is fetched, or waited for, instead of refused.
        self._model = TextEmbedding(model, local_files_only=True)
        self.dims = len(self.document("dims"))  # also proves the model runs, at load and not at the first search

    def document(self, text: str) -> list[float]:
        # One text per call, so a stored vector does not depend on what it was batched with.
        return [float(value) for value in next(iter(self._model.embed([text])))]

    def query(self, text: str) -> list[float]:
        return self.document(BGE_QUERY + text)


def load(setting: str | None) -> Embedder | None:
    """The embedder EMBEDDINGS_MODEL names: "hash:<dims>" or "fastembed:<model>".

    None, with one warning, when it is unset, unknown or cannot be loaded. Then no search tool is offered.
    """
    if not setting:
        log.warning("EMBEDDINGS_MODEL is not set. No search over past cases is offered.")
        return None
    kind, _, value = setting.partition(":")
    try:
        if kind == "hash" and value.isdigit() and int(value) > 0:
            return _Hashed(int(value))
        if kind == "fastembed" and value:
            return _FastEmbed(value)
    except Exception as error:  # ImportError, a missing model file, anything fastembed raises
        log.warning("EMBEDDINGS_MODEL %s could not be loaded (%s). No search over past cases is offered.",
                    setting, error)
        return None
    log.warning("EMBEDDINGS_MODEL %s is not an embedder this service knows. No search over past cases is offered.",
                setting)
    return None


def fill_vectors(session: Session, embedder: Embedder, limit: int = FILL_BATCH) -> int:
    """Embed up to limit reviews that have no vector from this embedder. Returns how many. Never commits.

    A review with no words gets a zero vector, so it is not tried again at every search. Ranking skips it.
    """
    reviews = session.scalars(
        select(Review)
        .where(or_(Review.embedder.is_(None), Review.embedder != embedder.name))
        .order_by(Review.case_id)
        .limit(limit)
    ).all()
    for review in reviews:
        review.embedding = embedder.document(review.text)
        review.embedder = embedder.name
    session.flush()
    return len(reviews)


def search(embedder: Embedder, *, customer_id: str, as_of: str, query: str, k: int = TOP_K) -> Found:
    """The k approved reviews dated strictly before as_of that are nearest the query, of any customer.

    Ties go to the smaller case id. No score is given: a rank is all the model gets.
    Each result is masked and names no case, customer, account or record. Their case ids come apart.
    """
    with SessionLocal() as session:
        # Every candidate gets its vector first, however many there are.
        while fill_vectors(session, embedder, FILL_BATCH) == FILL_BATCH:
            pass
        session.commit()
        wanted = embedder.query(query)
        candidates = session.scalars(
            select(Review).where(
                Review.as_of < date.fromisoformat(as_of),
                Review.outcome == "done",
                Review.check_passed.is_(True),
                Review.embedder == embedder.name,  # vectors of another embedder are never compared
            )
        ).all()
        scored = [
            (score, review.case_id, review)
            for review in candidates
            if (score := _cosine(wanted, review.embedding)) is not None
        ]
        scored.sort(key=lambda item: (-item[0], item[1]))
        top = [review for _, _, review in scored[:k]]
        return Found(
            results=[_result(rank, review, customer_id) for rank, review in enumerate(top, start=1)],
            case_ids=[review.case_id for review in top],
        )


def searcher(embedder: Embedder) -> Callable[[dict], Found]:
    """What build_graph gets. Takes {"customer_id", "as_of", "query"}."""
    return lambda asked: search(embedder, customer_id=asked["customer_id"], as_of=asked["as_of"], query=asked["query"])


def masked(text: str) -> str:
    """Every id by "[id]", then every figure by "[figure]"."""
    return _FIGURES.sub("[figure]", _IDS.sub("[id]", text))


def _cosine(left: list[float], right: list[float] | None) -> float | None:
    """None when either vector is zero, or the review's is missing or of another length.

    So a review with no words is skipped, and a query with no words finds nothing.
    """
    if not right or len(right) != len(left):
        return None
    norms = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
    return sum(a * b for a, b in zip(left, right)) / norms if norms else None


def _result(rank: int, review: Review, customer_id: str) -> dict:
    notes = " ".join(decision["note"] for decision in review.decisions if decision.get("note"))
    result = {
        "rank": rank,
        "same_customer": review.customer_id == customer_id,
        "as_of": review.as_of.isoformat(),
        "review_reasons": review.review_reasons,
        "likely_cause": review.likely_cause,
        "recommendation": review.recommendation,
        "guideline_ids": review.guideline_ids,
        "outcome": review.outcome,
        "text": masked(review.text)[:TEXT_MAX],
        "analyst_note": masked(notes)[:NOTE_MAX],
    }
    if review.human_decision:
        result["decided"] = review.human_decision
    return result
