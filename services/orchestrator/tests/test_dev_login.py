"""POST /dev_login: the demo-only endpoint that mints a JWT for the frontend."""

import os
import time

import jwt
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def api():
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_refuses_when_flag_is_off(api, monkeypatch):
    """Without DEV_LOGIN=1 the endpoint returns 404 — it is not even visible."""
    monkeypatch.delenv("DEV_LOGIN", raising=False)
    response = api.post(
        "/dev_login",
        json={"sub": "u_demo", "role": "analyst", "customers": ["c_001"]},
    )
    assert response.status_code == 404


def test_mints_an_analyst_token(api, monkeypatch):
    """With the flag on, an analyst request returns a valid JWT the service's own secret signs."""
    monkeypatch.setenv("DEV_LOGIN", "1")
    response = api.post(
        "/dev_login",
        json={"sub": "u_demo", "role": "analyst", "customers": ["c_001", "c_002"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sub"] == "u_demo"
    assert body["role"] == "analyst"
    assert body["customers"] == ["c_001", "c_002"]
    assert body["exp"] > int(time.time())
    # The token itself holds the same claims and the service's secret signs it.
    claims = jwt.decode(body["token"], os.environ["JWT_SECRET"], algorithms=["HS256"])
    assert claims["sub"] == "u_demo"
    assert claims["role"] == "analyst"
    assert claims["customers"] == ["c_001", "c_002"]


def test_also_mints_a_service_token(api, monkeypatch):
    """Service role is the only unauthenticated role kept beside analyst and manager,
    so a dev can post signals through curl without running app.dev_token in the container."""
    monkeypatch.setenv("DEV_LOGIN", "1")
    response = api.post(
        "/dev_login",
        json={"sub": "svc_signal", "role": "service", "customers": ["c_001"]},
    )
    assert response.status_code == 200
    assert response.json()["role"] == "service"


def test_refuses_the_admin_role(api, monkeypatch):
    """Admin is never minted here. An unauthenticated admin token is the one thing this
    endpoint must not hand out."""
    monkeypatch.setenv("DEV_LOGIN", "1")
    response = api.post(
        "/dev_login",
        json={"sub": "u_demo", "role": "admin", "customers": []},
    )
    assert response.status_code == 422
