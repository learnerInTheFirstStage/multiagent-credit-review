"""What the guideline needs permit on the evaluation's data, with no model (spec 8.3, 9.9).

For every case of the golden set: the signal facts of eval/signals.py, the history facts of the seed files
(seeded into the test database), and what the real stand-ins answer, topic by topic, on the case's path, on
the fixed plan of 8.4, and with every topic asked. The tests pin today's findings (5.2): a change to a need,
a threshold, a stand-in or a case that opens a new overlap fails here before a model is paid for.
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "eval"))

from signals import FILES, customers, signal_for  # noqa: E402

from app import guidelines, history, policy  # noqa: E402
from app.agents import mock  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.graph import causes, facts, figures, needs, plans  # noqa: E402
from app.graph.build import MAX_ROUNDS, WINDOW_DAYS  # noqa: E402
from contracts import InvestigateRequest  # noqa: E402

GOLDEN = yaml.safe_load((ROOT / "eval" / "golden_set.yaml").read_text())
CASES = {case["id"]: case for case in GOLDEN["customers"]}
TODAY, DATA = customers()
# The c_007 seed every stack has, and the evaluation's own seeds (spec 0.3, item 1). A file not there is skipped.
HISTORY_FILES = (ROOT / "eval" / "history.yaml", ROOT / "eval" / "history_eval.yaml")
SIDES = {"AskCashFlow": mock.cash_flow, "AskCreditBehavior": mock.credit_behavior}
TOOL = {"bank": "AskCashFlow", "card": "AskCreditBehavior"}
# The records code gives at open or works out itself. A run is given them whatever it asks.
OWN = ("hist_", "sig_", "xs_")
PLAN = [(step["tool"], step["args"]["topic"], None) for step in plans.FIXED_PLAN]
# Permitted with every topic asked, and not accepted (5.2). The check cannot tell them apart. The model must.
OVERLAPS = {
    "c_003": {"request_documents", "pause_card"},
    "c_203": {"request_documents", "pause_card"},
    "c_303": {"request_documents", "pause_card"},
    "c_107": {"request_documents", "pause_card", "keep_limit"},
}
by_case = pytest.mark.parametrize("case", GOLDEN["customers"], ids=lambda case: case["id"])


@pytest.fixture(scope="module", autouse=True)
def evaluation():
    """The stand-ins read the evaluation's customers, and the database holds its seeded history and nothing else."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("SHARED_CUSTOMERS_FILE", os.pathsep.join(str(path) for path in FILES))
        # A review left behind would give a later test's customer an earlier review.
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        try:
            with SessionLocal() as session:
                history.seed(session, history.read_seeds(HISTORY_FILES), expect_clean=True, clock=TODAY)
                session.commit()
            yield
        finally:
            Base.metadata.drop_all(engine)
            Base.metadata.create_all(engine)


def steps(path: list[str]) -> list[tuple[str | None, str, str | None]]:
    """"bank.payee_history@txn_c_009_01" as ("AskCashFlow", "payee_history", "txn_c_009_01")."""
    found = []
    for step in path:
        side, _, rest = step.partition(".")
        topic, _, record_id = rest.partition("@")
        found.append((TOOL.get(side), topic, record_id or None))
    return found


def walk(customer_id: str, asks: list[tuple]) -> tuple[list[dict], list[dict]]:
    """The ledger and the `asked` entries (1.3) of a case after these questions, as the graph would hold them.

    The window open_case sets, the facts it writes at open, then each question to the real stand-in.
    """
    signal = signal_for(DATA[customer_id], TODAY)
    end = date.fromisoformat(signal["as_of"])
    window = {"start": (end - timedelta(days=WINDOW_DAYS)).isoformat(), "end": end.isoformat()}
    reviews = history.earlier_reviews(customer_id, signal["as_of"], limit=None)
    state = {"facts": [*facts.signal_facts(signal, window), *facts.history_facts(reviews, signal)], "answers": []}
    asked = []
    for tool, topic, record_id in asks:
        focus = {"record_id": record_id} if record_id else {}
        request = InvestigateRequest(
            case_id="case_policy", customer_id=customer_id, question="What happened?",
            window_start=window["start"], window_end=window["end"], topic=topic, **focus,
        )  # fmt: skip
        response = SIDES[tool](request).model_dump(mode="json")
        state["answers"].append({"agent": response["agent"], "question": "What happened?", "response": response})
        asked.append({
            "tool": tool, "agent": response["agent"], "topic": topic, "separates": causes.TOPIC_TESTS[topic],
            "tests": causes.TOPIC_TESTS[topic], "outcome": "answered",
            "returned": [item["id"] for item in response["evidence"]], **focus,
        })  # fmt: skip
    return list(facts.ledger(state).values()), asked


def every_topic(customer_id: str) -> tuple[list[dict], list[dict]]:
    """Every topic in catalogue order, then the payee's history of every payment the bank side listed."""
    asks = [
        (tool, topic, None)
        for tool, topics in causes.TOPICS.items()
        for topic in topics
        if (tool, topic) not in causes.NEEDS_RECORD
    ]
    records, _ = walk(customer_id, asks)
    asks += [("AskCashFlow", "payee_history", item["id"]) for item in records if item["kind"] == "bank_transaction"]
    return walk(customer_id, asks)


def carried(records: list[dict], asked: list[dict]) -> set[str]:
    """What a report could recommend: the permitted actions, and a hand-over when a guideline carries one."""
    loaded = guidelines.load()
    found = set(needs.permitted(loaded, records))
    if needs.hand_over(loaded, records, asked):
        found.add("needs_human")
    return found


def carriers(case: dict, records: list[dict], asked: list[dict]) -> set[str]:
    """The guidelines that carry one of the case's accepted recommendations on these records.

    For an acting one, those needs.permitted gives. They carry it together: one of them may cover a must-address
    trigger for the others (2.A, rule 8).
    """
    loaded = guidelines.load()
    found = {
        guideline_id
        for recommendation, ids in needs.permitted(loaded, records).items()
        if recommendation in case["recommendations"]
        for guideline_id in ids
    }
    if "needs_human" in case["recommendations"]:
        found |= set(needs.hand_over(loaded, records, asked))
    return found


def carrying(case: dict, records: list[dict], asked: list[dict]) -> set[str]:
    """The case's own guidelines that carry one of its accepted recommendations on these records (8.3, point 3)."""
    return carriers(case, records, asked) & set(case["guidelines"])


def met(records: list[dict], asked: list[dict]) -> set[str]:
    """The guidelines with every need met, none_fits included."""
    loaded = guidelines.load()
    return {g["id"] for g in loaded if not needs.unmet(g, records, guidelines=loaded, known=records, asked=asked)}


def must_reach(case: dict) -> set[str]:
    """What a run must have been given for the case to count as tested (8.1)."""
    return set(case.get("must_reach", [cid for cid in case.get("must_cite", []) if not cid.startswith(OWN)]))


def reached(case: dict, records: list[dict], asked: list[dict]) -> bool:
    """The questions returned what the case must reach, the ledger holds what it must cite, and an accepted answer is
    carried: class R of 8.4."""
    returned = {evidence_id for entry in asked for evidence_id in entry["returned"]}
    return (
        must_reach(case) <= returned
        and set(case.get("must_cite", [])) <= {item["id"] for item in records}
        and bool(carrying(case, records, asked))
    )


@by_case
def test_every_case_can_be_reached_in_six_questions_and_its_path_returns_what_it_must_cite(case):
    path = steps(case["path"])
    assert 0 < len(path) <= MAX_ROUNDS
    assert len(set(path)) == len(path)  # a question asked twice settles nothing new
    for number, (tool, topic, record_id) in enumerate(path):
        assert tool in causes.TOPICS and topic in causes.TOPICS[tool], case["path"][number]
        # A payee's history is asked about one payment, which an earlier answer listed (2.C). No other topic takes one.
        assert (record_id is not None) == ((tool, topic) in causes.NEEDS_RECORD), case["path"][number]
        if record_id:
            known = {item["id"]: item["kind"] for item in walk(case["id"], path[:number])[0]}
            assert known.get(record_id) == "bank_transaction", case["path"][number]

    records, asked = walk(case["id"], path)
    returned = {evidence_id for entry in asked for evidence_id in entry["returned"]}
    assert set(case.get("must_cite", [])) <= {item["id"] for item in records}
    # A record a run must reach is one a question returns, never one code gives at open.
    assert not [evidence_id for evidence_id in must_reach(case) if evidence_id.startswith(OWN)]
    assert must_reach(case) <= returned


def test_a_hand_over_counts_as_tested_only_once_a_question_returned_what_it_must_reach():
    # 8.1, 8.5: a run that asked nothing can end in a hand-over too, and a case with no must_reach is never "not
    # reached". c_307's and c_314's only must_cite is a record code gives at open, so their must_reach is written out.
    hand_overs = {cid for cid, case in CASES.items() if "needs_human" in case["recommendations"]}

    assert {"c_307", "c_314"} <= hand_overs
    assert {cid for cid in hand_overs if not must_reach(CASES[cid])} == set()


@by_case
def test_every_accepted_cause_rests_on_a_record_the_cases_path_returns(case):
    # The cause rule refuses a cause no cited record shows (2.D.4). On a path without that record the accepted cause
    # cannot be given: c_307's path asks prior_year for seasonal_dip (8.1). no_clear_cause claims nothing.
    records, _ = walk(case["id"], steps(case["path"]))
    for cause in case["causes"]:
        alternatives = causes.CAUSE_NEEDS.get(cause, [])
        assert not alternatives or any(needs.meets(need, records) for need in alternatives), cause


@by_case
def test_a_memo_and_a_payee_history_the_case_rests_on_are_records_it_must_reach(case):
    # 8.1: c_009 and c_203 do not count as tested unless the payment with the memo was returned, and c_009's answer
    # rests on a payee's history, which only a question about one payment returns (class D, 8.4).
    records, _ = every_topic(case["id"])
    memos = {item["id"] for item in records if item["kind"] == "bank_transaction" and "memo" in item["data"]}
    assert memos <= must_reach(case)

    kinds = {need["kind"] for g in guidelines.load() if g["id"] in case["guidelines"] for need in g["needs"]}
    if "payee_history" in kinds:
        found = {item["id"] for item in walk(case["id"], steps(case["path"]))[0] if item["kind"] == "payee_history"}
        assert found and found <= must_reach(case)


@by_case
def test_the_records_of_a_case_meet_an_accepted_guideline_and_carry_nothing_else_but_the_listed_overlaps(case):
    assert carrying(case, *walk(case["id"], steps(case["path"])))

    found = carried(*every_topic(case["id"]))
    assert found - set(case["recommendations"]) == OVERLAPS.get(case["id"], set())


@by_case
def test_the_guidelines_a_case_lists_are_the_ones_that_carry_an_accepted_answer(case):
    # Stronger than 8.3, point 3. The path grade counts a report right on `guideline` when every guideline it cites
    # is listed (8.5). So every listed guideline carries an accepted answer on the path, alone or cited with the
    # other listed ones that carry it: c_111's G-01 passes only beside G-08a, which covers the failed repayment. And
    # a guideline that carries one, on the path or with every topic asked, is listed, or a report that passes the
    # check is graded wrong: c_012's hand-over is carried by G-10b and by G-11, so both are listed (0.3, item 13).
    listed = set(case["guidelines"])
    path = carriers(case, *walk(case["id"], steps(case["path"])))
    every = carriers(case, *every_topic(case["id"]))

    assert listed <= path
    assert (path | every) - listed == set()


def test_the_accepted_answers_no_guideline_can_carry_are_the_two_known_ones():
    # c_010: no guideline that allows a cut has a need its records meet. c_106: its inflows stopped, and G-06 needs
    # them shrinking. The other accepted answer of each is carried.
    left = {}
    for case in GOLDEN["customers"]:
        if missing := set(case["recommendations"]) - carried(*every_topic(case["id"])):
            left[case["id"]] = missing

    assert left == {"c_010": {"reduce_limit"}, "c_106": {"reduce_limit"}}


def test_with_the_strict_reading_of_unpaid_nothing_accepted_for_c_010_can_pass(monkeypatch):
    walks = {case["id"]: every_topic(case["id"]) for case in GOLDEN["customers"]}
    default = {cid: carried(*found) for cid, found in walks.items()}
    path = walk("c_010", steps(CASES["c_010"]["path"]))

    # Decision 1: "not_recorded" no longer counts as unpaid. Policy values are read when used (0.3, item 6).
    monkeypatch.setattr(policy, "UNPAID_MEANS", ("no",))
    assert next(g for g in guidelines.load() if g["id"] == "G-08b")["needs"][0]["where"]["paid_since"] == {"in": ["no"]}
    strict = {cid: carried(*found) for cid, found in walks.items()}

    for records, asked in (walks["c_010"], path):
        assert carried(records, asked) & set(CASES["c_010"]["recommendations"]) == set()
        # Nothing acting is permitted and nothing is left open, so the case goes to a person under G-11.
        assert carried(records, asked) == {"needs_human"}
        assert needs.hand_over(guidelines.load(), records, asked) == ["G-11"]
    # The reading bears on c_010 alone: c_110's failed repayment is recorded unpaid, c_111's as paid.
    assert {cid for cid in walks if strict[cid] != default[cid]} == {"c_010"}


def test_twins_meet_different_guidelines_or_differ_in_the_account_match():
    def account_match(customer_id: str, records: list[dict]) -> str | None:
        # The ids that say whose it is are taken out: "acct_external_new_c106" is the same account as c_006's.
        found = next((item["data"] for item in records if item["id"] == "xs_account_match"), None)
        if found is None:
            return None
        return json.dumps(found, sort_keys=True).replace(customer_id, "X").replace(customer_id.replace("_", ""), "X")

    separated = {}
    for case in GOLDEN["customers"]:
        if "twin_of" not in case:
            continue
        mine, theirs = every_topic(case["id"]), every_topic(case["twin_of"])
        separated[case["id"]] = [
            what
            for what, differ in (
                ("guidelines", met(*mine) != met(*theirs)),
                ("account match", account_match(case["id"], mine[0]) != account_match(case["twin_of"], theirs[0])),
            )
            if differ
        ]

    assert separated and all(separated.values()), separated
    # c_006 and c_106 both meet G-05 and nothing else. Only the cross-side match tells them apart: a payment c_106's
    # bank side listed (txn_c_106_02, which it must cite) went to the account that now pays the card, and none of
    # c_006's did.
    assert met(*every_topic("c_006")) == met(*every_topic("c_106")) == {"G-05"}
    assert separated["c_106"] == ["account match"]
    sent = {
        cid: next(item["data"]["transaction_ids"] for item in every_topic(cid)[0] if item["id"] == "xs_account_match")
        for cid in ("c_006", "c_106")
    }
    assert sent == {"c_006": [], "c_106": ["txn_c_106_02"]}


def test_the_fixed_plan_reaches_every_case_but_the_ones_that_need_a_payees_history():
    # A payee's history needs the id of a payment (4.1), so the plan cannot ask it. Class D of 8.4.
    need_history = {
        case["id"]
        for case in GOLDEN["customers"]
        if any(topic == "payee_history" for _, topic, _ in steps(case["path"]))
    }
    not_reached = {case["id"] for case in GOLDEN["customers"] if not reached(case, *walk(case["id"], PLAN))}

    # Only a question about c_009's payment shows that it is in line with its payee's history. For c_011 and c_307
    # "none fits" is not settled until that history was asked for (2.A, rule 7b).
    assert not_reached == need_history == {"c_009", "c_011", "c_307"}


def test_after_the_fixed_plan_one_question_the_code_gaps_name_reaches_each_case_the_plan_did_not():
    # fixed_rules asks the plan, then what the code gaps name (8.4). For a case of class D that is one follow-up.
    dependent = 0
    for case in GOLDEN["customers"]:
        records, asked = walk(case["id"], PLAN)
        if reached(case, records, asked):
            continue
        dependent += 1
        found = needs.code_gaps(guidelines.load(), records, asked)
        assert len(found) == 1, (case["id"], found)
        follow_up = plans.follow_up(found[0]["ask"])
        step = (follow_up["tool"], follow_up["args"]["topic"], follow_up["args"].get("record_id"))

        assert step in steps(case["path"])
        assert reached(case, *walk(case["id"], [*PLAN, step])), case["id"]
    assert dependent


@pytest.mark.parametrize(
    "case",
    [case for case in GOLDEN["customers"] if "needs_human" in case["recommendations"]],
    ids=lambda case: case["id"],
)
def test_a_hand_over_because_nothing_fits_is_carried_on_the_cases_path_and_not_after_its_first_question(case):
    loaded, path = guidelines.load(), steps(case["path"])

    assert "G-11" in needs.hand_over(loaded, *walk(case["id"], path))
    # After one question a guideline that allows an action still needs a record nobody asked for (2.A, rule 7b).
    records, asked = walk(case["id"], path[:1])
    assert needs.hand_over(loaded, records, asked) == []
    assert needs.unsettled(loaded, records, asked)


@by_case
def test_code_finds_no_gap_at_the_end_of_a_cases_path(case):
    # The listed path is one the agent is not nudged off: after its last question code asks for nothing more (2.D.3).
    assert needs.code_gaps(guidelines.load(), *walk(case["id"], steps(case["path"]))) == []


def test_a_dev_lookalike_carries_what_its_dev_case_carries_and_only_by_the_default_reading(monkeypatch):
    # Tuning on the dev case is tuning on its look-alike, so the summary reports it apart (8.5). A look-alike meets
    # the same guidelines, carries the same recommendations and shares an accepted one.
    dev = [case for case in GOLDEN["customers"] if case["split"] == "dev"]
    held_out = [case for case in GOLDEN["customers"] if case["split"] == "held_out"]
    walks = {case["id"]: every_topic(case["id"]) for case in GOLDEN["customers"]}

    def lookalikes() -> dict[str, str]:
        seen = {cid: (met(*found), carried(*found)) for cid, found in walks.items()}
        return {
            case["id"]: other["id"]
            for case in held_out
            for other in dev
            if seen[case["id"]] == seen[other["id"]] and set(case["recommendations"]) & set(other["recommendations"])
        }

    named = {case["id"]: case["dev_lookalike"] for case in GOLDEN["customers"] if "dev_lookalike" in case}
    assert named and lookalikes() == named
    # c_110 looks like c_010 only while "not_recorded" counts as unpaid (decision 1).
    monkeypatch.setattr(policy, "UNPAID_MEANS", ("no",))
    assert lookalikes() == {}


def anonymous(customer_id: str, records: list[dict]) -> dict[str, dict]:
    """Records by id, with the ids that say whose they are taken out: c_304's "acct_c304_a" reads as c_314's."""
    text = json.dumps(records).replace(customer_id, "X").replace(customer_id.replace("_", ""), "X")
    return {record["id"]: record for record in json.loads(text)}


def opened_with(customer_id: str) -> dict[str, dict]:
    """The data of the history records a case opens with, by id, without the ids that say whose."""
    opened = anonymous(customer_id, walk(customer_id, [])[0])
    return {record_id: record["data"] for record_id, record in opened.items() if record_id.startswith("hist_")}


def test_the_seeds_give_each_case_the_history_the_spec_lists():
    # The check for the implementer in 8.2. A slip in a seed's date, amount or customer moves one of these, and an
    # answer with it.
    opened = {case_id: opened_with(case_id) for case_id in CASES}
    seeded = {case_id for case_id, found in opened.items() if found["hist_X_summary"]["earlier_reviews"]}
    recovery = {case_id: found["hist_X_recovery"]["now_vs_then_bp"] for case_id, found in opened.items()
                if "hist_X_recovery" in found}  # fmt: skip
    counts = {
        case_id: tuple(opened[case_id]["hist_X_summary"][key] for key in (
            "reviews_in_window", "document_requests_in_window", "document_requests_for_income_in_window",
        ))  # fmt: skip
        for case_id in ("c_301", "c_304", "c_314")
    }

    assert seeded == {"c_007", "c_107", "c_307", "c_303", "c_301", "c_304", "c_314"}
    # Cash before this fall against cash before the fall a year ago: back for c_007, c_107 and c_303, not for c_307.
    assert recovery == {"c_007": 10294, "c_107": 10294, "c_307": 7778, "c_303": 10345}
    assert counts == {"c_301": (2, 0, 0), "c_304": (2, 2, 0), "c_314": (2, 2, 2)}
    assert [opened["c_314"][f"hist_seed_X_{n}"]["days_before"] for n in "ab"] == [147, 77]
    # A case with no seed opens with a summary of zeros and nothing else.
    for case_id in CASES.keys() - seeded:
        assert list(opened[case_id]) == ["hist_X_summary"], case_id


def test_the_control_for_a_repeat_differs_from_the_repeat_only_in_the_cause_of_its_earlier_requests():
    # 8.3, point 8. c_304 was asked twice since June to reconnect a feed, c_314 twice for weak income, on the same
    # dates and with the same decisions. Every other record is the same. The count G-14 reads is not.
    loaded = guidelines.load()
    control, repeat = (anonymous(cid, every_topic(cid)[0]) for cid in ("c_304", "c_314"))
    assert control.keys() == repeat.keys()
    assert all(control[record_id]["data"].keys() == repeat[record_id]["data"].keys() for record_id in control)
    differ = {
        record_id: sorted(key for key, value in record["data"].items() if repeat[record_id]["data"][key] != value)
        for record_id, record in control.items()
        if record != repeat[record_id]
    }
    assert differ == {
        "hist_X_summary": ["document_requests_for_income_in_window"],
        # The cause of each earlier request, and with it the guideline and the review reason it was filed under.
        "hist_seed_X_a": ["guideline_ids", "likely_cause", "review_reasons"],
        "hist_seed_X_b": ["guideline_ids", "likely_cause", "review_reasons"],
    }
    assert control["hist_X_summary"]["data"]["document_requests_for_income_in_window"] == 0
    assert repeat["hist_X_summary"]["data"]["document_requests_for_income_in_window"] == 2
    for walked in (lambda cid: walk(cid, steps(CASES[cid]["path"])), every_topic):
        assert needs.permitted(loaded, walked("c_304")[0]) == {"request_documents": ["G-14"]}
        assert needs.permitted(loaded, walked("c_314")[0]) == {}


def test_a_supported_precedent_and_todays_facts_are_both_permitted_for_c_107():
    # 8.1: c_007's review a year ago, c_007's stocking payment and last year's record, so G-03 is fully met. Shrinking
    # inflows and a late repayment meet G-06 and G-07 as well. The check permits keeping the limit and cutting it: only
    # the model, or a precedence rule the owner writes (decision 7), makes today's facts win.
    loaded, path = guidelines.load(), steps(CASES["c_107"]["path"])
    for records, asked in (walk("c_107", path), every_topic("c_107")):
        found = needs.permitted(loaded, records)

        assert found["keep_limit"] == ["G-03"]
        assert found["reduce_limit"] == ["G-06", "G-07"]
        assert {"G-03", "G-06", "G-07"} <= met(records, asked)
    # Last year's record is what makes the precedent a live option, which is why the case must reach it.
    assert path[-1] == ("AskCashFlow", "prior_year", None)
    assert "keep_limit" not in needs.permitted(loaded, walk("c_107", path[:-1])[0])
    assert "agg_c_107_prior_year" in must_reach(CASES["c_107"])


def test_the_words_that_say_a_need_hold_no_figure():
    # Both models are shown these words. A threshold in them could be copied into a report as a fact (1.6, 6.1).
    says = [need["says"] for guideline in guidelines.load() for need in guideline["needs"]]

    assert says
    assert {text: found for text in says if (found := figures.figures(text))} == {}
