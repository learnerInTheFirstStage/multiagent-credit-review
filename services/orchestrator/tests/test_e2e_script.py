"""scripts/e2e.py on a stack that is faked here: no stack, no model, no Card.

The script must never print PASS for a run whose Bank or Card gave nothing. A case that waits for one is shown with
the reason while the script waits, and the run fails with it on timeout; a case that failed for one fails the run
with the reason; and a path page that cannot be read stops the run before anything is decided.
"""

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import e2e  # noqa: E402

WAITING = {
    "service": "Bank Service", "codes": "http_503", "kind": "transient", "attempts": 1,
    "since": "2026-11-18T10:00:00Z", "next_retry_at": "2026-11-18T10:01:00Z",
    "message": "Bank Service unavailable (http_503) since 2026-11-18T10:00Z, retried 0 times, next retry at 2026-11-18T10:01Z",
}
AGAIN = {
    **WAITING, "attempts": 2, "next_retry_at": "2026-11-18T10:06:00Z",
    "message": "Bank Service unavailable (http_503) since 2026-11-18T10:00Z, retried 1 time, next retry at 2026-11-18T10:06Z",
}
GAVE_UP = {
    **WAITING, "attempts": 4, "next_retry_at": None,
    "message": "Bank Service unavailable (http_503) since 2026-11-18T10:00Z, retried 3 times",
}


def case_page(status: str, outage: dict | None = None, **more) -> dict:
    return {"case_id": "case_1", "customer_id": "c_010", "status": status, "waiting_for": None, "report": None,
            "decisions": [], "action": None, "outage": outage, **more}


class Clock:
    """time for the script: sleeping moves the clock on at once."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Pages:
    """The Orchestrator's case page, one answer per read, the last one repeated."""

    def __init__(self, *pages: dict):
        self.pages = list(pages)

    def get(self, url, **kwargs):
        page = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        return httpx.Response(200, json=page)


def test_while_a_case_waits_for_bank_or_card_the_script_says_why_and_fails_with_it_on_timeout(monkeypatch, capsys):
    monkeypatch.setattr(e2e, "time", Clock())
    pages = Pages(case_page("investigating"), case_page("waiting_for_data", WAITING),
                  case_page("waiting_for_data", WAITING), case_page("waiting_for_data", AGAIN))

    with pytest.raises(e2e.Failed) as failed:
        e2e.wait(pages, "case_1", "a-token", timeout=9, project="e2e")

    assert failed.value.code == 2
    assert AGAIN["message"] in str(failed.value) and "case_1" in str(failed.value)
    printed = capsys.readouterr().out
    assert "case_1 is waiting_for_data" in printed
    # Each reason once, as it changes.
    assert printed.count(WAITING["message"]) == 1 and printed.count(AGAIN["message"]) == 1


def test_a_case_that_waited_and_then_went_on_is_waited_for(monkeypatch):
    monkeypatch.setattr(e2e, "time", Clock())
    ready = case_page("needs_approval", waiting_for="analyst", report={"recommendation": "keep_limit"})
    pages = Pages(case_page("waiting_for_data", WAITING), case_page("investigating", WAITING), ready)

    assert e2e.wait(pages, "case_1", "a-token", timeout=60, project="e2e") == ready


def test_a_case_that_failed_for_an_outage_fails_the_run_with_the_reason(monkeypatch):
    monkeypatch.setattr(e2e, "time", Clock())

    with pytest.raises(e2e.Failed) as failed:
        e2e.wait(Pages(case_page("failed", GAVE_UP)), "case_1", "a-token", timeout=60, project="e2e")

    assert failed.value.code == 1
    assert GAVE_UP["message"] in str(failed.value)


# The whole run, up to the decision


class Stack:
    """The Orchestrator and Card as the script reads them: an investigated case, and a path page that answers 500."""

    def __init__(self, snapshot: dict, ready: dict):
        self.snapshot, self.ready = snapshot, ready
        self.posted: list[str] = []

    def get(self, url, params=None, headers=None):
        if url.endswith("/snapshots"):
            return httpx.Response(200, json=self.snapshot)
        if url == "/cases":
            return httpx.Response(200, json={"cases": [], "total": 0})
        if url.endswith("/path"):
            return httpx.Response(500, json={"error": "internal_server_error", "message": "The service failed"})
        return httpx.Response(200, json=self.ready)

    def post(self, url, json=None, headers=None):
        self.posted.append(url)
        if url == "/signals":
            return httpx.Response(201, json={"case_id": "case_1", "status": "queued", "result": "opened"})
        return httpx.Response(200, json={**self.ready, "status": "done", "waiting_for": None})

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_path_page_that_cannot_be_read_stops_the_run_before_anything_is_decided(monkeypatch):
    today, data = e2e.customers()
    signal = e2e.signal_for(data["c_010"], today)
    snapshot = {"as_of": "2026-11-10", "customers": [{
        "customer_id": "c_010", "card_status": "active", "credit_limit_cents": signal["current_limit_cents"],
        "balance_cents": 0, "funding_account_id": "acct_1",
    }]}
    hand_over = {
        "recommendation": "needs_human", "likely_cause": "no_clear_cause", "confidence": "low", "guideline_ids": [],
        "engine_limit_cents": signal["engine_limit_cents"], "summary": "Nothing was found.", "evidence": [],
        "review_reasons": [], "ruled_out": [], "other_findings": [], "next_action": None,
    }
    stack = Stack(snapshot, case_page("needs_approval", waiting_for="analyst", report=hand_over))
    monkeypatch.setattr(e2e, "httpx", SimpleNamespace(**{**vars(httpx), "Client": lambda **_: stack}))
    args = argparse.Namespace(customer="c_010", project="e2e", orchestrator_url="http://orchestrator",
                              card_url="http://card", container="", card_container=None, decide="approve", timeout=5)

    with pytest.raises(e2e.Failed) as failed:
        e2e.run(args)

    assert "path" in str(failed.value) and "500" in str(failed.value)
    assert stack.posted == ["/signals"]  # no decision went out
