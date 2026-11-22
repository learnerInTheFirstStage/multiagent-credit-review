"""The Orchestrator's side of its one call that changes anything: an approved action, sent to the Card Service.

The Card Service itself is services/card. This file only makes the HTTP request.
"""

import os

import httpx

from contracts.logs import trace_headers

# What Card's actions endpoint answers when it refuses an action for good: a bad body or limit, a role
# that may not decide, an unknown customer, another action under the same approval_id.
REFUSALS = {400, 403, 404, 409, 422}


class CardError(Exception):
    """Card did not carry out the action.

    `refused` is True when Card read the request and said no (REFUSALS), so sending the same
    approval again gets the same answer. `token_rejected` is True when Card did not take the
    approver's token (401): Card and the Orchestrator check sign-ins differently, and a resend
    works only once that is put right. Otherwise Card failed or was not reached.
    """

    def __init__(self, message: str, *, refused: bool = False, token_rejected: bool = False):
        super().__init__(message)
        self.refused = refused
        self.token_rejected = token_rejected


def _card_message(response: httpx.Response) -> str:
    """Card's own words for a refusal: {"detail": {"error", "message"}}, or FastAPI's list of field errors."""
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return response.text[:300]
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("error") or detail)[:300]
    return str(detail)[:300]


def carry_out(action: dict, token: str | None) -> dict:
    """Send one approved action to Card with the approver's own token (services/card, POST .../actions).

    Card checks the approver's role again and applies each approval_id once, so a retry is safe.
    """
    if not token:
        # Only a decision request has the approver's token, so nothing else can carry out an action.
        raise CardError("there is no approver token to send")
    base = os.environ.get("CARD_URL", "http://card:8000")
    try:
        response = httpx.post(
            f"{base}/customers/{action['customer_id']}/actions",
            json=action,
            headers={"Authorization": f"Bearer {token}", **trace_headers()},
            timeout=10,
        )
        if response.status_code in REFUSALS:
            raise CardError(f"Card refused the action ({response.status_code}): {_card_message(response)}", refused=True)
        if response.status_code == 401:
            # The Orchestrator has just accepted this token. Unless it expired in between, the two services
            # disagree: a Card image built before a change to sign-in, or a setting only one of them has.
            raise CardError(
                "Card did not accept the approver's token (401), which the Orchestrator had just accepted. Unless it "
                "expired in between, Card is not on the Orchestrator's build or does not have its JWT_SECRET, "
                "AUTH0_DOMAIN and AUTH0_AUDIENCE: rebuild and restart Card, then send the approval again",
                token_rejected=True,
            )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as error:  # ValueError: the answer is not JSON
        raise CardError(str(error)) from error
