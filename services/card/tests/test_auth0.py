"""With AUTH0_DOMAIN and AUTH0_AUDIENCE set, the approver's token that the Orchestrator passes on comes from Auth0.

No test reaches Auth0. PyJWT's key client runs as it is; only its HTTP opener answers for the tenant.
"""

import io
import json
import time
import urllib.error
import urllib.request

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.api import auth
from tests.conftest import bearer, mint_token

DOMAIN = "tenant.example.auth0.com"
AUDIENCE = "https://api.credit-review.demo"
TENANT_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY_SET = json.dumps(
    {"keys": [{**jwt.algorithms.RSAAlgorithm.to_jwk(TENANT_KEY.public_key(), as_dict=True), "kid": "tenant-key", "use": "sig"}]}
).encode()
MANAGER = "auth0|manager"


def signed_in(role="manager", customers=("c_010",)) -> dict:
    """The header for an access token as Auth0 issues it after the post-login Action."""
    now = int(time.time())
    claims = {
        "sub": MANAGER,
        "iss": f"https://{DOMAIN}/",
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 3600,
        f"{auth.CLAIMS}role": role,
        f"{auth.CLAIMS}customers": list(customers),
    }
    return bearer(jwt.encode(claims, TENANT_KEY, algorithm="RS256", headers={"kid": "tenant-key"}))


def pause(by: str = MANAGER) -> dict:
    return {"type": "pause_card", "case_id": "case_a0", "customer_id": "c_010", "approved_by": by,
            "approval_id": "case_a0:manager"}


def tenant_answers(monkeypatch, answer: bytes | Exception) -> None:
    """The tenant's key set endpoint answers with this body, or fails with this error."""

    class Opener:
        def open(self, request, timeout):
            if isinstance(answer, Exception):
                raise answer
            return io.BytesIO(answer)

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Opener())
    auth.signing_keys.cache_clear()


@pytest.fixture
def tenant(monkeypatch):
    monkeypatch.setenv("AUTH0_DOMAIN", DOMAIN)
    monkeypatch.setenv("AUTH0_AUDIENCE", AUDIENCE)
    tenant_answers(monkeypatch, KEY_SET)


def test_a_manager_signed_in_through_auth0_can_pause_the_card(client, tenant):
    answer = client.post("/customers/c_010/actions", json=pause(), headers=signed_in())

    assert answer.status_code == 200, answer.text
    assert answer.json()["approved_by"] == MANAGER


def test_with_auth0_set_the_shared_secret_signs_no_approver(client, tenant):
    # The Orchestrator holds JWT_SECRET too. Without this, it could approve as a manager on its own.
    forged = bearer(mint_token(sub="u_manager", role="manager", customers=["c_010"]))

    assert client.post("/customers/c_010/actions", json=pause(by="u_manager"), headers=forged).status_code == 401


def test_with_auth0_set_an_agent_still_reads_its_customer_with_its_own_token(client, tenant):
    agent = bearer(mint_token(sub="card-agent:case_a0", role="service", customers=["c_010"]))

    assert client.get("/customers/c_010/tools/repayments", headers=agent).status_code == 200


def test_an_auth0_token_with_a_services_role_cannot_use_the_tools(client, tenant):
    assert client.get("/customers/c_010/tools/repayments", headers=signed_in(role="service")).status_code == 401


def test_half_an_auth0_setting_refuses_everyone(client, monkeypatch):
    monkeypatch.setenv("AUTH0_DOMAIN", DOMAIN)
    monkeypatch.delenv("AUTH0_AUDIENCE", raising=False)
    agent = bearer(mint_token(sub="card-agent:case_a0", role="service", customers=["c_010"]))

    assert client.get("/customers/c_010/tools/repayments", headers=agent).status_code == 503


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(urllib.error.URLError("timed out"), id="unreachable"),
        pytest.param(b"<html>Bad gateway</html>", id="an error page"),
        pytest.param(b'{"keys": []}', id="no keys"),
    ],
)
def test_when_the_tenant_gives_no_usable_keys_card_says_so_rather_than_refuse_the_approver(client, tenant, monkeypatch, answer):
    tenant_answers(monkeypatch, answer)

    assert client.post("/customers/c_010/actions", json=pause(), headers=signed_in()).status_code == 503
