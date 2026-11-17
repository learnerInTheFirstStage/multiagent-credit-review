"""POST /customers/{id}/actions (KAN-28): checked again, applied once."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.db.models import AppliedAction, CreditLimit, Customer
from tests.conftest import bearer, mint_token

ANALYST, MANAGER = "u_analyst", "u_manager"


def person(role: str, sub: str | None = None, customers: list[str] | None = None) -> dict:
    sub = sub or {"analyst": ANALYST, "manager": MANAGER}.get(role, f"u_{role}")
    return bearer(mint_token(sub=sub, role=role, customers=customers or ["c_010", "c_003"]))


def reduce(limit_cents: int, case: str, step: str = "analyst", by: str = ANALYST, customer: str = "c_010") -> dict:
    return {"type": "reduce_limit", "limit_cents": limit_cents, "case_id": case, "customer_id": customer,
            "approved_by": by, "approval_id": f"{case}:{step}"}


def pause(case: str, by: str = MANAGER, step: str = "manager", customer: str = "c_010") -> dict:
    return {"type": "pause_card", "case_id": case, "customer_id": customer, "approved_by": by,
            "approval_id": f"{case}:{step}"}


@pytest.fixture
def card(client):
    def post(body: dict, headers: dict, customer: str | None = None):
        return client.post(f"/customers/{customer or body['customer_id']}/actions", json=body, headers=headers)

    return post


def limit_of(customer_id: str) -> int:
    with SessionLocal() as s:
        return s.get(Customer, customer_id).credit_limit_cents


def card_status(customer_id: str) -> str:
    with SessionLocal() as s:
        return s.get(Customer, customer_id).card_status


# --- what the Orchestrator sends (services/orchestrator/tests/test_cases.py) ---------------------


def test_an_analyst_reduction_is_applied(card, client):
    response = card(reduce(5_800_000, "case_a1"), person("analyst"))
    assert response.status_code == 200, response.text
    receipt = response.json()
    assert (receipt["status"], receipt["type"], receipt["approved_by"]) == ("applied", "reduce_limit", ANALYST)
    assert receipt["details"] == {"previous_limit_cents": 10_000_000, "limit_cents": 5_800_000, "cut_bp": 4200}
    assert limit_of("c_010") == 5_800_000
    with SessionLocal() as s:
        row = s.scalars(select(CreditLimit).where(CreditLimit.approval_id == "case_a1:analyst")).one()
        assert row.limit_cents == 5_800_000
    # The snapshot and the tools read the new limit.
    snap = client.get("/snapshots", params={"customer_id": "c_010"},
                      headers=bearer(mint_token(scope="snapshots:read"))).json()["customers"][0]
    assert snap["credit_limit_cents"] == 5_800_000


def test_a_manager_pause_is_applied(card):
    response = card(pause("case_p1"), person("manager"))
    assert response.status_code == 200, response.text
    assert response.json()["details"] == {"previous_card_status": "active", "card_status": "paused"}
    assert card_status("c_010") == "paused"


def test_a_manager_may_approve_the_analyst_step(card):
    assert card(reduce(8_000_000, "case_m1", by=MANAGER), person("manager")).status_code == 200


# --- each approval once -------------------------------------------------------------------------


def test_a_retry_returns_the_same_receipt_and_changes_nothing(card):
    first = card(reduce(5_800_000, "case_r1"), person("analyst")).json()
    second = card(reduce(5_800_000, "case_r1"), person("analyst"))
    assert second.status_code == 200
    assert second.json() == {**first, "status": "already_applied"}
    assert limit_of("c_010") == 5_800_000
    with SessionLocal() as s:
        assert len(s.scalars(select(AppliedAction)).all()) == 1
        assert len(s.scalars(select(CreditLimit).where(CreditLimit.customer_id == "c_010")).all()) == 2


def test_the_same_approval_for_another_action_is_409(card):
    card(reduce(5_800_000, "case_c1"), person("analyst"))
    response = card(reduce(4_000_000 + 2_000_000, "case_c1"), person("analyst"))
    assert response.status_code == 409
    assert limit_of("c_010") == 5_800_000


def test_two_copies_at_once_apply_once(card):
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda _: card(reduce(7_000_000, "case_t1"), person("analyst")), range(2)))
    assert sorted(a.status_code for a in answers) == [200, 200]
    assert sorted(a.json()["status"] for a in answers) == ["already_applied", "applied"]
    assert limit_of("c_010") == 7_000_000


# --- refused without an approval, or with the wrong one -----------------------------------------


def test_no_approval_id_is_refused(card):
    body = reduce(5_800_000, "case_n1")
    del body["approval_id"]
    assert card(body, person("analyst")).status_code == 422
    assert limit_of("c_010") == 10_000_000


@pytest.mark.parametrize("approval_id", ["case_n2", "case_other:analyst", "case_n2:admin", "case_n2:analyst:x", ""])
def test_an_approval_id_not_of_this_case_and_step_is_refused(card, approval_id):
    body = reduce(5_800_000, "case_n2") | {"approval_id": approval_id}
    assert card(body, person("analyst")).status_code == 422


def test_a_pause_needs_the_manager(card):
    # The analyst step cannot pause, even with a manager's token.
    assert card(pause("case_x1", by=ANALYST, step="analyst"), person("analyst")).status_code == 403
    assert card(pause("case_x2", step="analyst"), person("manager")).status_code == 403
    assert card_status("c_010") == "active"


def test_a_cut_of_more_than_half_needs_the_manager(card):
    assert card(reduce(4_999_999, "case_h1"), person("analyst")).status_code == 403
    assert limit_of("c_010") == 10_000_000
    # Exactly half is not more than half.
    assert card(reduce(5_000_000, "case_h2"), person("analyst")).status_code == 200


def test_an_analyst_cannot_give_the_managers_approval(card):
    assert card(reduce(4_000_000, "case_h3", step="manager"), person("analyst")).status_code == 403
    assert card(reduce(4_000_000, "case_h4", step="manager", by=MANAGER), person("manager")).status_code == 200


def test_an_admin_is_not_an_approver_here(card):
    assert card(reduce(8_000_000, "case_ad", by="u_admin"), person("admin")).status_code == 403


def test_a_service_token_is_refused(card):
    # An agent, or any service, never carries out an action.
    headers = bearer(mint_token(sub="credit_behavior:case_s1", role="service", customers=["c_010"]))
    assert card(reduce(8_000_000, "case_s1", by="credit_behavior:case_s1"), headers).status_code == 403


def test_approved_by_must_be_the_tokens_owner(card):
    assert card(reduce(8_000_000, "case_o1", by="u_someone_else"), person("analyst")).status_code == 403


def test_a_token_for_other_customers_is_refused(card):
    assert card(reduce(8_000_000, "case_o2"), person("analyst", customers=["c_003"])).status_code == 403


def test_no_token_is_401(card):
    assert card(reduce(8_000_000, "case_o3"), {}).status_code == 401


# --- the body ----------------------------------------------------------------------------------------


def test_a_raise_is_not_a_reduction(card):
    assert card(reduce(10_000_000, "case_b1"), person("analyst")).status_code == 422
    assert card(reduce(12_000_000, "case_b2"), person("analyst")).status_code == 422


def test_the_url_and_the_body_must_name_the_same_customer(card):
    assert card(reduce(8_000_000, "case_b3"), person("analyst"), customer="c_003").status_code == 422


@pytest.mark.parametrize("change", [
    {"type": "keep_limit"}, {"limit_cents": "5800000"}, {"limit_cents": None}, {"extra": 1},
])
def test_a_body_the_contract_does_not_allow_is_refused(card, change):
    assert card(reduce(5_800_000, "case_b4") | change, person("analyst")).status_code == 422


def test_pause_takes_no_limit(card):
    assert card(pause("case_b5") | {"limit_cents": 1}, person("manager")).status_code == 422


def test_unknown_customer_is_404(card):
    body = reduce(1_000, "case_u1", customer="c_098")
    assert card(body, person("analyst", customers=["c_098"])).status_code == 404
