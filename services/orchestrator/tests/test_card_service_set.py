"""The Card Service agent's test set (KAN-29) holds, offline, and its grader catches what it must.

The set lives in eval/card_service, outside this service, like the golden set. It is tested
here because it runs this service's agent. Card's answers come from eval/card_service/recorded.json,
recorded from the seeded stack with `run.py --no-model --record`.
"""

import copy
import importlib.util
from pathlib import Path

import httpx
import pytest

from tests._cash_flow_helpers import ScriptedChatModel

SET = Path(__file__).resolve().parents[3] / "eval" / "card_service"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load("card_service_run", SET / "run.py")
checks = runner.checks
CASES, WINDOW = runner.load_cases()
BY_ID = {case["id"]: case for case in CASES}
STORE = __import__("json").loads(runner.RECORDED.read_text())


def answer_of(case_id: str, model=None) -> tuple[dict, dict]:
    response, returned, problems = runner.run_case(BY_ID[case_id], WINDOW, store=STORE, replay=True, model=model)
    assert problems == []  # every call to Card was answered from the recording
    return response, returned


def failed(case_id: str, response: dict, returned: dict) -> set[str]:
    graded = checks.grade(BY_ID[case_id], response, returned)
    return {name for name, why in graded["checks"].items() if why}


# --- the set passes -------------------------------------------------------------------------------


def test_the_set_covers_what_the_ticket_asks():
    ids = set(BY_ID)
    assert {"cb_01_normal", "cb_02_no_data", "cb_03_card_down"} <= ids  # normal vs no data
    assert {"cb_04_new_paying_account", "cb_05_new_paying_account_unasked"} <= ids  # c_006
    assert {"cb_07_failed_repayment", "cb_08_failed_repayment_unasked"} <= ids  # c_010
    assert {c["customer"] for c in CASES} >= {f"c_{i:03d}" for i in range(1, 13)}  # every scenario customer


@pytest.mark.parametrize("case_id", sorted(BY_ID))
def test_every_case_passes_without_a_model(case_id):
    response, returned = answer_of(case_id)
    graded = checks.grade(BY_ID[case_id], response, returned)
    assert graded["passed"], {k: v for k, v in graded["checks"].items() if v}


def test_a_model_that_skips_the_failed_repayment_still_passes():
    # The model writes no findings at all. Code adds the failed repayment, so cb_07's answer holds.
    model = ScriptedChatModel(script=[{"findings": [], "could_not_determine": [], "suggested_next": None}])
    response, returned = answer_of("cb_07_failed_repayment", model=model)
    assert failed("cb_07_failed_repayment", response, returned) == set()


# --- the grader catches a bad answer ------------------------------------------------------------


@pytest.fixture
def c010():
    return answer_of("cb_07_failed_repayment")


def test_a_record_no_tool_returned_is_caught(c010):
    response, returned = copy.deepcopy(c010)
    response["evidence"].append({"id": "rep_c_010_209901", "kind": "card_repayment", "data": {"status": "paid"}})
    response["findings"].append({"claim": "Paid.", "confidence": "high", "evidence_ids": ["rep_c_010_209901"]})
    assert "cites_returned" in failed("cb_07_failed_repayment", response, returned)


def test_a_record_changed_after_the_tool_returned_it_is_caught(c010):
    response, returned = copy.deepcopy(c010)
    record = next(e for e in response["evidence"] if e["id"] == "rep_c_010_202611")
    record["data"]["paid_since"] = "yes"
    assert "cites_returned" in failed("cb_07_failed_repayment", response, returned)


def test_a_figure_not_in_the_records_is_caught(c010):
    response, returned = copy.deepcopy(c010)
    response["findings"][-1]["claim"] = "The $55,000 repayment due 2026-11-05 failed."
    # Only the figure: the date before "failed" is not a count.
    assert failed("cb_07_failed_repayment", response, returned) == {"figures_from_records"}


@pytest.mark.parametrize("claim", [
    "The $50,000 repayment due 2026-11-05 failed.",
    "The $50,000 repayment due on Nov 5 failed.",
    "The $50,000 repayment due 11/05 failed.",
    "Of the last repayments, 1 of 3 failed.",
    "Of the last repayments, 1 of the 3 failed.",
])
def test_a_date_or_a_total_is_not_read_as_a_count(c010, claim):
    response, returned = copy.deepcopy(c010)
    finding = next(f for f in response["findings"] if f["evidence_ids"] == ["rep_c_010_202611"])
    finding["claim"] = claim
    assert failed("cb_07_failed_repayment", response, returned) == set()


def test_n_of_m_is_read_as_n(c010):
    response, returned = copy.deepcopy(c010)
    finding = next(f for f in response["findings"] if f["evidence_ids"] == ["rep_c_010_202611"])
    finding["claim"] = "Of the last repayments, 3 of 3 failed."
    assert failed("cb_07_failed_repayment", response, returned) == {"counts_from_records"}


def test_a_count_not_in_the_records_is_caught(c010):
    response, returned = copy.deepcopy(c010)
    summary = next(f for f in response["findings"] if "were paid on time" in f["claim"])
    summary["claim"] = summary["claim"].replace("2 were paid on time", "3 were paid on time").replace("1 failed", "0 failed")
    assert "counts_from_records" in failed("cb_07_failed_repayment", response, returned)


def test_a_recommendation_is_caught(c010):
    response, returned = copy.deepcopy(c010)
    response["suggested_next"] = "Pause the card until it is paid."
    assert "no_action" in failed("cb_07_failed_repayment", response, returned)


def test_calling_no_data_normal_is_caught():
    normal, _ = answer_of("cb_01_normal")
    _, returned = answer_of("cb_02_no_data")
    # c_001's "all paid" findings handed back for a customer Card does not hold.
    assert {"no_findings", "cites_returned"} <= failed("cb_02_no_data", normal, returned)


def test_missing_the_paying_account_change_is_caught():
    response, returned = answer_of("cb_05_new_paying_account_unasked")
    response["findings"] = [f for f in response["findings"] if "changed from" not in f["claim"]]
    assert "cites_1_funding_account_change" in failed("cb_05_new_paying_account_unasked", response, returned)


def test_a_failed_repayment_reported_as_paid_since_is_caught(c010):
    response, returned = copy.deepcopy(c010)
    for item in (response["evidence"], list(returned.values())):
        for record in item:
            if record["id"] == "rep_c_010_202611":
                record["data"]["paid_since"] = "yes"
    assert "cites_1_card_repayment" in failed("cb_07_failed_repayment", response, returned)


# --- the wording of a claim holds to the records it cites ------------------------------------------


@pytest.mark.parametrize("claim", [
    "The $50,000 repayment due 2026-11-05 bounced but was paid two days later.",
    "The $50,000 repayment due 2026-11-05 failed. It will probably be paid soon; nothing to worry about.",
    "The $50,000 repayment due 2026-11-05 failed, but it has been paid since.",
])
def test_an_unpaid_repayment_said_to_be_paid_is_caught(c010, claim):
    response, returned = copy.deepcopy(c010)
    finding = next(f for f in response["findings"] if f["evidence_ids"] == ["rep_c_010_202611"])
    finding["claim"] = claim
    assert failed("cb_07_failed_repayment", response, returned) == {"wording_from_records"}


@pytest.mark.parametrize("claim", [
    "The $50,000 repayment due 2026-11-05 failed. It has not been paid since.",
    "The $50,000 repayment due 2026-11-05 failed and hasn't been paid since.",
    "The $50,000 repayment due 2026-11-05 failed; it was never paid afterwards.",
])
def test_an_unpaid_repayment_said_to_be_unpaid_passes(c010, claim):
    response, returned = copy.deepcopy(c010)
    finding = next(f for f in response["findings"] if f["evidence_ids"] == ["rep_c_010_202611"])
    finding["claim"] = claim
    assert failed("cb_07_failed_repayment", response, returned) == set()


@pytest.mark.parametrize(("case_id", "claim", "want"), [
    ("cb_11_spiking_use", "The card is at 92% of the limit, which is normal for this customer.",
     {"wording_from_records"}),
    ("cb_11_spiking_use", "The card is at 92% of the limit, which is not normal for this customer.", set()),
    ("cb_11_spiking_use", "The card is at 92% of the limit, against its usual 47%.", set()),
    ("cb_11_spiking_use", "The card is at 92% of the limit, against a typical 47%.", set()),
    ("cb_11_spiking_use", "Utilization shows a steady climb to 92% of the limit.", set()),
    ("cb_11_spiking_use", "The card is at 92% of the limit, consistent with the spiking trend.", set()),
    ("cb_10_rising_use", "The card is at 85% of the limit, a steady level.", {"wording_from_records"}),
    # Found on a model run: a comparison with the customer's usual share, not a claim of normal.
    ("cb_10_rising_use", "The customer's usual share is 41%, so 85% is unusual against its own past.", set()),
])
def test_a_rising_or_spiking_balance_called_normal_is_caught(case_id, claim, want):
    response, returned = answer_of(case_id)
    finding = next(f for f in response["findings"] if f["evidence_ids"][0].startswith("bal_card_"))
    finding["claim"] = claim
    assert failed(case_id, response, returned) == want


# --- the runner -------------------------------------------------------------------------------------


def test_a_call_with_no_recording_fails_the_case_on_replay():
    # Replayed, a call Card never answered must not pass as "Card did not answer".
    case = {"id": "unrecorded", "customer": "c_099", "topic": "repayments", "question": "Repaid on time?",
            "expect": {"findings": "none", "says": ["did not answer"]}}
    [result] = runner.run(replay=True, cases=[case])
    assert not result["passed"]
    assert "no recording for c_099/repayments" in result["checks"]["card_calls"]


def test_a_model_call_with_no_recording_fails_the_case_on_replay():
    # The model asks for 6 repayments; only the preflight's 3 are recorded.
    model = ScriptedChatModel(script=[[("card_repayments", {"count": 6})],
                                      {"findings": [], "could_not_determine": [], "suggested_next": None}])
    _, _, problems = runner.run_case(BY_ID["cb_07_failed_repayment"], WINDOW, store=STORE, replay=True, model=model)
    assert problems == ["no recording for c_010/repayments?count=6"]


def test_a_card_answer_that_is_not_json_is_a_failed_recording():
    store: dict = {}
    transport = runner.Recording(store=store, replay=False, down=False)
    html_500 = httpx.Response(500, text="<html>Internal Server Error</html>")
    transport.inner = httpx.MockTransport(lambda request: html_500)
    with httpx.Client(transport=transport) as http:
        response = http.get("http://card/customers/c_010/tools/repayments", params={"count": 3})
    assert response.status_code == 500 and "Internal Server Error" in response.text
    assert store == {}
    assert transport.problems == ["c_010/repayments?count=3: Card answered 500 with a body that is not JSON"]


def test_an_unknown_topic_is_answered_without_calling_card():
    response, returned = answer_of("cb_19_unknown_topic")
    assert returned == {}
    assert failed("cb_19_unknown_topic", response, returned) == set()
