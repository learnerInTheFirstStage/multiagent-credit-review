"""Seed-level tests. Run seed() against the real data/shared_customers.yaml.

Expected numbers come from that file, so the tests follow it when it changes.
"""

import copy
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db.models import CardTransaction, CreditLimit, Customer, Repayment
from app.db.seed import HISTORY_CAP_SHARE, SPEND_JITTER, cycles, generate, load, seed, seed_if_empty

ROOT = Path(__file__).resolve().parents[3]
BOTH_FILES = f"{ROOT / 'data' / 'shared_customers.yaml'}:{ROOT / 'eval' / 'extra_customers.yaml'}"
DATA = load()
AS_OF = date.fromisoformat(DATA["clock"]["signal_date"])
CUSTOMERS = {c["id"]: c for c in DATA["customers"]}
ALL_CUSTOMERS = {c["id"]: c for c in load(BOTH_FILES)["customers"]}
STATUS = {"paid": "COMPLETED", "late": "COMPLETED", "failed": "RETURNED"}


@pytest.fixture
def seeded(session):
    seed(session)
    return session


@pytest.fixture
def seeded_all(session):
    """Every customer in the team's file and the evaluation's."""
    seed(session, BOTH_FILES)
    return session


def _balances(session, cid: str) -> dict[date, int]:
    """End-of-day balance: authorized and not declined, less completed repayments."""
    spent: dict[date, int] = {}
    for when, amount in session.execute(
        select(CardTransaction.authorized_at, CardTransaction.amount_cents)
        .where(CardTransaction.customer_id == cid, CardTransaction.state != "DECLINED")
    ):
        spent[when.date()] = spent.get(when.date(), 0) + amount
    paid: dict[date, int] = {}
    for when, amount in session.execute(
        select(Repayment.settled_at, Repayment.amount_cents)
        .where(Repayment.customer_id == cid, Repayment.status == "COMPLETED")
    ):
        paid[when.date()] = paid.get(when.date(), 0) + amount
    out, balance, day = {}, 0, min(spent)
    while day <= AS_OF:
        balance += spent.get(day, 0) - paid.get(day, 0)
        out[day] = balance
        day += timedelta(days=1)
    return out


def _last_three(session, cid: str) -> list[Repayment]:
    rows = session.scalars(select(Repayment).where(Repayment.customer_id == cid).order_by(Repayment.due_date)).all()
    return rows[-3:]


def test_every_customer_in_the_file_is_seeded(seeded):
    ids = set(seeded.scalars(select(Customer.customer_id)).all())
    assert ids == set(CUSTOMERS)


def test_limits_match_the_file(seeded):
    for cid, c in CUSTOMERS.items():
        customer = seeded.get(Customer, cid)
        assert customer.credit_limit_cents == c["snapshot"]["card"]["credit_limit_cents"], cid
        history = seeded.scalars(select(CreditLimit.limit_cents).where(CreditLimit.customer_id == cid)).all()
        assert history == [customer.credit_limit_cents], cid


def test_signal_date_balance_gives_the_files_utilization(seeded):
    for cid, c in CUSTOMERS.items():
        card = c["snapshot"]["card"]
        expected = round(card["credit_limit_cents"] * card["utilization_pct"])
        assert _balances(seeded, cid)[AS_OF] == expected, cid


def test_the_balance_never_passes_the_limit(seeded):
    for cid, c in CUSTOMERS.items():
        limit = c["snapshot"]["card"]["credit_limit_cents"]
        assert max(_balances(seeded, cid).values()) <= limit, cid


def test_last_three_repayments_are_due_on_the_5th_with_the_files_statuses(seeded):
    dues = [date(2026, 9, 5), date(2026, 10, 5), date(2026, 11, 5)]
    for cid, c in CUSTOMERS.items():
        statuses = c["snapshot"]["card"]["last_3_repayments"]
        last = _last_three(seeded, cid)
        assert [r.due_date for r in last] == dues, cid
        assert [r.status for r in last] == [STATUS[s] for s in statuses], cid


def test_a_paid_repayment_settles_on_its_due_date_and_a_late_one_after(seeded):
    for cid, c in CUSTOMERS.items():
        for status, rep in zip(c["snapshot"]["card"]["last_3_repayments"], _last_three(seeded, cid), strict=True):
            if status == "paid":
                assert rep.settled_at.date() == rep.due_date, cid
            elif status == "late":
                assert rep.settled_at.date() > rep.due_date, cid
                assert rep.payment_method == "ach_manual", cid


def test_c003_late_repayment_has_the_files_amount(seeded):
    rep = _last_three(seeded, "c_003")[-1]
    assert (rep.status, rep.amount_cents) == ("COMPLETED", 4_000_000)
    assert rep.due_date < rep.settled_at.date() <= AS_OF


def test_c010_failed_repayment_is_returned_for_insufficient_funds_and_still_owed(seeded):
    rep = _last_three(seeded, "c_010")[-1]
    assert (rep.status, rep.ach_return_code, rep.amount_cents) == ("RETURNED", "R01", 5_000_000)
    assert rep.settled_at is None and rep.returned_at.date() > rep.due_date
    # Nothing has paid it since: no later repayment row exists.
    later = seeded.scalar(select(func.count()).select_from(Repayment)
                          .where(Repayment.customer_id == "c_010", Repayment.due_date > rep.due_date))
    assert later == 0


def test_c006_pays_from_a_new_account_for_the_last_two_repayments(seeded):
    last = _last_three(seeded, "c_006")
    assert [r.funding_account_id for r in last] == ["acct_c006_a", "acct_external_new_c006", "acct_external_new_c006"]
    customer = seeded.get(Customer, "c_006")
    assert customer.funding_account_id == "acct_external_new_c006"
    assert last[0].due_date < customer.funding_account_since < last[1].due_date


def test_every_other_customer_pays_from_its_first_bank_account(seeded):
    for cid, c in CUSTOMERS.items():
        if cid == "c_006":
            continue
        account = c["snapshot"]["bank"]["linked_accounts"][0]["id"]
        used = set(seeded.scalars(select(Repayment.funding_account_id).where(Repayment.customer_id == cid)).all())
        assert used == {account}, cid
        assert seeded.get(Customer, cid).funding_account_id == account, cid


def test_a_statement_is_the_sum_of_its_cycles_spend(seeded):
    for cid in CUSTOMERS:
        for rep in seeded.scalars(select(Repayment).where(Repayment.customer_id == cid)):
            spent = seeded.scalar(
                select(func.sum(CardTransaction.amount_cents)).where(
                    CardTransaction.customer_id == cid,
                    CardTransaction.state != "DECLINED",
                    func.date(CardTransaction.authorized_at) >= rep.statement_period_start.isoformat(),
                    func.date(CardTransaction.authorized_at) <= rep.statement_period_end.isoformat(),
                )
            )
            assert spent == rep.statement_balance_cents == rep.amount_cents, (cid, rep.repayment_id)


def test_the_ledger_invariants_hold_for_all_26_customers(seeded_all):
    for cid, c in ALL_CUSTOMERS.items():
        card = c["snapshot"]["card"]
        balances = _balances(seeded_all, cid)
        assert balances[AS_OF] == round(card["credit_limit_cents"] * card["utilization_pct"]), cid
        assert max(balances.values()) <= card["credit_limit_cents"], cid
        for rep in seeded_all.scalars(select(Repayment).where(Repayment.customer_id == cid)):
            spent = seeded_all.scalar(
                select(func.sum(CardTransaction.amount_cents)).where(
                    CardTransaction.customer_id == cid,
                    CardTransaction.state != "DECLINED",
                    func.date(CardTransaction.authorized_at) >= rep.statement_period_start.isoformat(),
                    func.date(CardTransaction.authorized_at) <= rep.statement_period_end.isoformat(),
                )
            )
            assert spent == rep.statement_balance_cents == rep.amount_cents, (cid, rep.repayment_id)


def test_older_cycles_spend_the_files_monthly_give_or_take_the_jitter(seeded_all):
    for cid, c in ALL_CUSTOMERS.items():
        monthly = c["baselines"]["card"]["monthly_card_spend_cents"]
        # The cycles before the last three, which no event moves.
        rows = seeded_all.scalars(select(Repayment).where(Repayment.customer_id == cid, ~Repayment.repayment_id.like("%_r1"))
                                  .order_by(Repayment.due_date)).all()
        for rep in rows[:-3]:
            assert abs(rep.amount_cents - monthly) <= SPEND_JITTER * monthly + 1, (cid, rep.repayment_id)


def test_the_last_three_statements_are_exactly_the_files_amounts(seeded_all):
    # The signal's rule: the monthly spend, except a late or failed last one, which has its event's amount.
    for cid, c in ALL_CUSTOMERS.items():
        monthly = c["baselines"]["card"]["monthly_card_spend_cents"]
        statuses = c["snapshot"]["card"]["last_3_repayments"]
        events = {e["kind"]: e for e in c["signal_window"].get("card_events") or []}
        expected = [monthly] * 3
        if f"{statuses[-1]}_repayment" in events:
            expected[-1] = events[f"{statuses[-1]}_repayment"]["amount_cents"]
        rows = seeded_all.scalars(select(Repayment).where(Repayment.customer_id == cid, ~Repayment.repayment_id.like("%_r1"))
                                  .order_by(Repayment.due_date)).all()
        assert [r.amount_cents for r in rows[-3:]] == expected, cid


def test_an_event_for_a_repayment_that_is_not_the_latest_is_an_error():
    # The signal gives an event's amount to the latest repayment only. An event about an earlier one (its amount,
    # repaid_after_days, still_unpaid) has nowhere to go, and is refused rather than dropped.
    for cid, statuses in (("c_010", ["paid", "failed", "paid"]), ("c_111", ["failed", "paid", "paid"]),
                          ("c_003", ["late", "paid", "paid"])):
        c = copy.deepcopy(ALL_CUSTOMERS[cid])
        c["snapshot"]["card"]["last_3_repayments"] = statuses
        with pytest.raises(ValueError) as error:
            generate(c, AS_OF)
        assert cid in str(error.value) and "latest" in str(error.value), cid


def test_an_earlier_late_or_failed_repayment_without_an_event_is_the_monthly_spend():
    c = copy.deepcopy(CUSTOMERS["c_003"])
    c["snapshot"]["card"]["last_3_repayments"] = ["failed", "paid", "late"]
    monthly = c["baselines"]["card"]["monthly_card_spend_cents"]

    repayments = generate(c, AS_OF)["repayments"][-3:]

    assert [(r["status"], r["amount_cents"]) for r in repayments] == [
        ("RETURNED", monthly), ("COMPLETED", monthly), ("COMPLETED", 4_000_000),
    ]


def test_every_customer_spends_within_the_share_of_its_limit_a_ledger_can_carry():
    for cid, c in ALL_CUSTOMERS.items():
        card = c["snapshot"]["card"]
        assert c["baselines"]["card"]["monthly_card_spend_cents"] <= HISTORY_CAP_SHARE * card["credit_limit_cents"], cid


def test_a_monthly_spend_too_high_for_the_limit_is_an_error_not_a_silent_cap():
    c = copy.deepcopy(CUSTOMERS["c_001"])
    limit = c["snapshot"]["card"]["credit_limit_cents"]
    c["baselines"]["card"]["monthly_card_spend_cents"] = limit // 2
    with pytest.raises(ValueError) as error:
        generate(c, AS_OF)
    message = str(error.value)
    assert "c_001" in message and str(limit // 2) in message and str(limit) in message


def test_only_the_stressed_customers_are_declined_at_the_limit(seeded):
    declined = set(seeded.scalars(
        select(CardTransaction.customer_id).where(CardTransaction.decline_reason == "AUTHORIZER_BUSINESS_LIMIT")
    ).all())
    assert declined == {cid for cid, c in CUSTOMERS.items() if "utilization_trend" in c["baselines"]["card"]}
    assert declined == {"c_003", "c_010"}


def test_history_reaches_400_days_back(seeded):
    closed, _ = cycles(AS_OF)
    assert (AS_OF - closed[0][0]).days >= 400
    first = seeded.scalar(select(func.min(CardTransaction.authorized_at)))
    assert (AS_OF - first.date()).days >= 400


def test_only_recent_purchases_are_pending_and_nothing_is_after_the_signal_date(seeded):
    latest = seeded.scalar(select(func.max(CardTransaction.authorized_at)))
    assert latest.date() <= AS_OF
    # A purchase settles one or two days later, so only the last three days can still be pending.
    pending_days = set(seeded.scalars(
        select(func.date(CardTransaction.authorized_at)).where(CardTransaction.state == "PENDING")
    ).all())
    assert pending_days <= {(AS_OF - timedelta(days=k)).isoformat() for k in range(3)}
    assert (AS_OF - timedelta(days=1)).isoformat() in pending_days


def test_ids_are_unique_and_carry_the_card_prefixes(seeded):
    txn_ids = seeded.scalars(select(CardTransaction.txn_id)).all()
    assert len(txn_ids) == len(set(txn_ids))
    assert all(t.startswith("ctx_c_") for t in txn_ids)
    rep_ids = seeded.scalars(select(Repayment.repayment_id)).all()
    assert all(r.startswith("rep_c_") for r in rep_ids)


def test_generation_is_deterministic():
    c = CUSTOMERS["c_003"]
    assert generate(c, AS_OF) == generate(c, AS_OF)


def test_seed_if_empty_is_idempotent(session):
    first = seed_if_empty(session)
    second = seed_if_empty(session)
    assert first is not None and first["customers"] == len(CUSTOMERS)
    assert second is None
    assert session.scalar(select(func.count()).select_from(Customer)) == len(CUSTOMERS)


def test_health_after_start(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "card"
