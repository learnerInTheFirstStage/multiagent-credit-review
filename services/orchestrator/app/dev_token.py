"""Make a token for local use, until the Auth Service issues them.

    python -m app.dev_token --role analyst --sub u_analyst --customers c_002 c_010
    python -m app.dev_token --role service --sub signal-generator

Signed with JWT_SECRET, like every token in the POC (API doc section 5).
"""

import argparse
import os
import time

import jwt

ROLES = ("analyst", "manager", "admin", "service")


def make(sub: str, role: str, customers: list[str], hours: float) -> str:
    claims = {"sub": sub, "role": role, "customers": customers, "exp": int(time.time() + hours * 3600)}
    return jwt.encode(claims, os.environ["JWT_SECRET"], algorithm="HS256")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--role", required=True, choices=ROLES)
    parser.add_argument("--sub", required=True, help="who the token is for, such as u_analyst")
    parser.add_argument("--customers", nargs="*", default=[], help="the customers it may see, such as c_010")
    parser.add_argument("--hours", type=float, default=8, help="how long it lasts (default 8)")
    args = parser.parse_args()
    print(make(args.sub, args.role, args.customers, args.hours))
