"""The application log (contracts.logs): one JSON line per event, carrying the request and the case it belongs to,
and never the text a model or a customer wrote."""

import io
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app import runner
from app.agents import mock
from app.agents.cash_flow import agent as cash_flow_agent
from app.agents.cash_flow import cash_flow
from app.agents.cash_flow import findings as cash_flow_findings
from app.agents.cash_flow.agent import _run_tool
from app.db import engine
from contracts import Finding
from contracts.logs import JsonFormatter, RequestLog, bind
from tests._bank_standin import BankStandin
from tests.test_cash_flow_regressions import ask
from tests.test_graph import BANK_ONLY, DONE, FROM_THE_FACTS, INVESTIGATION, Run, ask_bank, call, gaps, report
from tests.test_graph_flows import Searches, search

log = logging.getLogger("test")

# A customer's name from the test data: what must never reach a log line.
NAME = "Northwind Robotics"


@pytest.fixture
def lines():
    """The JSON lines logged during the test, as dicts."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("test"))
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # Not the test client's own lines: httpx logs each request it sends.
    yield lambda: [line for line in map(json.loads, stream.getvalue().splitlines())
                   if not line["logger"].startswith("httpx")]  # fmt: skip
    root.removeHandler(handler)
    root.setLevel(level)


def client() -> TestClient:
    app = FastAPI()
    app.add_middleware(RequestLog)

    @app.get("/cases/{case_id}")
    def read(case_id: str, background: BackgroundTasks):
        log.info("reading")
        background.add_task(log.info, "after the response")
        return {}

    @app.get("/health")
    def health():
        return {}

    @app.get("/broken")
    def broken():
        raise RuntimeError("broken")

    return TestClient(app, raise_server_exceptions=False)


def test_the_request_line_carries_the_gateways_id_and_the_case_and_every_line_logged_meanwhile_carries_the_id(lines):
    client().get("/cases/case_1", headers={"X-Request-ID": "rid-1"})

    logged = lines()
    by = {line["message"]: line for line in logged}
    request = by["GET /cases/case_1 200"]
    assert {key: request[key] for key in ("method", "path", "status", "request_id", "case_id")} == {
        "method": "GET", "path": "/cases/case_1", "status": 200, "request_id": "rid-1", "case_id": "case_1",
    }  # fmt: skip
    assert by["reading"]["request_id"] == by["after the response"]["request_id"] == "rid-1"
    # Written once the response is sent, so a background task (a case run) does not count in its duration.
    assert logged.index(request) < logged.index(by["after the response"])


def test_a_call_that_did_not_come_through_the_gateway_gets_an_id_of_its_own(lines):
    web = client()
    web.get("/cases/case_1")
    web.get("/cases/case_1")

    first, second = (line["request_id"] for line in lines() if line["logger"] == "request")
    assert first and second and first != second


def test_a_passing_health_check_is_not_logged(lines):
    client().get("/health")

    assert lines() == []


def test_a_request_that_raised_is_logged_once_as_a_500_error(lines):
    client().get("/broken", headers={"X-Request-ID": "rid-2"})

    requests = [line for line in lines() if line["logger"] == "request"]
    assert [(line["status"], line["level"], line["request_id"]) for line in requests] == [(500, "ERROR", "rid-2")]


def test_a_bound_case_is_gone_once_its_block_ends_even_in_a_thread_that_is_used_again(lines):
    # Cases run on the event loop's thread pool, whose threads keep their context from one case to the next.
    def run():
        with bind(case_id="case_1"):
            log.info("in the case")

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(run).result()
        pool.submit(log.info, "after it").result()

    by = {line["message"]: line for line in lines()}
    assert by["in the case"]["case_id"] == "case_1"
    assert "case_id" not in by["after it"]


def test_every_line_of_a_case_run_carries_the_case(lines):
    class Broken:
        def get_state(self, config):
            raise RuntimeError("the checkpointer is gone")

    runner.run_case(Broken(), "case_1")

    logged = lines()
    assert logged and all(line["case_id"] == "case_1" for line in logged)


def invalid_answer(request):
    return {"agent": "cash_flow", "findings": NAME}


@pytest.mark.parametrize(
    ("run", "logged"),
    [
        pytest.param(lambda: Run([*INVESTIGATION, report(likely_cause=NAME), report()]),
                     "draft 1 is not a report: likely_cause", id="a draft that is not a report"),
        pytest.param(lambda: Run([*INVESTIGATION[:-1], call("GapReview", gaps=NAME), report()]),
                     "the gap review gave no usable answer", id="a gap review that is not one"),
        pytest.param(lambda: Run([search(), ask_bank("Where did the money go?"), DONE, gaps(), report(**BANK_ONLY)],
                                 search_reviews=Searches(raises=RuntimeError(NAME))),
                     "the search gave no usable answer: RuntimeError", id="a search that raised"),
        pytest.param(lambda: Run([ask_bank("Where did the money go?"), DONE, gaps(), report(**FROM_THE_FACTS)],
                                 specialists={"cash_flow": invalid_answer, "credit_behavior": mock.credit_behavior}),
                     "cash_flow gave no usable answer: ", id="a specialist answer that is not one"),
    ],
)  # fmt: skip
def test_a_failure_is_logged_by_what_failed_never_by_the_text_that_did_not_fit(caplog, run, logged):
    run()

    assert logged in caplog.text
    assert NAME not in caplog.text


def test_a_cash_flow_tool_that_raised_is_logged_by_the_kind_of_error_never_its_message(caplog):
    class Raising:
        def invoke(self, args, config):
            raise ValueError(f"customer_name {NAME} is not an argument")

    answer = _run_tool(Raising(), "get_cash_summary", {"customer_name": NAME}, RunnableConfig())

    assert NAME in answer["message"]  # the model is told; Langfuse keeps it
    assert "cash_flow tool get_cash_summary raised ValueError" in caplog.text
    assert NAME not in caplog.text


def test_a_cash_flow_run_whose_answer_did_not_fit_is_logged_by_the_kind_of_error_never_the_answer(caplog, monkeypatch):
    class Unfit:
        def invoke(self, *args, **kwargs):
            return {"response": {"agent": "cash_flow", "findings": NAME}}

    monkeypatch.setattr(cash_flow_agent, "_GRAPH", Unfit())
    cash_flow(ask("c_010", "reconciliation"), model=None, http=BankStandin().client())

    assert "cash_flow graph failed: ValidationError" in caplog.text
    assert NAME not in caplog.text


def test_a_finding_code_could_not_write_is_logged_by_the_kind_of_error_never_the_claim(caplog, monkeypatch):
    def unfit(record):
        return Finding(claim=NAME, confidence="certain", evidence_ids=[record["id"]])

    monkeypatch.setitem(cash_flow_findings._WRITERS, "daily_balance", unfit)

    assert cash_flow_findings.write({"id": "bal_c_010", "kind": "daily_balance"}, {}) is None
    assert "cash_flow could not write a finding for bal_c_010: ValidationError" in caplog.text
    assert NAME not in caplog.text


class Answer(BaseModel):
    findings: list[str]


def test_a_traceback_names_the_fields_a_validation_error_failed_on_never_the_input_it_quotes(lines):
    # Chained, as `raise ... from` leaves it: the cause is cleaned too, and the outer error keeps its message.
    try:
        try:
            Answer.model_validate({"findings": NAME})
        except ValidationError as invalid:
            raise RuntimeError("the answer did not fit") from invalid
    except RuntimeError:
        log.exception("the run failed")

    (line,) = lines()
    assert "findings: list_type" in line["error"] and "RuntimeError: the answer did not fit" in line["error"]
    assert NAME not in line["error"]


def test_a_traceback_keeps_the_message_of_any_other_error(lines):
    try:
        raise ConnectionError("failed to resolve host 'postgres'")
    except ConnectionError:
        log.exception("the request failed")

    assert "ConnectionError: failed to resolve host 'postgres'" in lines()[0]["error"]


def test_a_failed_database_call_does_not_quote_what_it_carried():
    with pytest.raises(DBAPIError) as failed, engine.connect() as connection:
        connection.execute(text("SELECT * FROM no_such_table WHERE note = :note"), {"note": NAME})

    assert NAME not in str(failed.value)
