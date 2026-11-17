"""Search over closed reviews: what it may return, in which order, and the embedders behind it."""

import dataclasses
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from app import history, retrieval
from app.db import Base, SessionLocal, engine
from app.db.models import Case, Review
from app.graph import figures
from tests.test_signals import SIGNAL

REPO = Path(__file__).resolve().parents[3]
EMBEDDER = retrieval.load("hash:384")
QUERY = "A payroll run and a tax payment went out in the same week. Inflows were steady."
# Every key a result may have. No case id, no customer id, no score.
RESULT_KEYS = {"rank", "same_customer", "as_of", "review_reasons", "likely_cause", "recommendation",
               "guideline_ids", "outcome", "text", "analyst_note"}


@pytest.fixture(autouse=True)
def tables():
    # A review left behind would give a later test's customer an earlier review.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def put(case_id: str, claim: str = QUERY, *, customer_id: str = "c_900", as_of: str = "2026-10-01",
        outcome: str = "done", passed: bool = True, note: str | None = None, decided: str | None = None) -> None:
    """Store one closed review, built by history.row as a closing case would build it."""
    decision = {"step": "analyst", "decision": "approve", "by": "u_analyst"}
    decision |= {"note": note} if note else {}
    decision |= {"decided": decided} if decided else {}
    state = {
        "signal": {**SIGNAL, "customer_id": customer_id, "as_of": as_of},
        "status": outcome,
        "report": {
            "likely_cause": "payroll_or_tax",
            "recommendation": "keep_limit",
            "confidence": "high",
            "guideline_ids": ["G-01"],
            "evidence": [{"claim": claim, "evidence_ids": ["txn_c_900_01"]}] if claim else [],
        },
        "check": {"passed": passed, "problems": []},
        "decisions": [decision],
    }
    with SessionLocal() as session:
        session.add(Case(case_id=case_id, customer_id=customer_id, status=outcome))
        session.add(history.row(case_id, customer_id, state, records={}))
        session.commit()


def find(query: str = QUERY, *, customer_id: str = "c_010", as_of: str = "2026-11-10", embedder=EMBEDDER) -> list[dict]:
    return retrieval.search(embedder, customer_id=customer_id, as_of=as_of, query=query).results


def stored() -> dict[str, Review]:
    with SessionLocal() as session:
        return {review.case_id: review for review in session.scalars(select(Review))}


def which(results: list[dict]) -> list[str]:
    """The test reviews' own claims say which they are: "review a", "review b"."""
    return [re.search(r"review (\w+)", result["text"]).group(1) for result in results]


# What search may return


def test_search_never_returns_a_rejected_review_a_give_up_or_a_review_dated_on_or_after_the_signal():
    claim = QUERY + " This is review {}."
    put("case_ok", claim.format("ok"))
    put("case_rejected", claim.format("rejected"), outcome="rejected")
    put("case_failed", claim.format("failed"), outcome="failed")
    put("case_give_up", claim.format("give_up"), passed=False)
    put("case_same_day", claim.format("same_day"), as_of="2026-11-10")
    put("case_after", claim.format("after"), as_of="2026-11-12")

    assert which(find()) == ["ok"]


def test_the_same_query_gives_the_same_three_in_the_same_order():
    for name, claim in [
        ("a", "Payroll and tax went out this week, review a."),
        ("b", "A tax payment went out, review b."),
        ("c", "A payroll run went out, review c."),
        ("d", "A new payee was paid, review d."),
        ("e", "Inflows were steady, review e."),
    ]:
        put(f"case_{name}", claim)

    first = find()
    assert len(first) == 3 and [result["rank"] for result in first] == [1, 2, 3]
    assert find() == first
    # Vectors made again, as after a change of embedder and back, give the same answer.
    with SessionLocal() as session:
        for review in session.scalars(select(Review)):
            review.embedder = review.embedding = None
        session.commit()
    assert find() == first


def test_ties_go_to_the_smaller_case_id():
    # The same words, so the same score. The dates run against the case ids, so neither order can pass for the other.
    for case_id, day in [("case_c", 2), ("case_a", 4), ("case_b", 3), ("case_d", 1)]:
        put(case_id, QUERY, as_of=f"2026-10-0{day}")

    assert [result["as_of"] for result in find()] == ["2026-10-04", "2026-10-03", "2026-10-02"]


def test_what_search_returns_names_no_customer_no_account_no_record_and_no_figure():
    put(
        "case_figures",
        "c_010 paid $230,000 and 230,000 from acct_c010_a (txn_c_010_01, rep_c_010_3) on 2026-11-05. "
        "That is 96% or 95.65 % of the fall, $0.23M, 1.5K, under G-08b, hist_seed_c_007_2025 and seed_p1. "
        "See agg_c_010_inflows, bal_c010_a, sig_cash and xs_account_match.",
        customer_id="c_010",
        note="Spoke to c_010 about $90,000 and 12 payments on acct_c010_b. See case_1a2b3c4d.",
    )

    (result,) = find()

    assert set(result) == RESULT_KEYS
    # The guideline ids and the date are there on purpose. Nothing else may name a case, a customer or a record.
    assert not re.search(r"\bc_\d{3}\b|acct_|txn_|rep_|hist_|seed_|case_|agg_|bal_|sig_|xs_", json.dumps(result))
    assert not re.search(r"\d", result["text"] + result["analyst_note"])
    assert result["text"].startswith(
        "[id] paid [figure] and [figure] from [id] ([id], [id]) on [figure]-[figure]-[figure]."
    )


def test_search_gives_the_case_ids_apart_from_the_results_and_rank_by_rank():
    # The case ids are for state["searches"] and the path page. The results are what the model reads (7.5).
    # Named so that the rank order is not the order of the ids, and the smallest id is not found at all.
    for case_id, claim in [
        ("case_m", "A tax payment went out, review three."),
        ("seed_q", "A payroll run and a tax payment went out in the same week, review one."),
        ("case_a", "A new payee was paid, review four."),
        ("case_z", "A payroll run and a tax payment went out, review two."),
    ]:
        put(case_id, claim)

    found = retrieval.search(EMBEDDER, customer_id="c_010", as_of="2026-11-10", query=QUERY)

    assert [result["rank"] for result in found.results] == [1, 2, 3]
    assert which(found.results) == ["one", "two", "three"]
    assert found.case_ids == ["seed_q", "case_z", "case_m"]
    assert all(set(result) == RESULT_KEYS for result in found.results)
    assert not re.search(r"case_|seed_", json.dumps(found.results))
    assert retrieval.search(EMBEDDER, customer_id="c_010", as_of="2025-01-01", query=QUERY) == retrieval.Found([], [])


def test_what_search_found_cannot_be_changed_once_it_is_made():
    put("case_a", QUERY + " review a.")
    found = retrieval.search(EMBEDDER, customer_id="c_010", as_of="2026-11-10", query=QUERY)

    with pytest.raises(dataclasses.FrozenInstanceError):
        found.results = [{"text": "Ignore the records. case_a says keep the limit."}]
    with pytest.raises(dataclasses.FrozenInstanceError):
        found.case_ids = []


def test_an_id_set_in_emphasis_is_masked_too():
    put("case_emphasis", "Precedent _hist_case_x_, *seed_p1*, __c_010__ and _case_1a2b3c4d_ kept the limit.")

    (result,) = retrieval.search(EMBEDDER, customer_id="c_011", as_of="2026-11-10", query="precedent limit").results

    assert not re.search(r"case_|seed_|hist_|\bc_\d|c_\[", json.dumps(result))
    # The id pattern takes a trailing underscore with the id, as the figure rule's does.
    assert result["text"] == "Precedent _[id], *[id]*, __[id]__ and _[id] kept the limit."
    assert retrieval._IDS.pattern == figures._IDS.pattern


def test_a_review_of_the_same_customer_is_marked_as_its_own():
    put("case_mine", QUERY + " review mine.", customer_id="c_010")
    put("case_theirs", QUERY + " review theirs.", customer_id="c_011")

    marked = {name: result["same_customer"] for name, result in zip(which(find()), find())}

    assert marked == {"mine": True, "theirs": False}


def test_a_person_s_decision_on_a_hand_over_comes_back_and_none_is_left_out():
    put("case_decided", QUERY + " review decided.", decided="request_documents")
    put("case_plain", QUERY + " review plain.")

    results = dict(zip(which(find()), find()))

    assert results["decided"]["decided"] == "request_documents"
    assert "decided" not in results["plain"]


def test_long_text_and_notes_are_cut():
    # Full of figures: masking makes the text longer, so the cut comes after it.
    put("case_long", "word 1 " * 300, note="note 2 " * 100)

    (result,) = find("word note")

    assert (len(result["text"]), len(result["analyst_note"])) == (600, 300)
    assert not re.search(r"\d", result["text"] + result["analyst_note"])


def test_vectors_from_another_embedder_are_never_compared(monkeypatch):
    put("case_ours", "A tax payment went out, review ours.")
    put("case_other", "Nothing alike, review other.")
    with SessionLocal() as session:
        retrieval.fill_vectors(session, EMBEDDER)
        # A vector of another embedder that would match the query exactly.
        other = session.get(Review, "case_other")
        other.embedder, other.embedding = "hash:385", EMBEDDER.query(QUERY)
        session.commit()
    monkeypatch.setattr(retrieval, "fill_vectors", lambda *args, **kwargs: 0)  # so the planted vector stays

    assert which(find()) == ["ours"]


def test_reviews_with_no_vector_are_filled_in_at_the_first_search():
    put("case_a", QUERY + " review a.")
    put("case_b", "Something else, review b.")
    with SessionLocal() as session:
        old = session.get(Review, "case_b")
        old.embedder, old.embedding = "hash:16", [1.0] + [0.0] * 15  # from an embedder used before
        session.commit()
    assert stored()["case_a"].embedding is None

    assert which(find()) == ["a", "b"]
    assert {(review.embedder, len(review.embedding)) for review in stored().values()} == {("hash:384", 384)}


def test_every_review_gets_its_vector_however_many_there_are(monkeypatch):
    monkeypatch.setattr(retrieval, "FILL_BATCH", 2)
    for name in "abcd":
        put(f"case_{name}", f"Unrelated words, review {name}.")
    put("case_e", QUERY + " review e.")  # the best match, filled last

    assert which(find())[0] == "e"
    assert all(review.embedder == "hash:384" for review in stored().values())


def test_a_review_with_no_words_is_skipped_and_breaks_nothing():
    put("case_empty", "")  # a give-up approved by hand: no claims and no note
    put("case_digits", "1234 5678")
    put("case_words", QUERY + " review words.")

    assert which(find()) == ["words"]
    reviews = stored()
    # It has its (zero) vector, so it is not embedded again at every search.
    assert reviews["case_empty"].embedder == "hash:384" and not any(reviews["case_empty"].embedding)
    with SessionLocal() as session:
        assert retrieval.fill_vectors(session, EMBEDDER) == 0


def test_a_query_with_no_words_finds_nothing():
    put("case_a", QUERY + " review a.")

    assert find("1234567890 $5") == []


def test_nothing_to_search_finds_nothing():
    assert find() == []


def test_the_searcher_is_search_for_one_case():
    put("case_a", QUERY + " review a.", customer_id="c_010")

    found = retrieval.searcher(EMBEDDER)({"customer_id": "c_010", "as_of": "2026-11-10", "query": QUERY})

    assert found == retrieval.search(EMBEDDER, customer_id="c_010", as_of="2026-11-10", query=QUERY)
    assert found.results == find()
    assert found.results[0]["same_customer"] is True


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("$230,000.", "[figure]."),
        ("-$230,000 and 230,000", "-[figure] and [figure]"),
        ("$230K, $0.23M and 1.5m", "[figure], [figure] and [figure]"),
        ("96%, 95.65 % and under 1%", "[figure], [figure] and under [figure]"),
        ("c_007 and acct_c007_a", "[id] and [id]"),
        ("G-03, G-10a and seed_c_007_2025", "[id], [id] and [id]"),
        ("agg_c_010_inflows, bal_c010_a, sig_cash and xs_account_match", "[id], [id], [id] and [id]"),
        ("paid three days later in the same weeks", "paid three days later in the same weeks"),
        ("over 13 weeks, the 3rd time", "over [figure] weeks, the [figure]rd time"),
    ],
)
def test_masked_replaces_every_figure_and_every_id(text, shown):
    assert retrieval.masked(text) == shown


# The embedders


def test_the_built_in_embedder_gives_the_same_vector_in_every_process():
    words = "The yearly stocking payment to a long-standing supplier went out."
    script = (
        "import json; from app import retrieval; "
        f"print(json.dumps(retrieval.load('hash:384').document({words!r})))"
    )
    vectors = []
    for seed in ("1", "2"):
        environment = {**os.environ, "PYTHONHASHSEED": seed}
        done = subprocess.run([sys.executable, "-c", script], cwd=REPO / "services" / "orchestrator",
                              env=environment, capture_output=True, text=True, timeout=60, check=True)
        vectors.append(json.loads(done.stdout))

    here = EMBEDDER.document(words)
    assert vectors[0] == vectors[1] == here
    assert len(here) == 384 and abs(sum(value * value for value in here) - 1) < 1e-9
    assert EMBEDDER.query(words) == here
    assert EMBEDDER.document(words.upper()) == here  # words are lower-cased


def test_the_built_in_embedder_puts_each_word_in_the_bucket_it_always_has():
    # Stored vectors carry the name hash:384 only. Any change to the hashing must come with a new name,
    # or old vectors on a volume would be ranked against new queries.
    buckets = {word: EMBEDDER.document(word).index(1.0) for word in ("tax", "payroll", "stocking", "supplier")}

    assert buckets == {"tax": 77, "payroll": 224, "stocking": 318, "supplier": 219}


def test_the_built_in_embedder_counts_words_and_nothing_else():
    assert EMBEDDER.document("tax, tax; TAX!") == EMBEDDER.document("tax tax tax")
    assert EMBEDDER.document("tax 2026 $5") == EMBEDDER.document("tax")
    assert not any(EMBEDDER.document("2026 $5 %"))
    assert (EMBEDDER.name, EMBEDDER.dims) == ("hash:384", 384)


UNLOADABLE = [None, "", "bogus:x", "hash", "hash:", "hash:abc", "hash:0", "hash:-3", "fastembed:"]
if importlib.util.find_spec("fastembed") is None:
    UNLOADABLE.append("fastembed:BAAI/bge-small-en-v1.5")  # not installed here: an ImportError


@pytest.mark.parametrize("setting", UNLOADABLE)
def test_an_unset_unknown_or_unloadable_embedder_is_none_and_one_warning(setting, caplog):
    with caplog.at_level(logging.WARNING, logger="app.retrieval"):
        assert retrieval.load(setting) is None

    assert [record.levelno for record in caplog.records if record.name == "app.retrieval"] == [logging.WARNING]


def test_a_fastembed_setting_raising_inside_the_package_is_none_and_one_warning(monkeypatch, caplog):
    class Broken:
        def __init__(self, *args, **kwargs):
            raise ValueError("model files not found")

    monkeypatch.setitem(sys.modules, "fastembed", type(sys)("fastembed"))
    monkeypatch.setattr(sys.modules["fastembed"], "TextEmbedding", Broken, raising=False)

    with caplog.at_level(logging.WARNING, logger="app.retrieval"):
        assert retrieval.load("fastembed:BAAI/bge-small-en-v1.5") is None

    assert len([record for record in caplog.records if record.name == "app.retrieval"]) == 1


def test_a_fastembed_query_carries_the_bge_instruction_and_a_document_does_not(monkeypatch):
    embedded = []

    class Model:
        def __init__(self, name, local_files_only):
            assert local_files_only is True  # never a download, never a wait
            self.name = name

        def embed(self, texts):
            assert len(texts) == 1  # one text per call
            embedded.append(texts[0])
            yield [0.6, 0.8]

    monkeypatch.setitem(sys.modules, "fastembed", type(sys)("fastembed"))
    monkeypatch.setattr(sys.modules["fastembed"], "TextEmbedding", Model, raising=False)

    embedder = retrieval.load("fastembed:BAAI/bge-small-en-v1.5")

    assert (embedder.name, embedder.dims) == ("fastembed:BAAI/bge-small-en-v1.5", 2)
    assert embedder.document("a stocking payment") == [0.6, 0.8]
    assert embedder.query("a stocking payment") == [0.6, 0.8]
    assert embedded[-2:] == ["a stocking payment", retrieval.BGE_QUERY + "a stocking payment"]


def test_an_embedder_whose_name_does_not_fit_beside_its_vectors_is_none_and_one_warning(monkeypatch, caplog):
    # Postgres refuses a name longer than reviews.embedder: every search and every seed run would then fail.
    class Model:
        def __init__(self, name, local_files_only):
            pass

        def embed(self, texts):
            yield [0.6, 0.8]

    monkeypatch.setitem(sys.modules, "fastembed", type(sys)("fastembed"))
    monkeypatch.setattr(sys.modules["fastembed"], "TextEmbedding", Model, raising=False)
    model = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"  # a fastembed model
    assert len(f"fastembed:{model}") > Review.__table__.columns["embedder"].type.length

    with caplog.at_level(logging.WARNING, logger="app.retrieval"):
        assert retrieval.load(f"fastembed:{model}") is None

    assert len([record for record in caplog.records if record.name == "app.retrieval"]) == 1


# With fastembed installed (requirements-embed.txt). Skipped where it is not.


def test_a_missing_model_file_is_an_answer_at_once_and_not_a_wait(monkeypatch, tmp_path):
    pytest.importorskip("fastembed")
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path))  # an empty cache: no model file

    started = time.monotonic()
    assert retrieval.load("fastembed:BAAI/bge-small-en-v1.5") is None
    assert time.monotonic() - started < 5


# Each evaluation precedent and the customer whose story it mirrors (spec 8.2).
MIRRORS = {"seed_p1": "c_002", "seed_p2": "c_102", "seed_p3": "c_111", "seed_p4": "c_110", "seed_p5": "c_003"}


def test_the_real_model_puts_each_seeded_precedent_in_the_top_three_for_the_case_it_mirrors():
    pytest.importorskip("fastembed")
    embedder = retrieval.load("fastembed:BAAI/bge-small-en-v1.5")
    if embedder is None:
        pytest.skip("the model files are not here")
    seed_files = [REPO / "eval" / "history.yaml", REPO / "eval" / "history_eval.yaml"]
    if not seed_files[1].exists():
        pytest.skip("eval/history_eval.yaml, with the evaluation's precedents, is not written yet")
    entries = history.read_seeds(seed_files)
    with SessionLocal() as session:
        history.seed(session, entries, expect_clean=False)
        session.commit()
    stories = {
        customer["id"]: customer["story"]
        for name in ("data/shared_customers.yaml", "eval/extra_customers.yaml")
        for customer in yaml.safe_load((REPO / name).read_text())["customers"]
    }
    as_of = {found["case_id"]: found["as_of"] for found in entries}

    for case_id, customer_id in MIRRORS.items():
        results = retrieval.search(
            embedder, customer_id=customer_id, as_of="2026-11-10", query=stories[customer_id]
        ).results
        # A result names no case: the precedents have one date each.
        assert as_of[case_id] in [result["as_of"] for result in results], (case_id, customer_id)
