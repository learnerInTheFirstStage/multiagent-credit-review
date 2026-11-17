"""The Credit Behavior test set (KAN-29) holds, offline, and its grader catches what it must.

The set lives in eval/credit_behavior, outside this service, like the golden set. It is tested
here because it runs this service's agent. Card's answers come from eval/credit_behavior/recorded.json,
recorded from the seeded stack with `run.py --no-model --record`.
"""

import copy
import importlib.util
from pathlib import Path

import pytest

from tests._cash_flow_helpers import ScriptedChatModel

SET = Path(__file__).resolve().parents[3] / "eval" / "credit_behavior"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load("credit_behavior_run", SET / "run.py")
checks = runner.checks
CASES, WINDOW = runner.load_cases()
BY_ID = {case["id"]: case for case in CASES}
STORE = __import__("json").loads(runner.RECORDED.read_text())


def answer_of(case_id: str, model=None) -> tuple[dict, dict]:
    return runner.run_case(BY_ID[case_id], WINDOW, store=STORE, replay=True, model=model)


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
    # The model writes only the balance. Code adds the failed repayment, so cb_07's answer holds.
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
    response["findings"][-1]["claim"] = "The repayment due 2026-11-05 for $55,000 failed."
    assert "figures_from_records" in failed("cb_07_failed_repayment", response, returned)


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
