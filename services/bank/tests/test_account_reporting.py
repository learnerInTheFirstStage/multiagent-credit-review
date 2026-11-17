"""account_reporting endpoint tests. c_005 is the main story."""


def _get(client, headers, customer, as_of="2026-11-10"):
    return client.get(
        f"/customers/{customer}/tools/account_reporting?as_of={as_of}",
        headers=headers,
    )


def test_c005_has_one_stale_and_one_fresh(client, auth_headers):
    response = _get(client, auth_headers, "c_005")
    assert response.status_code == 200
    accounts = response.json()["data"]["accounts"]
    assert len(accounts) == 2
    by_id = {a["id"]: a for a in accounts}
    assert by_id["acct_c005_a"]["days_stale"] == 5
    assert by_id["acct_c005_b"]["days_stale"] == 0


def test_evidence_matches_accounts(client, auth_headers):
    body = _get(client, auth_headers, "c_005").json()
    evidence_ids = {e["id"] for e in body["evidence"]}
    account_ids = {a["id"] for a in body["data"]["accounts"]}
    assert evidence_ids == account_ids


def test_c002_single_fresh_account(client, auth_headers):
    body = _get(client, auth_headers, "c_002").json()
    accounts = body["data"]["accounts"]
    assert len(accounts) == 1
    assert accounts[0]["days_stale"] == 0
    assert accounts[0]["active"] is True
