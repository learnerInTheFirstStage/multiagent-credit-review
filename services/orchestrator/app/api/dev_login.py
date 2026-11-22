"""Dev-only endpoint: mint a JWT from a role + customer list.

Behind DEV_LOGIN=1 only. People sign in through Auth0 when it is set
(services/auth/README.md); this endpoint is the review app's sign-in on a
local stack without Auth0. See KAN-38.

POST /dev_login
  body: {"sub": "u_analyst", "role": "analyst", "customers": ["c_002", ...]}
  → {"token": "...", "sub": "...", "role": "...", "customers": [...]}
"""

from __future__ import annotations

import logging
import os
import time
from typing import Literal

import jwt
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)
router = APIRouter()

# Roles the dev endpoint is allowed to mint. The frontend only needs analyst and
# manager; service is kept so a dev can post signals through curl without running
# app.dev_token in the container. "admin" is never minted here: an unauthenticated
# admin token is the one thing this endpoint must not hand out.
ROLES = ("analyst", "manager", "service")
TOKEN_TTL_SECONDS = 8 * 3600  # 8 hours, matches python -m app.dev_token --hours 8 default


class DevLoginRequest(BaseModel):
    sub: str = Field(min_length=1)
    role: Literal["analyst", "manager", "service"]
    customers: list[str] = Field(default_factory=list)


class DevLoginResponse(BaseModel):
    token: str
    sub: str
    role: str
    customers: list[str]
    exp: int


def enabled() -> bool:
    return os.environ.get("DEV_LOGIN", "").lower() in ("1", "true", "yes")


@router.post("/dev_login", response_model=DevLoginResponse)
def dev_login(request: DevLoginRequest) -> DevLoginResponse:
    """Mint a JWT for demo use. Refused unless DEV_LOGIN=1."""
    if not enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Endpoint not available")
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "JWT_SECRET is not set")

    exp = int(time.time()) + TOKEN_TTL_SECONDS
    claims = {
        "sub": request.sub,
        "role": request.role,
        "customers": request.customers,
        "exp": exp,
    }
    token = jwt.encode(claims, secret, algorithm="HS256")
    log.info("dev_login: minted token for sub=%s role=%s", request.sub, request.role)
    return DevLoginResponse(
        token=token,
        sub=request.sub,
        role=request.role,
        customers=request.customers,
        exp=exp,
    )
