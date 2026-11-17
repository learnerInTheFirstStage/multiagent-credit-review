"""Who is calling the Card Service. API doc section 5.

The same tokens as the rest of the POC: HS256, signed with the one shared JWT_SECRET, with
`sub`, `role` and `customers` claims (services/orchestrator/app/auth.py). Card adds one
optional claim, `scope`, a list of strings or a space-separated string.

- GET /snapshots: role `service` and `snapshots:read` in `scope`. The detection job's token.
  An agent's token, which names one customer and no scope, cannot read every customer.
- The analysis tools (KAN-25): role `service`, and the URL's customer in `customers`.

A refusal is `{"error": ..., "message": ...}` with 401, 403 or 503, as on the Bank Service.
"""

import os
from typing import Literal

import jwt
from fastapi import Depends, HTTPException, Path, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, ValidationError, field_validator

_bearer = HTTPBearer(auto_error=False)

SNAPSHOTS_SCOPE = "snapshots:read"


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


def current_caller(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> Caller:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        # Refuse everyone rather than accept tokens nobody can check.
        raise refused(status.HTTP_503_SERVICE_UNAVAILABLE, "unconfigured", "JWT_SECRET is not set")
    if credentials is None:
        raise refused(status.HTTP_401_UNAUTHORIZED, "unauthorized", "A valid bearer token is required")
    try:
        claims = jwt.decode(
            credentials.credentials, secret, algorithms=["HS256"], options={"require": ["exp", "sub"]}
        )
        return Caller.model_validate(claims)
    except (jwt.InvalidTokenError, ValidationError):
        raise refused(status.HTTP_401_UNAUTHORIZED, "unauthorized", "A valid bearer token is required") from None


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
