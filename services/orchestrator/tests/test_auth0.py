"""People sign in through Auth0 once AUTH0_DOMAIN and AUTH0_AUDIENCE are set; services keep JWT_SECRET.

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
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app import auth
from tests.conftest import bearer, sender

DOMAIN = "tenant.example.auth0.com"
AUDIENCE = "https://api.credit-review.demo"
TENANT_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KEY_SET = json.dumps(
    {"keys": [{**jwt.algorithms.RSAAlgorithm.to_jwk(TENANT_KEY.public_key(), as_dict=True), "kid": "tenant-key", "use": "sig"}]}
).encode()


def signed_in(role="manager", customers=("c_010",), key=TENANT_KEY, kid="tenant-key", expires_in=3600, **claims) -> str:
    """An access token as Auth0 issues it after the post-login Action. A claim set to None is left out."""
    now = int(time.time())
    payload = {
        "sub": "auth0|manager",
        "iss": f"https://{DOMAIN}/",
        # Auth0 adds its own userinfo audience when the app asks for openid.
        "aud": [AUDIENCE, f"https://{DOMAIN}/userinfo"],
        "iat": now,
        "exp": now + expires_in,
        f"{auth.CLAIMS}role": role,
        f"{auth.CLAIMS}customers": list(customers),
        **claims,
    }
    payload = {name: value for name, value in payload.items() if value is not None}
    return jwt.encode(payload, key, algorithm="RS256", headers={"kid": kid})


def caller(token_or_headers) -> auth.User:
    token = token_or_headers if isinstance(token_or_headers, str) else token_or_headers["Authorization"].split()[1]
    return auth.current_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))


def refusal(token_or_headers) -> int:
    with pytest.raises(HTTPException) as refused:
        caller(token_or_headers)
    return refused.value.status_code


def tenant_answers(monkeypatch, answer: bytes | Exception) -> None:
    """The tenant's key set endpoint answers with this body, or fails with this error."""

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == f"https://{DOMAIN}/.well-known/jwks.json"
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


def test_a_person_signed_in_through_auth0_has_the_role_and_customers_the_action_added(tenant):
    user = caller(signed_in(role="manager", customers=("c_002", "c_010")))

    assert (user.sub, user.role, user.customers) == ("auth0|manager", "manager", ["c_002", "c_010"])


def test_the_domain_may_be_copied_with_its_scheme_and_a_trailing_slash(tenant, monkeypatch):
    monkeypatch.setenv("AUTH0_DOMAIN", f"https://{DOMAIN}/")

    assert caller(signed_in()).role == "manager"


def test_a_token_issued_a_few_seconds_ahead_of_this_clock_is_accepted(tenant):
    # Auth0's clock and the container's are seldom exactly together.
    assert caller(signed_in(iat=int(time.time()) + 5)).role == "manager"


def test_with_auth0_set_the_shared_secret_signs_no_person(tenant):
    # Every service holds JWT_SECRET. Without this, any of them could approve as a manager.
    assert refusal(bearer(sub="u_manager", role="manager")) == 401


def test_with_auth0_set_a_service_still_signs_its_own_token(tenant):
    assert caller(sender()).role == "service"


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(lambda: signed_in(aud="https://another-api"), id="for another API"),
        pytest.param(lambda: signed_in(iss="https://another-tenant.auth0.com/"), id="from another tenant"),
        pytest.param(lambda: signed_in(key=OTHER_KEY), id="not signed by the tenant's key"),
        pytest.param(lambda: signed_in(kid="not-published"), id="a key the tenant does not publish"),
        pytest.param(lambda: signed_in(expires_in=-60), id="expired"),
        pytest.param(lambda: signed_in(role=None), id="no role"),
        pytest.param(lambda: signed_in(role="service"), id="a service's role"),
    ],
)
def test_an_auth0_token_that_does_not_hold_is_refused(tenant, token):
    assert refusal(token()) == 401


def test_without_auth0_set_a_token_from_auth0_is_refused():
    # Nothing to check it against, so it is not taken as an HS256 token either.
    assert refusal(signed_in()) == 401


def test_half_an_auth0_setting_refuses_everyone(monkeypatch):
    monkeypatch.setenv("AUTH0_DOMAIN", DOMAIN)
    monkeypatch.delenv("AUTH0_AUDIENCE", raising=False)

    assert refusal(sender()) == 503


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(urllib.error.URLError("timed out"), id="unreachable"),
        pytest.param(b"<html>Bad gateway</html>", id="an error page"),
        pytest.param(b'{"keys": []}', id="no keys"),
    ],
)
def test_when_the_tenant_gives_no_usable_keys_the_service_says_so_rather_than_refuse_the_person(tenant, monkeypatch, answer):
    tenant_answers(monkeypatch, answer)

    assert refusal(signed_in()) == 503
