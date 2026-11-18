"""The Card analysis tools (KAN-25), on the seeded scenario customers and one new customer."""

from datetime import UTC, date, datetime, timedelta

import pytest

from app.db import SessionLocal
from app.db.models import CardTransaction, CreditLimit, Customer
from app.db.seed import load
from tests.conftest import bearer, mint_token

DATA = load()
CUSTOMERS = {c["id"]: c for c in DATA["customers"]}
AS_OF = date.fromisoformat(DATA["clock"]["signal_date"])
NEW = "c_099"  # three weeks of card history, no statement yet


@pytest.fixture(scope="module")
def tools(seeded_client):
    _add_new_customer()

    def call(customer_id: str, tool: str, token_for: str | None = None, **params):
        headers = bearer(mint_token(sub="card_service:case_t", customers=[token_for or customer_id]))
        return seeded_client.get(f"/customers/{customer_id}/tools/{tool}", params=params, headers=headers)

    return call


def _add_new_customer() -> None:
    onboarded = AS_OF - timedelta(days=21)
    with SessionLocal() as session:
        session.add(Customer(
            customer_id=NEW, name="New Co", industry="software", onboarded_on=onboarded,
            credit_limit_cents=2_000_000, funding_account_id="acct_c099_a", funding_account_since=onboarded,
            card_feed_updated_at=datetime(2026, 11, 10, 6, tzinfo=UTC),
            payments_feed_updated_at=datetime(2026, 11, 10, 6, tzinfo=UTC),
        ))
        session.flush()
        session.add(CreditLimit(customer_id=NEW, effective_on=onboarded, limit_cents=2_000_000, reason="initial"))
        for k in range(20):
            when = datetime.combine(onboarded + timedelta(days=k), datetime.min.time(), tzinfo=UTC) + timedelta(hours=10)
            session.add(CardTransaction(
                txn_id=f"ctx_{NEW}_{k:05d}", customer_id=NEW, card_id="crd_c_099_1", card_last4="0001",
                card_nickname="Founders card", state="CLEARED", authorized_at=when, cleared_at=when + timedelta(days=1),
                amount_cents=40_000, merchant_amount_cents=40_000, merchant_id="mer_1", merchant_name="Digi-Key",
                merchant_descriptor="DIGI-KEY ELECTRONICS", mcc="5065", mcc_description="Electrical Parts",
                spend_category="Electronic components",
            ))
        session.commit()


def _ids(body: dict) -> set[str]:
    return {e["id"] for e in body["evidence"]}


# --- who may call -----------------------------------------------------------------------------------


def test_a_token_for_another_customer_is_403(tools):
    response = tools("c_003", "repayments", token_for="c_010")
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "forbidden"


def test_a_persons_token_is_403(tools, seeded_client):
    headers = bearer(mint_token(sub="u_analyst", role="analyst", customers=["c_010"]))
    assert seeded_client.get("/customers/c_010/tools/repayments", headers=headers).status_code == 403


def test_no_token_is_401(seeded_client):
    assert seeded_client.get("/customers/c_010/tools/repayments").status_code == 401


def test_unknown_customer_is_404(tools):
    assert tools("c_098", "repayments").status_code == 404


def test_window_rules(tools):
    assert tools("c_003", "transactions", start="2026-11-10", end="2026-11-01").status_code == 422
    assert tools("c_003", "transactions", start="2026-11-01", end="2026-11-11").status_code == 422
    assert tools("c_003", "transactions", start="2025-01-01", end="2026-11-10").status_code == 422


# --- repayments -------------------------------------------------------------------------------------


def test_repayment_statuses_and_counts_match_the_file(tools):
    for cid, c in CUSTOMERS.items():
        body = tools(cid, "repayments", count=3).json()
        statuses = [r["status"] for r in body["data"]["repayments"]]
        assert statuses == c["snapshot"]["card"]["last_3_repayments"], cid
        counts = body["data"]["counts"]
        assert (counts["paid"], counts["late"], counts["failed"]) == (
            statuses.count("paid"), statuses.count("late"), statuses.count("failed")), cid


def test_c010_failed_repayment_is_returned_and_not_paid_since(tools):
    body = tools("c_010", "repayments", count=1).json()
    rep = next(e for e in body["evidence"] if e["kind"] == "card_repayment")
    assert rep["id"] == "rep_c_010_202611"
    assert (rep["data"]["status"], rep["data"]["amount_cents"]) == ("failed", 5_000_000)
    assert (rep["data"]["ach_return_code"], rep["data"]["paid_since"]) == ("R01", "no")
    assert "repaid_after_days" not in rep["data"]


def test_c003_late_repayment_says_how_late(tools):
    rep = tools("c_003", "repayments", count=1).json()["data"]["repayments"][0]
    assert (rep["status"], rep["amount_cents"], rep["days_late"], rep["payment_method"]) == ("late", 4_000_000, 3, "ach_manual")


def test_new_customer_has_no_repayments_yet(tools):
    body = tools(NEW, "repayments").json()
    assert body["data"]["repayments"] == [] and body["data"]["counts"]["n"] == 0


# --- paying_account ---------------------------------------------------------------------------------


def test_c006_paying_account_changed_to_the_new_account(tools):
    body = tools("c_006", "paying_account").json()
    change = next(e for e in body["evidence"] if e["kind"] == "funding_account_change")
    assert change["data"]["changed"] is True
    assert (change["data"]["from"], change["data"]["to"]) == ("acct_c006_a", "acct_external_new_c006")
    assert change["data"]["changed_on"] == "2026-09-20"
    # The repayments it compared come back too, so a finding can cite them.
    assert {"rep_c_006_202609", "rep_c_006_202610", "rep_c_006_202611"} <= _ids(body)


def test_no_other_customer_changed_its_paying_account(tools):
    for cid, c in CUSTOMERS.items():
        if cid == "c_006":
            continue
        data = tools(cid, "paying_account").json()["data"]
        account = c["snapshot"]["bank"]["linked_accounts"][0]["id"]
        assert (data["changed"], data["from"], data["to"]) == (False, account, account), cid


# --- utilization ------------------------------------------------------------------------------------


def test_utilization_on_the_signal_date_matches_the_file(tools):
    for cid, c in CUSTOMERS.items():
        data = tools(cid, "utilization").json()["data"]
        assert data["used_share_bp"] == round(10_000 * c["snapshot"]["card"]["utilization_pct"]), cid
        assert data["balance_id"] == f"bal_card_{cid}_20261110"


def test_utilization_trend_follows_the_files_story(tools):
    trends = {cid: tools(cid, "utilization").json()["data"]["utilization_trend"] for cid in CUSTOMERS}
    assert trends.pop("c_003") == "rising"
    assert trends.pop("c_010") == "spiking"
    assert set(trends.values()) == {"steady"}


def test_short_history_is_too_short_to_tell_not_steady(tools):
    data = tools(NEW, "utilization").json()["data"]
    assert data["utilization_trend"] == "too_short_to_tell"
    assert "usual_share_bp" not in data


# --- spend_by_cycle ---------------------------------------------------------------------------------


def test_spend_by_cycle_flags_only_c003(tools):
    unusual = {cid: tools(cid, "spend_by_cycle").json()["data"]["unusual"] for cid in CUSTOMERS}
    assert unusual.pop("c_003") == "up"
    assert set(unusual.values()) == {"no"}


def test_spend_by_cycle_usual_spend_is_the_closed_cycles_median(tools):
    data = tools("c_010", "spend_by_cycle", cycles=3).json()["data"]
    assert [c["spend_cents"] for c in data["cycles"]] == [4_500_000, 4_500_000, 5_000_000]
    assert data["monthly_card_spend_cents"] == 4_500_000
    assert data["current_cycle"]["spend_cents"] == 4_200_000 and data["current_cycle"]["complete"] is True


def test_new_customer_spend_history_is_too_short_to_tell(tools):
    data = tools(NEW, "spend_by_cycle").json()["data"]
    assert data["history"] == "too_short_to_tell"
    assert "unusual" not in data and "monthly_card_spend_cents" not in data


# --- transactions and spend_breakdown --------------------------------------------------------------


def test_c003_declines_at_the_limit_in_the_open_cycle(tools):
    body = tools("c_003", "transactions", start="2026-10-11", end="2026-11-10", state="declined").json()
    assert body["data"]["declined_by_reason"]["AUTHORIZER_BUSINESS_LIMIT"] == 2
    assert all(t["state"] == "DECLINED" for t in body["data"]["transactions"])
    assert all(e["id"].startswith(("ctx_c_003_", "agg_")) for e in body["evidence"])


def test_transactions_are_capped_and_largest_first(tools):
    data = tools("c_009", "transactions", start="2026-10-11", end="2026-11-10", limit=5).json()["data"]
    amounts = [abs(t["amount_cents"]) for t in data["transactions"]]
    assert len(amounts) == 5 and amounts == sorted(amounts, reverse=True)
    assert data["truncated"] is True and data["matches"] > 5


def test_transactions_filter_by_merchant(tools):
    data = tools("c_010", "transactions", start="2026-10-11", end="2026-11-10", merchant="brewers").json()["data"]
    assert data["matches"] > 0
    assert {t["merchant"] for t in data["transactions"]} == {"Brewers Supply Group"}


def test_spend_breakdown_compares_with_the_period_before(tools):
    data = tools("c_003", "spend_breakdown", start="2026-10-11", end="2026-11-10", by="category").json()["data"]
    assert data["prior_start"] == "2026-09-10" and data["prior_end"] == "2026-10-10"
    assert data["window_total_cents"] == 10_625_000
    assert sum(g["window_cents"] for g in data["groups"]) <= data["window_total_cents"]
    assert data["change_bp"] > 0


# --- evidence ids keep their content ---------------------------------------------------------------


def test_the_same_id_has_the_same_content_whichever_call_returns_it(tools):
    seen: dict[str, dict] = {}
    calls = [
        ("repayments", {"count": 3}), ("repayments", {"count": 13}), ("paying_account", {"count": 3}),
        ("paying_account", {"count": 6}), ("utilization", {}), ("utilization", {"on": "2026-10-10"}),
        ("transactions", {"start": "2026-11-01", "end": "2026-11-10"}),
        ("transactions", {"start": "2026-10-01", "end": "2026-11-10", "limit": 50}),
    ]
    for cid in ("c_003", "c_006", "c_010"):
        for tool, params in calls:
            for item in tools(cid, tool, **params).json()["evidence"]:
                before = seen.setdefault(item["id"], item)
                assert before == item, item["id"]


def test_transactions_summary_keeps_its_content_whatever_the_limit(tools):
    # The same filters with another page size: the summary id may be shared, its content must be too.
    window = {"start": "2026-10-11", "end": "2026-11-10"}
    small, large = (tools("c_003", "transactions", limit=n, **window).json() for n in (5, 50))
    large_by_id = {e["id"]: e for e in large["evidence"]}
    for item in small["evidence"]:
        if item["id"] in large_by_id:
            assert large_by_id[item["id"]] == item, item["id"]
    # The page fields stay in the answer, outside the record.
    assert (small["data"]["shown"], small["data"]["truncated"]) == (5, True)
    summary = next(e for e in small["evidence"] if e["id"] == small["data"]["summary_id"])
    assert "shown" not in summary["data"] and "truncated" not in summary["data"]


def test_paying_account_record_is_the_same_for_every_count(tools):
    for cid in ("c_006", "c_010", NEW):
        records = []
        for count in range(2, 14):
            body = tools(cid, "paying_account", count=count).json()
            [record] = [e for e in body["evidence"] if e["kind"] == "funding_account_change"]
            assert record["id"] == f"rep_{cid}_funding" == body["data"]["change_id"]
            assert "repayments_compared" not in record["data"] and "repayment_ids" not in record["data"]
            records.append(record)
        assert all(r == records[0] for r in records), cid
    c006 = tools("c_006", "paying_account", count=2).json()
    [record] = [e for e in c006["evidence"] if e["kind"] == "funding_account_change"]
    assert record["data"] == {"changed": True, "from": "acct_c006_a", "to": "acct_external_new_c006",
                              "changed_on": "2026-09-20"}


def test_aggregate_ids_change_with_the_question(tools):
    a = tools("c_010", "repayments", count=3).json()["data"]["summary_id"]
    b = tools("c_010", "repayments", count=4).json()["data"]["summary_id"]
    assert a != b and a.startswith("agg_") and len(a) == len("agg_") + 8
    assert a == tools("c_010", "repayments", count=3).json()["data"]["summary_id"]


def test_evidence_kinds_are_the_ones_the_orchestrator_reads(tools):
    kinds = set()
    for tool in ("repayments", "paying_account", "utilization", "spend_by_cycle"):
        kinds |= {e["kind"] for e in tools("c_010", tool).json()["evidence"]}
    assert {"card_repayment", "funding_account_change", "card_balance", "card_spend_series"} <= kinds


def test_a_time_reads_the_same_from_sqlite_and_postgres():
    # SQLite returns a naive UTC datetime, Postgres an aware one in the session's zone (seen: New York).
    from datetime import datetime, timedelta, timezone

    from app.api.evidence import plain

    naive = datetime(2026, 11, 6, 17, 54)
    utc = naive.replace(tzinfo=timezone.utc)
    new_york = utc.astimezone(timezone(timedelta(hours=-5)))
    assert plain(naive) == plain(utc) == plain(new_york) == "2026-11-06T17:54:00+00:00"
