"""Who may read snapshots. The tools' customer scoping is tested with the tools."""

from tests.conftest import bearer, mint_token


def test_no_token_is_401(seeded_client):
    response = seeded_client.get("/snapshots")
    assert response.status_code == 401
    assert response.json()["detail"]["error"] == "unauthorized"


def test_token_signed_with_another_secret_is_401(seeded_client):
    token = mint_token(scope="snapshots:read", secret="another-secret-that-is-long-enough-to-sign")
    assert seeded_client.get("/snapshots", headers=bearer(token)).status_code == 401


def test_expired_token_is_401(seeded_client):
    token = mint_token(scope="snapshots:read", expires_in=-10)
    assert seeded_client.get("/snapshots", headers=bearer(token)).status_code == 401


def test_service_token_without_the_scope_is_403(seeded_client):
    # An agent's token names one customer and has no scope: it may not read every customer.
    token = mint_token(sub="credit_behavior:case_1", customers=["c_010"])
    response = seeded_client.get("/snapshots", headers=bearer(token))
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "forbidden"


def test_a_person_with_the_scope_is_still_403(seeded_client):
    token = mint_token(sub="u_analyst", role="analyst", customers=["c_010"], scope=["snapshots:read"])
    assert seeded_client.get("/snapshots", headers=bearer(token)).status_code == 403


def test_scope_as_a_space_separated_string(seeded_client):
    token = mint_token(scope="other snapshots:read")
    assert seeded_client.get("/snapshots", headers=bearer(token)).status_code == 200


def test_health_needs_no_token(seeded_client):
    assert seeded_client.get("/health").status_code == 200
