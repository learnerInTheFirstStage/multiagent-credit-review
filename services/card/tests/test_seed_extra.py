"""Seeding the team's file and the evaluation's extra customers together.

SHARED_CUSTOMERS_YAML takes a colon-separated list. Expected numbers come from the two files, so
the tests follow them when they change.
"""

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from app.api import evidence as ev
from app.db import Base, engine
from app.db.models import CardTransaction, Customer, Repayment
from app.db.seed import (
    FALLBACK_PROFILE,
    PROFILES,
    generate,
    load,
    profile_for,
    seed,
    seed_if_empty,
)
from app.ledger import load_book, repayment_status
from tests.conftest import bearer, mint_token
from tests.test_seed import _balances

ROOT = Path(__file__).resolve().parents[3]
TEAM_FILE = ROOT / "data" / "shared_customers.yaml"
EXTRA_FILE = ROOT / "eval" / "extra_customers.yaml"
BOTH = f"{TEAM_FILE}:{EXTRA_FILE}"

DATA = load(BOTH)
AS_OF = date.fromisoformat(DATA["clock"]["signal_date"])
CUSTOMERS = {c["id"]: c for c in DATA["customers"]}
STATUS = {"paid": "COMPLETED", "late": "COMPLETED", "failed": "RETURNED"}
DUES = [date(2026, 9, 5), date(2026, 10, 5), date(2026, 11, 5)]


@pytest.fixture
def seeded(session, monkeypatch):
    monkeypatch.setenv("SHARED_CUSTOMERS_YAML", BOTH)
    seed(session)
    return session


def _events(c: dict) -> dict:
    return {e["kind"]: e for e in c["signal_window"].get("card_events") or []}


def _statements(session, cid: str) -> list[Repayment]:
    """One row per statement, the first by (due_date, repayment_id): the scheduled debit, not a
    later payment toward the same statement."""
    rows = session.scalars(select(Repayment).where(Repayment.customer_id == cid)
                           .order_by(Repayment.due_date, Repayment.repayment_id)).all()
    seen, out = set(), []
    for r in rows:
        if r.statement_period_end not in seen:
            seen.add(r.statement_period_end)
            out.append(r)
    return out


# --- loading -----------------------------------------------------------------------------------------


def test_the_two_files_load_with_no_id_twice():
    assert len(CUSTOMERS) == len(DATA["customers"])


def test_one_path_still_loads_the_team_file_alone(monkeypatch):
    monkeypatch.delenv("SHARED_CUSTOMERS_YAML", raising=False)
    assert len(load()["customers"]) == 12
    monkeypatch.setenv("SHARED_CUSTOMERS_YAML", str(TEAM_FILE))
    assert len(load()["customers"]) == 12


def test_a_customer_in_two_files_is_an_error_naming_both(tmp_path):
    dup = tmp_path / "dup.yaml"
    team = yaml.safe_load(TEAM_FILE.read_text())
    dup.write_text(yaml.safe_dump({**team, "customers": [team["customers"][0]]}))
    with pytest.raises(ValueError, match="c_001") as raised:
        load(f"{TEAM_FILE}:{dup}")
    assert str(TEAM_FILE) in str(raised.value) and str(dup) in str(raised.value)


def test_files_with_different_clocks_are_an_error(tmp_path):
    other = tmp_path / "other.yaml"
    extra = yaml.safe_load(EXTRA_FILE.read_text())
    other.write_text(yaml.safe_dump({**extra, "clock": {**extra["clock"], "signal_date": "2026-12-01"}}))
    with pytest.raises(ValueError, match="clock"):
        load(f"{TEAM_FILE}:{other}")


# --- profiles ----------------------------------------------------------------------------------------


def test_every_extra_customer_takes_its_team_twins_profile():
    twins = {
        "c_102": "c_002", "c_103": "c_003", "c_203": "c_003", "c_303": "c_003", "c_106": "c_006",
        "c_107": "c_007", "c_307": "c_007", "c_110": "c_010", "c_111": "c_010", "c_201": "c_001",
        "c_301": "c_001", "c_208": "c_008", "c_304": "c_004", "c_314": "c_004",
    }
    assert set(twins) == set(CUSTOMERS) - set(PROFILES)
    for cid, twin in twins.items():
        assert profile_for(cid) is PROFILES[twin], cid
    # c_013, twin of no team customer, has a profile of its own and not the fallback.
    assert "c_013" in CUSTOMERS and profile_for("c_013") is PROFILES["c_013"] is not FALLBACK_PROFILE


def test_an_unknown_customer_gets_the_fallback_profile_not_a_key_error():
    stranger = {**CUSTOMERS["c_201"], "id": "c_999", "name": "Stranger Co"}
    assert profile_for("c_999") is FALLBACK_PROFILE
    rows = generate(stranger, AS_OF)
    assert rows["customers"][0]["industry"] == FALLBACK_PROFILE.industry
    assert generate(stranger, AS_OF) == rows


# --- the 27 customers against the files -------------------------------------------------------------


def test_every_customer_in_both_files_is_seeded(seeded):
    assert set(seeded.scalars(select(Customer.customer_id)).all()) == set(CUSTOMERS)


def test_limits_and_signal_date_balances_match_the_files(seeded):
    for cid, c in CUSTOMERS.items():
        card = c["snapshot"]["card"]
        assert seeded.get(Customer, cid).credit_limit_cents == card["credit_limit_cents"], cid
        assert _balances(seeded, cid)[AS_OF] == round(card["credit_limit_cents"] * card["utilization_pct"]), cid


def test_the_balance_never_passes_the_limit(seeded):
    for cid, c in CUSTOMERS.items():
        assert max(_balances(seeded, cid).values()) <= c["snapshot"]["card"]["credit_limit_cents"], cid


def test_last_three_statements_have_the_files_statuses_and_event_amounts(seeded):
    for cid, c in CUSTOMERS.items():
        statuses = c["snapshot"]["card"]["last_3_repayments"]
        last = _statements(seeded, cid)[-3:]
        assert [r.due_date for r in last] == DUES, cid
        assert [r.status for r in last] == [STATUS[s] for s in statuses], cid
        # As the signal reads the file: only the latest repayment takes its event's amount.
        event = _events(c).get(f"{statuses[-1]}_repayment")
        if event:
            assert last[-1].amount_cents == event["amount_cents"], cid


def test_the_paying_account_follows_the_files_funding_change(seeded):
    for cid, c in CUSTOMERS.items():
        change = _events(c).get("funding_account_change")
        customer = seeded.get(Customer, cid)
        last = _statements(seeded, cid)[-3:]
        if change:
            assert [r.funding_account_id for r in last] == [change["from"], change["to"], change["to"]], cid
            assert customer.funding_account_id == change["to"], cid
        else:
            account = c["snapshot"]["bank"]["linked_accounts"][0]["id"]
            used = set(seeded.scalars(select(Repayment.funding_account_id).where(Repayment.customer_id == cid)))
            assert used == {account} and customer.funding_account_id == account, cid
    assert {cid for cid, c in CUSTOMERS.items() if _events(c).get("funding_account_change")} == {"c_006", "c_106"}


def test_only_customers_with_a_utilization_trend_are_declined_at_the_limit(seeded):
    declined = set(seeded.scalars(
        select(CardTransaction.customer_id).where(CardTransaction.decline_reason == "AUTHORIZER_BUSINESS_LIMIT")
    ))
    assert declined == {cid for cid, c in CUSTOMERS.items() if "utilization_trend" in c["baselines"]["card"]}


# --- a failed repayment, paid since or not ---------------------------------------------------------


def test_c110_failed_repayment_is_still_unpaid(seeded):
    book = load_book(seeded, "c_110")
    rep = book.due_by(AS_OF)[-1]
    assert (rep.status, rep.amount_cents) == ("RETURNED", 5_000_000)
    assert [r for r in book.repayments if r.statement_period_end == rep.statement_period_end] == [rep]
    data = ev.repayment(book, rep, AS_OF)["data"]
    assert (data["status"], data["paid_since"]) == ("failed", "no")
    assert "repaid_after_days" not in data


def test_c111_failed_repayment_was_paid_after_the_files_days(seeded):
    days = _events(CUSTOMERS["c_111"])["failed_repayment"]["repaid_after_days"]
    book = load_book(seeded, "c_111")
    rep = book.due_by(AS_OF)[-1]
    assert (rep.repayment_id, rep.status, rep.amount_cents) == ("rep_c_111_202611", "RETURNED", 5_000_000)
    data = ev.repayment(book, rep, AS_OF)["data"]
    assert (data["status"], data["paid_since"], data["repaid_after_days"]) == ("failed", "yes", days)

    retry = seeded.get(Repayment, "rep_c_111_202611_r1")
    assert retry.status == "COMPLETED" and retry.amount_cents == rep.amount_cents
    assert retry.statement_period_end == rep.statement_period_end and retry.due_date == rep.due_date
    assert retry.settled_at.date() == rep.returned_on + timedelta(days=days) <= AS_OF
    # The repaid statement is no longer owed: the balance fell by its amount that day.
    balances = _balances(seeded, "c_111")
    paid_on = retry.settled_at.date()
    spent_that_day = sum(amount for when, amount in seeded.execute(
        select(CardTransaction.authorized_at, CardTransaction.amount_cents)
        .where(CardTransaction.customer_id == "c_111", CardTransaction.state != "DECLINED")) if when.date() == paid_on)
    assert balances[paid_on] == balances[paid_on - timedelta(days=1)] + spent_that_day - retry.amount_cents


def test_a_payment_toward_a_returned_statement_is_not_a_repayment_of_its_own(seeded):
    """due_by lists statements: c_111's last three are still paid, paid, failed, not paid, failed, late."""
    book = load_book(seeded, "c_111")
    last = book.due_by(AS_OF)[-3:]
    assert [r.due_date for r in last] == DUES
    assert [repayment_status(r, AS_OF) for r in last] == ["paid", "paid", "failed"]


def test_repaid_after_the_signal_date_is_an_error():
    c111 = CUSTOMERS["c_111"]
    late = {**c111, "signal_window": {**c111["signal_window"], "card_events": [
        {"kind": "failed_repayment", "amount_cents": 5_000_000, "repaid_after_days": 30}]}}
    with pytest.raises(ValueError, match="after the signal date"):
        generate(late, AS_OF)


def test_an_event_both_unpaid_and_repaid_is_an_error():
    c111 = CUSTOMERS["c_111"]
    both = {**c111, "signal_window": {**c111["signal_window"], "card_events": [
        {"kind": "failed_repayment", "amount_cents": 5_000_000, "repaid_after_days": 3, "still_unpaid": True}]}}
    with pytest.raises(ValueError, match="still_unpaid"):
        generate(both, AS_OF)


# --- determinism and restart -----------------------------------------------------------------------


def test_generation_is_deterministic_for_every_customer():
    for c in CUSTOMERS.values():
        assert generate(c, AS_OF) == generate(c, AS_OF), c["id"]


def test_seed_if_empty_with_both_files_is_idempotent(session, monkeypatch):
    monkeypatch.setenv("SHARED_CUSTOMERS_YAML", BOTH)
    first = seed_if_empty(session)
    assert first is not None and first["customers"] == len(CUSTOMERS)
    assert seed_if_empty(session) is None
    assert set(session.scalars(select(Customer.customer_id)).all()) == set(CUSTOMERS)


# --- through the tools -----------------------------------------------------------------------------


@pytest.fixture
def both_client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("SHARED_CUSTOMERS_YAML", BOTH)
    Base.metadata.drop_all(engine)
    with TestClient(app) as client:
        yield client


def _tool(client, cid: str, tool: str, **params) -> dict:
    headers = bearer(mint_token(sub="card_service:case_t", customers=[cid]))
    response = client.get(f"/customers/{cid}/tools/{tool}", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_the_repayments_tool_reads_c111_as_failed_then_paid(both_client):
    reps = _tool(both_client, "c_111", "repayments", count=3)["repayments"]
    assert [r["status"] for r in reps] == ["paid", "paid", "failed"]
    assert (reps[-1]["paid_since"], reps[-1]["repaid_after_days"]) == ("yes", 3)
    unpaid = _tool(both_client, "c_110", "repayments", count=3)["repayments"]
    assert [r["status"] for r in unpaid] == ["paid", "paid", "failed"] and unpaid[-1]["paid_since"] == "no"


def test_spend_by_cycle_lists_each_cycle_once(both_client):
    cycles = _tool(both_client, "c_111", "spend_by_cycle", cycles=6)["cycles"]
    ends = [c["end"] for c in cycles]
    assert len(ends) == len(set(ends))
