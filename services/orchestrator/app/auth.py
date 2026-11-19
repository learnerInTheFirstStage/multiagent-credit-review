"""Who is calling. API doc section 5.

Two kinds of token reach the service:
- A person's (analyst, manager, admin). With AUTH0_DOMAIN and AUTH0_AUDIENCE set, it is an RS256 access token
  from Auth0, checked against the tenant's published keys, its issuer and this API's audience. The role and the
  customers come in claims under CLAIMS, which the post-login Action adds (services/auth/auth0/post-login.js).
- A service's (the signal sender, a specialist's token for one case): HS256, signed with JWT_SECRET.
With Auth0 set, an HS256 token is accepted for role service only, so a service cannot make itself a manager.
With it unset, every token is HS256 as before: local runs, the tests, the evaluation and app.dev_token.
"""

import json
import os
from functools import lru_cache
from typing import Literal

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, ValidationError

_bearer = HTTPBearer(auto_error=False)

# Auth0 asks that custom claims in an access token sit under a namespace of our own. The Action uses the same one.
CLAIMS = "https://credit-review.demo/"
PEOPLE = ("analyst", "manager", "admin")


class User(BaseModel):
    sub: str = Field(min_length=1)
    role: Literal["analyst", "manager", "admin", "service"]
    # The customers this user may see. A service token names none here.
    customers: list[str] = []
    # Passed on to Card when this user approves an action. Never stored.
    token: str = Field(exclude=True, repr=False)


def auth0() -> tuple[str, str] | None:
    """The Auth0 tenant's domain and this API's audience, or None while people's tokens are HS256."""
    # Copied from the tenant's page, the domain may come with https:// or a trailing slash.
    domain = os.environ.get("AUTH0_DOMAIN", "").removeprefix("https://").rstrip("/")
    audience = os.environ.get("AUTH0_AUDIENCE", "")
    if bool(domain) != bool(audience):
        # Half a setting would check people's tokens against nothing. Refuse everyone instead.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AUTH0_DOMAIN and AUTH0_AUDIENCE must be set together")
    return (domain, audience) if domain else None


@lru_cache
def signing_keys(domain: str) -> jwt.PyJWKClient:
    # The client keeps the tenant's keys for five minutes, and fetches them again for a key it has not seen.
    return jwt.PyJWKClient(f"https://{domain}/.well-known/jwks.json", timeout=5)


def _person(token: str, domain: str, audience: str) -> dict:
    try:
        key = signing_keys(domain).get_signing_key_from_jwt(token).key
    except (jwt.PyJWKSetError, json.JSONDecodeError, OSError) as error:
        # The tenant's answer is broken (an error page, no keys, a dropped connection): the service's fault, not
        # the person's. A key id the tenant does not publish stays a refusal.
        raise jwt.PyJWKClientConnectionError(f"Auth0's signing keys are unusable: {error}") from error
    claims = jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        audience=audience,
        issuer=f"https://{domain}/",
        # Auth0's clock and this container's can be seconds apart, which would refuse a token just issued.
        leeway=30,
        options={"require": ["exp", "sub", "iss", "aud"]},
    )
    role = claims.get(f"{CLAIMS}role")
    if role not in PEOPLE:
        # No service signs in through Auth0, and a person with no role has nothing to do here.
        raise jwt.InvalidTokenError("no person's role")
    return {"sub": claims["sub"], "role": role, "customers": claims.get(f"{CLAIMS}customers", [])}


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> User:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        # Refuse everyone rather than accept tokens nobody can check.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "JWT_SECRET is not set")
    tenant = auth0()
    refused = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "A valid bearer token is required",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise refused
    token = credentials.credentials
    try:
        if tenant and jwt.get_unverified_header(token).get("alg") == "RS256":
            claims = _person(token, *tenant)
        else:
            claims = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
            if tenant and claims.get("role") != "service":
                raise refused  # people sign in through Auth0
        return User.model_validate({**claims, "token": token})
    except jwt.PyJWKClientConnectionError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Auth0's signing keys cannot be fetched") from None
    except (jwt.InvalidTokenError, jwt.PyJWKClientError, ValidationError):
        raise refused from None
