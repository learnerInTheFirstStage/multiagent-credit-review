"""Who is calling a Bank analysis tool. API doc section 5.

Mirrors services/orchestrator/app/auth.py: HS256 JWT signed with a shared
JWT_SECRET, one secret for every service in the POC. The extra piece here is
require_service_access(customer_id), which enforces the two scope rules the
analysis tools need:

- The caller's role must be `service` (only the Orchestrator reaches these
  endpoints; end-user tokens are refused).
- The URL's customer_id must appear in the token's `customers` claim.

Any failure returns a short JSON body `{"error": ..., "message": ...}` with the
right HTTP status, per API doc section 8.
"""

import os
from typing import Literal

import jwt
from fastapi import Depends, HTTPException, Path, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, ValidationError

_bearer = HTTPBearer(auto_error=False)


class ServiceCaller(BaseModel):
    sub: str = Field(min_length=1)
    role: Literal["analyst", "manager", "admin", "service"]
    # The customers the token may see. A service token names one.
    customers: list[str] = []


def _refused(status_code: int, error: str, message: str) -> HTTPException:
    # One error shape across the service (API doc section 8).
    detail = {"error": error, "message": message}
    headers = {"WWW-Authenticate": "Bearer"} if status_code == status.HTTP_401_UNAUTHORIZED else None
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


def current_caller(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> ServiceCaller:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        # Refuse everyone rather than accept tokens nobody can check.
        raise _refused(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "unconfigured",
            "JWT_SECRET is not set",
        )
    if credentials is None:
        raise _refused(status.HTTP_401_UNAUTHORIZED, "unauthorized", "A valid bearer token is required")
    try:
        claims = jwt.decode(
            credentials.credentials,
            secret,
            algorithms=["HS256"],
            options={"require": ["exp", "sub"]},
        )
        return ServiceCaller.model_validate(claims)
    except (jwt.InvalidTokenError, ValidationError):
        raise _refused(status.HTTP_401_UNAUTHORIZED, "unauthorized", "A valid bearer token is required") from None


def require_service_access(
    customer_id: str = Path(..., min_length=1),
    caller: ServiceCaller = Depends(current_caller),
) -> ServiceCaller:
    """Gate every analysis tool endpoint. Returns the caller on success."""
    if caller.role != "service":
        raise _refused(
            status.HTTP_403_FORBIDDEN,
            "forbidden",
            "Only service callers may use analysis tools",
        )
    if customer_id not in caller.customers:
        raise _refused(
            status.HTTP_403_FORBIDDEN,
            "forbidden",
            f"This token does not cover customer {customer_id}",
        )
    return caller
