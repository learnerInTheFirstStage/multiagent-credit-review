"""Who is calling the Card Service. API doc section 5.

The same tokens as the Orchestrator (services/orchestrator/app/auth.py): `sub`, `role` and `customers`.
A service's token is HS256, signed with the shared JWT_SECRET; so is a person's while Auth0 is not set.
Card adds one optional claim, `scope`, a list of strings or a space-separated string.

- GET /snapshots: role `service` and `snapshots:read` in `scope`. The detection job's token.
  An agent's token, which names one customer and no scope, cannot read every customer.
- The analysis tools (KAN-25): role `service`, and the URL's customer in `customers`.
- POST /actions: the approver's own token, which the Orchestrator passes on. With AUTH0_DOMAIN and AUTH0_AUDIENCE
  set, a person's token comes from Auth0 (RS256, checked against the tenant's keys, issuer and audience), with the
  role and customers in claims under CLAIMS, and an HS256 token is accepted for role `service` only. As the
  Orchestrator's app/auth.py.

A refusal is `{"error": ..., "message": ...}` with 401, 403 or 503, as on the Bank Service.
"""

import json
import os
from functools import lru_cache
from typing import Literal

import jwt
from fastapi import Depends, HTTPException, Path, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, ValidationError, field_validator

_bearer = HTTPBearer(auto_error=False)

SNAPSHOTS_SCOPE = "snapshots:read"
# The namespace of the claims the Auth0 post-login Action adds (services/auth/auth0/post-login.js).
CLAIMS = "https://credit-review.demo/"
PEOPLE = ("analyst", "manager", "admin")


class Caller(BaseModel):
    sub: str = Field(min_length=1)
    role: Literal["analyst", "manager", "admin", "service"]
    # The customers the token may see. An agent's service token names one.
    customers: list[str] = []
    scope: list[str] = []

    @field_validator("scope", mode="before")
    @classmethod
    def _split(cls, value: object) -> object:
        return value.split() if isinstance(value, str) else value


def refused(status_code: int, error: str, message: str) -> HTTPException:
    headers = {"WWW-Authenticate": "Bearer"} if status_code == status.HTTP_401_UNAUTHORIZED else None
    return HTTPException(status_code=status_code, detail={"error": error, "message": message}, headers=headers)


def auth0() -> tuple[str, str] | None:
    """The Auth0 tenant's domain and the API's audience, or None while people's tokens are HS256."""
    domain = os.environ.get("AUTH0_DOMAIN", "").removeprefix("https://").rstrip("/")
    audience = os.environ.get("AUTH0_AUDIENCE", "")
    if bool(domain) != bool(audience):
        raise refused(status.HTTP_503_SERVICE_UNAVAILABLE, "unconfigured", "AUTH0_DOMAIN and AUTH0_AUDIENCE must be set together")
    return (domain, audience) if domain else None


@lru_cache
def signing_keys(domain: str) -> jwt.PyJWKClient:
    # Kept for five minutes, and fetched again for a key it has not seen.
    return jwt.PyJWKClient(f"https://{domain}/.well-known/jwks.json", timeout=5)


def _person(token: str, domain: str, audience: str) -> dict:
    try:
        key = signing_keys(domain).get_signing_key_from_jwt(token).key
    except (jwt.PyJWKSetError, json.JSONDecodeError, OSError) as error:
        # A broken answer from the tenant is the service's fault, not the caller's.
        raise jwt.PyJWKClientConnectionError(f"Auth0's signing keys are unusable: {error}") from error
    claims = jwt.decode(
        token,
        key,
        algorithms=["RS256"],
        audience=audience,
        issuer=f"https://{domain}/",
        leeway=30,  # Auth0's clock and this container's can be seconds apart
        options={"require": ["exp", "sub", "iss", "aud"]},
    )
    role = claims.get(f"{CLAIMS}role")
    if role not in PEOPLE:
        # No service signs in through Auth0.
        raise jwt.InvalidTokenError("no person's role")
    return {"sub": claims["sub"], "role": role, "customers": claims.get(f"{CLAIMS}customers", [])}


def current_caller(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Caller:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        # Refuse everyone rather than accept tokens nobody can check.
        raise refused(status.HTTP_503_SERVICE_UNAVAILABLE, "unconfigured", "JWT_SECRET is not set")
    tenant = auth0()
    unauthorized = refused(status.HTTP_401_UNAUTHORIZED, "unauthorized", "A valid bearer token is required")
    if credentials is None:
        raise unauthorized
    token = credentials.credentials
    try:
        if tenant and jwt.get_unverified_header(token).get("alg") == "RS256":
            claims = _person(token, *tenant)
        else:
            claims = jwt.decode(token, secret, algorithms=["HS256"], options={"require": ["exp", "sub"]})
            if tenant and claims.get("role") != "service":
                raise unauthorized  # people sign in through Auth0
        return Caller.model_validate(claims)
    except jwt.PyJWKClientConnectionError:
        raise refused(status.HTTP_503_SERVICE_UNAVAILABLE, "unavailable", "Auth0's signing keys cannot be fetched") from None
    except (jwt.InvalidTokenError, jwt.PyJWKClientError, ValidationError):
        raise unauthorized from None


def require_snapshot_reader(caller: Caller = Depends(current_caller)) -> Caller:
    if caller.role != "service" or SNAPSHOTS_SCOPE not in caller.scope:
        raise refused(status.HTTP_403_FORBIDDEN, "forbidden", f"Reading snapshots needs a service token with scope {SNAPSHOTS_SCOPE}")
    return caller


def require_service_access(
    customer_id: str = Path(..., min_length=1),
    caller: Caller = Depends(current_caller),
) -> Caller:
    """Gate every analysis tool: a service token that names this customer."""
    if caller.role != "service":
        raise refused(status.HTTP_403_FORBIDDEN, "forbidden", "Only service callers may use analysis tools")
    if customer_id not in caller.customers:
        raise refused(status.HTTP_403_FORBIDDEN, "forbidden", f"This token does not cover customer {customer_id}")
    return caller
