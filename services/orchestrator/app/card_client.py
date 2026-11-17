"""The Orchestrator's side of its one call that changes anything: an approved action, sent to the Card Service.

The Card Service itself is services/card. This file only makes the HTTP request.
"""

import os

import httpx


class CardError(Exception):
    """Card did not carry out the action."""


def carry_out(action: dict, token: str | None) -> dict:
    """Send one approved action to Card with the approver's own token.

    Card checks the approver's role again and applies each approval_id once.
    The request body is a proposal: Card's endpoint is not built yet.
    """
    if not token:
        # Only a decision request has the approver's token, so nothing else can carry out an action.
        raise CardError("there is no approver token to send")
    base = os.environ.get("CARD_URL", "http://card:8000")
    try:
        response = httpx.post(
            f"{base}/customers/{action['customer_id']}/actions",
            json=action,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as error:  # ValueError: the answer is not JSON
        raise CardError(str(error)) from error
