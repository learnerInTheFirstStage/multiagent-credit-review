"""Auth-layer tests for the analysis tool endpoints."""

import time

import jwt

URL = "/customers/c_002/tools/cash_summary?start=2026-11-03&end=2026-11-10"


def test_no_token_is_401(client):
    response = client.get(URL)
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "unauthorized"


def test_invalid_signature_is_401(client):
    bad = jwt.encode(
        {"sub": "x", "role": "service", "customers": ["c_002"],
         "exp": int(time.time()) + 3600},
        "wrong-secret",
        algorithm="HS256",
    )
    response = client.get(URL, headers={"Authorization": f"Bearer {bad}"})
    assert response.status_code == 401


def test_expired_token_is_401(client, mint):
    token = mint(customers=["c_002"], expires_in=-1)
    response = client.get(URL, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_analyst_role_is_403(client, mint):
    token = mint(role="analyst", customers=["c_002"])
    response = client.get(URL, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "forbidden"


def test_token_without_customer_scope_is_403(client, mint):
    token = mint(customers=["c_010"])  # right role, wrong customer
    response = client.get(URL, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_service_token_with_matching_customer_is_200(client, mint):
    token = mint(customers=["c_002"])
    response = client.get(URL, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert "data" in body and "evidence" in body
