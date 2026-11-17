from typing import get_args

import pytest
from pydantic import ValidationError

from app import guidelines, runner
from app.graph.report import Recommendation
from app.guidelines import GUIDELINES, Guideline
from tests import test_tracing
from tests.test_graph import INVESTIGATION, ScriptedModel, report, scripted
from tests.test_signals import SIGNAL  # c_010
from tests.test_tracing import service_graph


def test_there_are_fourteen_guidelines_numbered_in_order():
    assert [guideline.id for guideline in GUIDELINES] == [f"G-{n:02d}" for n in range(1, 15)]


def test_every_action_a_report_can_recommend_is_allowed_by_some_guideline():
    # An action no guideline allows could never pass the report check.
    allowed = {action for guideline in GUIDELINES for action in guideline.allows}

    assert allowed == set(get_args(Recommendation))


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param({"allows": ["raise_limit"]}, id="an action that does not exist"),
        pytest.param({"allows": []}, id="no action at all"),
        pytest.param({"id": "G-1"}, id="an id in another form"),
        pytest.param({"when": ""}, id="no condition"),
    ],
)
def test_a_guideline_that_code_could_not_check_cannot_be_written(broken):
    good = GUIDELINES[0].model_dump()

    with pytest.raises(ValidationError):
        Guideline.model_validate({**good, **broken})


def test_loading_gives_a_copy_that_a_caller_cannot_spoil():
    first = guidelines.load()
    first[0]["allows"].append("pause_card")
    first.pop()

    again = guidelines.load()

    assert len(again) == 14
    assert again[0]["allows"] == ["keep_limit"]
    assert set(again[0]) == {"id", "title", "when", "allows", "note"}


@pytest.fixture
def shown(monkeypatch) -> list[str]:
    """What the model is shown on each call, with the running service on a scripted model."""
    seen = []

    class Recording(ScriptedModel):
        def _generate(self, messages, *args, **kwargs):
            seen.append(messages[-1].content)
            return super()._generate(messages, *args, **kwargs)

    monkeypatch.setattr(test_tracing, "scripted", lambda script: scripted(script, Recording))
    return seen


@pytest.mark.parametrize(
    ("first_draft", "told"),
    [
        pytest.param(
            report(guideline_ids=["G-04"]),
            "No guideline it cites allows reduce_limit.",
            id="G-04 allows keeping the limit only",
        ),
        pytest.param(
            report(recommendation="pause_card", guideline_ids=["G-12", "G-08"]),
            "It cites G-12, under which a person decides, so it may only recommend needs_human.",
            id="G-12 leaves the decision to a person, whatever G-08 allows",
        ),
    ],
)
def test_the_running_service_checks_a_report_against_these_guidelines(monkeypatch, shown, first_draft, told):
    allowed = report(recommendation="keep_limit", guideline_ids=["G-04"])

    with service_graph(monkeypatch, [*INVESTIGATION, first_draft, allowed]) as graph:
        state = graph.invoke({"case_id": "case_guided", "signal": SIGNAL}, runner.thread("case_guided"))

    assert state["drafts"] == 2
    assert told in shown[-1]  # the redraft was refused for this reason and no other
    assert shown[-1].count("\n- ") == 1
    assert state["check"] == {"passed": True, "problems": []}
    assert state["report"]["recommendation"] == "keep_limit"
    assert [guideline["id"] for guideline in state["guidelines"]] == [guideline.id for guideline in GUIDELINES]


def test_the_report_writer_is_shown_when_each_guideline_applies_and_what_it_allows(monkeypatch, shown):
    nothing_to_do = report(likely_cause="no_clear_cause", recommendation="needs_human", guideline_ids=[], evidence=[])

    with service_graph(monkeypatch, [*INVESTIGATION, nothing_to_do]) as graph:
        graph.invoke({"case_id": "case_shown", "signal": SIGNAL}, runner.thread("case_shown"))

    brief = shown[-1]  # the last call to the model is the report
    assert "A transfer to the customer's own unlinked account explains the fall" in brief
    assert "Ask the customer to link the account." in brief
    assert '"G-14"' in brief
    assert '"allows": [\n      "keep_limit"\n    ]' in brief
