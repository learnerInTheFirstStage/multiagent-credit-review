"""Who is calling. API doc section 5. The claims are proposed there, not agreed yet."""

import os
from typing import Literal

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, ValidationError

_bearer = HTTPBearer(auto_error=False)


class User(BaseModel):
    sub: str = Field(min_length=1)
    role: Literal["analyst", "manager", "admin", "service"]
    # The customers this user may see. A service token names none here.
    customers: list[str] = []
    # Passed on to Card when this user approves an action. Never stored.
    token: str = Field(exclude=True, repr=False)


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> User:
    secret = os.environ.get("JWT_SECRET")
    if not secret:
        # Refuse everyone rather than accept tokens nobody can check.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "JWT_SECRET is not set")
    refused = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "A valid bearer token is required",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise refused
    try:
        claims = jwt.decode(
            credentials.credentials,
            secret,
            algorithms=["HS256"],  # one shared secret for the POC
            options={"require": ["exp", "sub"]},
        )
        return User.model_validate({**claims, "token": credentials.credentials})
    except (jwt.InvalidTokenError, ValidationError):
        raise refused from None
