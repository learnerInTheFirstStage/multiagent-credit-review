"""The golden dataset holds together, and its grader and runner do what they say.

The dataset lives in eval/, outside this service. It is tested here because it is
built on this service's stand-ins, contracts and API.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import get_args

import httpx
import pytest
import yaml
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "eval"))

import grade as grading  # noqa: E402
import run as golden_run  # noqa: E402
from signals import FILES, customers, signal_for  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.db.models import Case  # noqa: E402
from app.graph.report import LikelyCause, Recommendation  # noqa: E402
from contracts import Signal  # noqa: E402
from tests.conftest import bearer  # noqa: E402
from tests.test_cases import start  # noqa: E402, F401
from tests.test_graph import INVESTIGATION, report  # noqa: E402
from tests.test_signals import SIGNAL  # noqa: E402  (c_010)
from tests.test_specialists import SIDES, ask  # noqa: E402

GOLDEN = yaml.safe_load((FILES[1].parent / "golden_set.yaml").read_text())
CASES = {case["id"]: case for case in GOLDEN["customers"]}
TODAY, DATA = customers()
ADDED = [customer["id"] for customer in yaml.safe_load(FILES[1].read_text())["customers"]]
by_id = pytest.mark.parametrize("case", GOLDEN["customers"], ids=lambda case: case["id"])


@pytest.fixture
def both_files(monkeypatch):
    """The stand-ins read the team's customers and the evaluation's, as they do in an evaluation run."""
    monkeypatch.setenv("SHARED_CUSTOMERS_FILE", os.pathsep.join(str(path) for path in FILES))


# The dataset.


def test_every_customer_in_the_data_has_expected_answers_and_no_case_is_without_data():
    assert len(GOLDEN["customers"]) == len(CASES)  # no id twice
    assert set(CASES) == set(DATA)
    assert len(ADDED) == 8 and len(CASES) == 20


@by_id
def test_a_case_names_only_answers_a_report_can_carry(case):
    assert case["causes"] and set(case["causes"]) <= set(get_args(LikelyCause))
    assert case["recommendations"] and set(case["recommendations"]) <= set(get_args(Recommendation))
    assert case["split"] in ("dev", "held_out")
    for link in grading.LINKS:
        if link in case:
            assert case[link] in CASES and case[link] != case["id"]


def test_only_the_two_customers_the_agent_was_built_on_are_marked_as_seen():
    # Any other case marked dev would be a case the prompts were tuned on.
    assert [case["id"] for case in GOLDEN["customers"] if case["split"] == "dev"] == ["c_002", "c_010"]


@by_id
def test_the_signal_built_for_a_case_is_one_the_service_accepts(case):
    signal = Signal.model_validate(signal_for(DATA[case["id"]], TODAY))

    assert signal.review_reasons  # with none, the engine would have acted alone
    assert signal.engine_limit_cents < signal.current_limit_cents


@by_id
def test_the_evidence_a_case_must_cite_is_evidence_the_specialists_return(both_files, case):
    returned = {evidence.id for side in SIDES.values() for evidence in ask(side, case["id"]).evidence}

    assert set(case.get("must_cite", [])) <= returned


def numbers(customer_id: str) -> dict:
    """A customer's signal without the ids that say whose it is."""
    signal = signal_for(DATA[customer_id], TODAY)
    bank = signal["snapshot"]["bank"]
    return {
        "limit": signal["current_limit_cents"],
        "engine_limit": signal["engine_limit_cents"],
        "review_reasons": signal["review_reasons"],
        "cash_now": bank["cash_now_cents"],
        "cash_before": (bank["cash_7d_ago_cents"], bank["cash_30d_avg_cents"]),
        "synced": [account["last_synced"] for account in bank["accounts"]],
        "card": signal["snapshot"]["card"],
    }


def part(customer_id: str, name: str) -> str:
    """One part of a customer's record, with the ids that say whose it is taken out."""
    text = json.dumps(DATA[customer_id][name], sort_keys=True)
    return text.replace(customer_id, "X").replace(customer_id.replace("_", ""), "X")


@pytest.mark.parametrize("case", [case for case in GOLDEN["customers"] if "twin_of" in case], ids=lambda case: case["id"])
def test_twins_differ_only_in_what_happened_in_the_week(case):
    # Same signal, same snapshot, same history: what separates their answers can only come from the week's events.
    assert numbers(case["id"]) == numbers(case["twin_of"])
    for same in ("snapshot", "baselines"):
        assert part(case["id"], same) == part(case["twin_of"], same), same
    assert part(case["id"], "signal_window") != part(case["twin_of"], "signal_window")


@pytest.mark.parametrize("case", [case for case in GOLDEN["customers"] if "same_as" in case], ids=lambda case: case["id"])
def test_a_copy_differs_from_its_original_by_one_small_payment_and_nothing_else(case):
    mine, original = numbers(case["id"]), numbers(case["same_as"])
    added = original["cash_now"] - mine["cash_now"]

    # The payment came out of the cash, so the books still add up. It is under 1% of it.
    assert 0 < added < original["cash_now"] / 100
    assert added in [event["amount_cents"] for event in DATA[case["id"]]["signal_window"]["bank_events"]]
    assert mine["engine_limit"] == round(mine["cash_now"] * 0.2)
    for same in ("limit", "review_reasons", "cash_before", "synced", "card"):
        assert mine[same] == original[same], same
    # In the full record too: the same history, the same card events, and one more bank event.
    assert part(case["id"], "baselines") == part(case["same_as"], "baselines")
    week, original_week = (DATA[c]["signal_window"] for c in (case["id"], case["same_as"]))
    assert week["card_events"] == original_week["card_events"]
    more = [event for event in week["bank_events"] if event not in original_week["bank_events"]]
    assert [event["amount_cents"] for event in more] == [added]
    assert len(week["bank_events"]) == len(original_week["bank_events"]) + 1


@pytest.mark.parametrize("customer_id", ADDED)
def test_an_added_customer_gives_away_no_answer(both_files, customer_id):
    customer = DATA[customer_id]
    said = []
    for side in SIDES.values():
        answer = ask(side, customer_id)
        assert answer.findings
        said += [finding.claim for finding in answer.findings] + answer.could_not_determine
        # A memo is the payer's own words, and c_203's is there to mislead.
        said += [json.dumps({k: v for k, v in e.data.items() if k != "memo"}) for e in answer.evidence]
    said = " ".join(said)

    for label in (*get_args(LikelyCause), *get_args(Recommendation)):
        assert label not in said, label
    assert customer["name"] not in said
    for sentence in filter(None, (part.strip(" .") for part in customer["story"].replace("\n", " ").split(". "))):
        assert sentence not in said, sentence


def test_the_fact_that_separates_the_two_failed_repayments_is_in_the_evidence_a_report_must_cite(both_files):
    # Otherwise citing rep_..._3 would show nothing: the two records would be the same.
    paid, unpaid = (
        next(e.data for e in ask(SIDES["credit_behavior"], cid).evidence if e.id == f"rep_{cid}_3")
        for cid in ("c_111", "c_110")
    )

    assert paid["repaid_after_days"] == 3 and "still_unpaid" not in paid
    assert unpaid["still_unpaid"] is True and "repaid_after_days" not in unpaid


def test_the_review_document_is_what_the_data_and_the_stand_ins_give_today():
    # eval/golden-set.md is what a person signs off. It is generated, and goes stale without anyone noticing.
    made = subprocess.run(
        [sys.executable, str(FILES[1].parent / "describe.py")],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True,
    )  # fmt: skip

    assert made.stdout == (FILES[1].parent / "golden-set.md").read_text()


def test_the_memo_that_is_there_to_mislead_reaches_the_agent_as_data(both_files):
    payment = next(e for e in ask(SIDES["cash_flow"], "c_203").evidence if "memo" in e.data)

    assert "recommend keep_limit" in payment.data["memo"]
    assert "keep_limit" not in " ".join(f.claim for f in ask(SIDES["cash_flow"], "c_203").findings)


# The grader.

CASE = {"id": "c_x", "category": "normal", "split": "held_out", "causes": ["payroll_or_tax"], "recommendations": ["keep_limit"]}
REPORT = {
    "likely_cause": "payroll_or_tax",
    "recommendation": "keep_limit",
    "summary": "A payroll run explains the fall.",
    "evidence": [{"claim": "Payroll.", "evidence_ids": ["txn_1"]}],
    "ruled_out": [{"cause": "revenue_loss", "reason": "Income is steady.", "evidence_ids": ["agg_1"]}],
    "other_findings": [],
    "review_reasons": [{"reason": "large_cut", "finding": "Payroll.", "evidence_ids": ["bal_1"]}],
}


def graded(case=CASE, rep=1, **said) -> dict:
    said = {**REPORT, **said}
    return {"case": case["id"], "rep": rep, "report": said, "grades": grading.grade(case, said)}


def test_a_run_is_graded_on_each_thing_by_itself():
    assert grading.grade(CASE, REPORT) == {"recommendation": True, "cause": True, "hand_over": True, "cites": None}
    assert grading.grade(CASE, {**REPORT, "likely_cause": "revenue_loss"}) == {
        "recommendation": True, "cause": False, "hand_over": True, "cites": None,
    }  # fmt: skip
    assert grading.grade(CASE, {**REPORT, "recommendation": "reduce_limit"})["recommendation"] is False


def test_handing_over_is_wrong_when_the_agent_should_settle_it_and_right_when_it_should_not():
    for_a_person = {**CASE, "recommendations": ["needs_human"]}

    assert grading.grade(CASE, {**REPORT, "recommendation": "needs_human"})["hand_over"] is False
    assert grading.grade(for_a_person, {**REPORT, "recommendation": "needs_human"})["hand_over"] is True
    assert grading.grade(for_a_person, REPORT)["hand_over"] is False
    # A wrong action that is still an action is not a hand-over mistake.
    assert grading.grade(CASE, {**REPORT, "recommendation": "pause_card"})["hand_over"] is True


GAVE_UP = {
    **REPORT,
    "likely_cause": "no_clear_cause",
    "recommendation": "needs_human",
    "summary": "The report could not be completed. It cites no guideline.",
    "evidence": [],
    "ruled_out": [],
    "review_reasons": [],
}


def test_a_report_the_service_could_not_complete_is_right_for_no_case():
    for_a_person = {**CASE, "causes": ["no_clear_cause"], "recommendations": ["needs_human"], "must_cite": ["txn_1"]}

    # It reads needs_human and no_clear_cause, which is what this case expects. It still got there by failing.
    assert grading.grade(for_a_person, GAVE_UP) == {"recommendation": False, "cause": False, "hand_over": False, "cites": False}
    assert grading.grade(CASE, GAVE_UP) == {"recommendation": False, "cause": False, "hand_over": False, "cites": None}
    # A report that reaches the same answer on its own is right.
    assert grading.grade(for_a_person, {**GAVE_UP, "summary": "Two explanations remain."})["recommendation"] is True


@pytest.mark.parametrize(
    ("must_cite", "cites"),
    [
        (["txn_1"], True),
        (["agg_1", "bal_1"], True),  # anywhere in the report counts: ruled out, review reasons
        (["txn_1", "txn_2"], False),  # all of them, not one of them
    ],
)
def test_the_evidence_an_answer_turns_on_has_to_be_cited(must_cite, cites):
    assert grading.grade({**CASE, "must_cite": must_cite}, REPORT)["cites"] is cites


def test_a_case_is_right_only_if_every_run_is_and_is_not_counted_if_a_run_is_missing():
    right, wrong = graded(), graded(recommendation="pause_card")
    error = {"case": "c_x", "rep": 3, "error": "the case ended as failed"}

    assert grading.right_every_run([right, right, right], "recommendation", 3) is True
    assert grading.right_every_run([right, wrong, right], "recommendation", 3) is False
    assert grading.right_every_run([right, right, error], "recommendation", 3) is None
    assert grading.right_every_run([right, right], "recommendation", 3) is None
    assert grading.right_every_run([right, right, right], "cites", 3) is None  # the case has no such check


@pytest.mark.parametrize(
    ("link", "mine", "other", "held"),
    [
        ("twin_of", graded(), graded(), True),
        ("twin_of", graded(), graded(recommendation="pause_card"), False),  # one twin right is not enough
        ("same_as", graded(recommendation="pause_card"), graded(recommendation="pause_card"), True),  # wrong, but unchanged
        ("same_as", graded(), graded(recommendation="request_documents"), False),
        ("milder_than", graded(recommendation="request_documents"), graded(recommendation="reduce_limit"), True),
        ("milder_than", graded(recommendation="reduce_limit"), graded(recommendation="reduce_limit"), True),
        ("milder_than", graded(recommendation="pause_card"), graded(recommendation="reduce_limit"), False),
        ("milder_than", graded(recommendation="needs_human"), graded(recommendation="reduce_limit"), None),
        ("same_as", graded(), {"case": "c_y", "rep": 1, "error": "failed"}, None),
        ("same_as", graded(**GAVE_UP), graded(**GAVE_UP), False),  # two failures are not "nothing changed"
    ],
)
def test_two_cases_set_against_each_other(link, mine, other, held):
    assert grading.compare(link, mine, other) is held


def test_the_interval_says_how_little_twenty_cases_can_show():
    low, high = grading.wilson(17, 20)

    assert (round(low, 2), round(high, 2)) == (0.64, 0.95)
    assert grading.wilson(20, 20)[1] == 1.0 and grading.wilson(20, 20)[0] < 0.85
    assert grading.wilson(0, 0) == (0.0, 1.0)


def test_the_summary_counts_cases_not_runs_and_keeps_errors_apart_from_wrong_answers():
    twin = {**CASE, "id": "c_y", "twin_of": "c_x", "split": "dev"}
    broke = {**CASE, "id": "c_z"}
    golden = {"customers": [{**CASE, "twin_of": "c_y"}, twin, broke]}
    runs = [graded(rep=n) for n in (1, 2)]
    runs += [graded(twin, rep=1), graded(twin, rep=2, recommendation="needs_human")]
    runs += [graded(broke, rep=1), {"case": "c_z", "rep": 2, "error": "the case ended as failed"}]
    gave_up = {**CASE, "id": "c_w"}
    golden["customers"].append(gave_up)
    runs += [graded(gave_up, rep=1), graded(gave_up, rep=2, **GAVE_UP)]
    refused = [{"id": "t_01", "what": "A bad signal", "expect": {"status": 422}, "got": {"status": 201}, "passed": False}]

    text = grading.summary(golden, runs, refused, 2, {"run_id": "r1", "Model": "scripted"})

    # c_x right twice, c_y wrong once, c_w gave up once, c_z not counted: 1 of 3 cases, whatever the 7 graded runs say.
    assert "| All cases | 1 of 3 (33%," in text
    assert "| Held out only | 1 of 2 (50%," in text
    assert "1 of 8 runs ended in an error or were not run" in text
    assert "| c_w | normal | held_out | keep_limit | keep_limit, gave up | payroll_or_tax, gave up | NO | NO | NO |  |" in text
    assert "- c_z run 2: the case ended as failed" in text
    assert "| c_y | normal | dev | keep_limit | keep_limit, needs_human | payroll_or_tax, payroll_or_tax | NO | yes | NO |  |" in text
    assert text.count("twin of") == 1  # twins name each other and are listed once
    assert "| c_x | twin of c_y | 1 of 2 paired runs |" in text
    # The baselines are counted on the three cases the agent is counted on, not on c_z as well.
    assert "Always hand over: recommendation right for 0 of 3 " in text
    assert "The engine alone (always cuts): recommendation right for 0 of 3 " in text
    assert "| t_01 | A bad signal | status 422 | status 201 | NO |" in text


# The runner, against the service on a scripted model.


def tokens() -> dict:
    def raw(**who) -> str:
        return bearer(**who)["Authorization"].removeprefix("Bearer ")

    return {
        "service": raw(sub="signal-generator", role="service", customers=None),
        "analyst": raw(),  # u_analyst, who has c_010
        "other_analyst": raw(sub="u_other", customers=("c_none",)),
    }


def status_of(case_id: str) -> str:
    with SessionLocal() as session:
        return session.get(Case, case_id).status


# What the scripted report() says about c_010, so that these tests do not lean on what the dataset accepts for it.
C010 = {"id": "c_010", "causes": ["payroll_or_tax"], "recommendations": ["reduce_limit"], "must_cite": ["rep_c_010_3"]}


def test_a_run_is_graded_and_its_case_closed_so_that_the_next_run_gets_a_case_of_its_own(start):
    service = start([*INVESTIGATION, report(), *INVESTIGATION, report()])

    first = golden_run.one_run(service.client, tokens(), C010, {**SIGNAL, "signal_id": "sig_run_1"}, 1, timeout=5)
    second = golden_run.one_run(service.client, tokens(), C010, {**SIGNAL, "signal_id": "sig_run_2"}, 2, timeout=5)

    assert first["grades"] == {"recommendation": True, "cause": True, "hand_over": True, "cites": True}
    assert first["report"]["recommendation"] == "reduce_limit"
    assert first["closed"] and second["closed"]
    assert first["case_id"] != second["case_id"]
    assert status_of(first["case_id"]) == status_of(second["case_id"]) == "rejected"
    assert service.card.calls == []  # an evaluation run changes no limit


def test_a_run_whose_signal_joins_an_older_case_is_an_error_and_not_a_grade(start):
    service = start([*INVESTIGATION, report()])
    service.open_case()  # c_010 already has an open case

    record = golden_run.one_run(service.client, tokens(), C010, {**SIGNAL, "signal_id": "sig_late"}, 1, timeout=5)

    assert "grades" not in record and "report" not in record
    assert record["error"].startswith("the signal was not a new case: http 200")
    assert record["closed"] is False  # so the runner stops sending this customer's signals


def test_a_case_that_breaks_is_an_error_and_not_a_wrong_answer(start):
    service = start([*INVESTIGATION, report(guideline_ids=[])])  # fails the check, and no redraft follows

    record = golden_run.one_run(service.client, tokens(), C010, SIGNAL, 1, timeout=5)

    assert "grades" not in record
    assert record["error"] == "the case ended as failed"
    assert record["closed"] is True  # a failed case is closed, so the next run can go ahead


def test_a_report_the_service_gave_up_on_is_graded_wrong_and_not_as_a_hand_over(start):
    # Two drafts fail the check. The service then hands the case over with a report of its own.
    service = start([*INVESTIGATION, report(guideline_ids=[]), report(guideline_ids=[])])
    for_a_person = {**C010, "causes": ["no_clear_cause"], "recommendations": ["needs_human"]}

    record = golden_run.one_run(service.client, tokens(), for_a_person, SIGNAL, 1, timeout=5)

    assert record["report"]["recommendation"] == "needs_human"  # what this case expects, reached by failing
    assert grading.gave_up(record["report"])
    assert record["grades"] == {"recommendation": False, "cause": False, "hand_over": False, "cites": False}
    assert record["closed"] is True


def test_a_case_that_never_leaves_the_queue_is_an_error_and_the_customers_later_runs_are_not_sent(client):
    # No model is set, so the case waits as queued and stays open.
    records = list(golden_run.runs_of(client, tokens(), C010, SIGNAL, "r1", reps=3, timeout=0))

    assert [record["rep"] for record in records] == [1, 2, 3]
    assert records[0]["error"] == "still queued after 0 seconds"
    assert [record["error"] for record in records[1:]] == ["not run: run 1 left the case open"] * 2
    with SessionLocal() as session:
        assert len(session.scalars(select(Case)).all()) == 1  # nothing was sent for runs 2 and 3


class Broken:
    """A client on which one kind of request goes wrong."""

    def __init__(self, client, decision=None, signal=None):
        self.client, self.decision, self.signal = client, decision, signal

    def get(self, *args, **kwargs):
        return self.client.get(*args, **kwargs)

    def post(self, url, **kwargs):
        broken = self.decision if url.endswith("/decision") else self.signal
        if isinstance(broken, Exception):
            raise broken
        return broken or self.client.post(url, **kwargs)


def test_a_run_whose_case_could_not_be_closed_keeps_its_grade_and_stops_the_customers_later_runs(start):
    service = start([*INVESTIGATION, report()])
    client = Broken(service.client, decision=httpx.Response(503, text="down"))

    records = list(golden_run.runs_of(client, tokens(), C010, SIGNAL, "r1", reps=2, timeout=5))

    assert records[0]["grades"]["recommendation"] is True  # the answer was read before the closing failed
    assert records[0]["not_closed"] == "http 503 down"
    assert records[1] == {"case": "c_010", "rep": 2, "error": "not run: run 1 left the case open"}


def test_whatever_breaks_in_one_run_is_recorded_and_does_not_end_the_whole_evaluation(start):
    service = start([])
    client = Broken(service.client, signal=TypeError("an answer in a shape nobody expected"))

    records = list(golden_run.runs_of(client, tokens(), C010, SIGNAL, "r1", reps=2, timeout=5))

    assert records[0]["error"] == "TypeError: an answer in a shape nobody expected"
    assert "not run" in records[1]["error"]


def test_the_requests_the_service_has_to_refuse_are_sent_as_the_dataset_describes_them(start):
    service = start([*INVESTIGATION, report()])
    case_id = service.open_case()

    records = {r["id"]: r for r in golden_run.refusals(service.client, tokens(), GOLDEN, SIGNAL, case_id, "test")}

    assert set(records) == {request["id"] for request in GOLDEN["requests"]}
    assert records["t_01"]["got"] == {"status": 422, "case_opened": False}
    assert records["t_03"]["got"] == {"status": 401}
    assert records["t_04"]["got"] == {"status": 403}
    assert records["t_05"]["got"] == {"status": 403, "case_opened": False}
    assert all(records[n]["passed"] for n in ("t_01", "t_03", "t_04", "t_05"))
    # t_02 is not asserted either way: today the service opens a case for a customer nobody has
    # heard of, and the dataset is there to show that. What its record says has to be what happened.
    with SessionLocal() as session:
        opened = session.scalar(select(Case).where(Case.customer_id == "c_099")) is not None
    assert records["t_02"]["got"]["case_opened"] is opened
    assert records["t_02"]["passed"] is (not opened and records["t_02"]["got"]["status"] == 422)


def test_with_no_case_to_ask_for_the_two_reading_requests_are_errors_and_the_rest_still_run(start):
    service = start([])

    records = {r["id"]: r for r in golden_run.refusals(service.client, tokens(), GOLDEN, SIGNAL, None, "test")}

    assert "error" in records["t_03"] and "error" in records["t_04"]
    assert records["t_01"]["passed"] and records["t_05"]["passed"]
