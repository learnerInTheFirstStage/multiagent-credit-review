"""Make a token for the Card Service, for local use: a service's, or a person's that Card accepts only without Auth0.

    python -m app.dev_token --sub detection-job --scope snapshots:read
    python -m app.dev_token --sub card_service:case_dev --customers c_010

The same claims as the Orchestrator's dev tokens (sub, role, customers), plus Card's
optional scope. Signed with JWT_SECRET. Inside the stack:
    docker compose exec card python -m app.dev_token --sub detection-job --scope snapshots:read
"""

import argparse
import os
import time

import jwt

ROLES = ("analyst", "manager", "admin", "service")


def make(sub: str, role: str, customers: list[str], scope: list[str], hours: float) -> str:
    claims = {"sub": sub, "role": role, "customers": customers, "exp": int(time.time() + hours * 3600)}
    if scope:
        claims["scope"] = scope
    return jwt.encode(claims, os.environ["JWT_SECRET"], algorithm="HS256")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sub", required=True, help="who the token is for, such as detection-job")
    parser.add_argument("--role", default="service", choices=ROLES)
    parser.add_argument("--customers", nargs="*", default=[], help="the customers it may see, such as c_010")
    parser.add_argument("--scope", nargs="*", default=[], help="such as snapshots:read")
    parser.add_argument("--hours", type=float, default=8, help="how long it lasts (default 8)")
    args = parser.parse_args()
    print(make(args.sub, args.role, args.customers, args.scope, args.hours))
