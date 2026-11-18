"""account_reporting: each linked account, when it last reported, and payments to an account that is not linked."""

from tests.conftest import call, evidence_by_id


def test_c005_has_one_stale_and_one_fresh(client, auth_headers):
    """Regression: the kind was account_reporting_status, and staleness depended on an as_of argument."""
    body = call(client, auth_headers, "c_005", "account_reporting").json()
    assert body["data"] == {"account_ids": ["acct_c005_a", "acct_c005_b"]}
    records = evidence_by_id(body)
    assert records["acct_c005_a"] == {
        "id": "acct_c005_a",
        "kind": "linked_account",
        "data": {"active": True, "days_stale": 5, "last_synced": "2026-11-05", "is_stale": True},
    }
    assert records["acct_c005_b"]["data"]["is_stale"] is False


def test_staleness_reads_the_service_clock_not_an_argument(client, auth_headers):
    plain = call(client, auth_headers, "c_005", "account_reporting").json()
    asked = call(client, auth_headers, "c_005", "account_reporting", as_of="2026-12-01").json()
    assert asked["evidence"] == plain["evidence"]


def test_as_of_date_moves_the_clock(client, auth_headers, monkeypatch):
    monkeypatch.setenv("AS_OF_DATE", "2026-11-12")
    data = evidence_by_id(call(client, auth_headers, "c_005", "account_reporting").json())["acct_c005_b"]["data"]
    assert (data["days_stale"], data["is_stale"]) == (2, True)


def test_one_linked_account(client, auth_headers):
    body = call(client, auth_headers, "c_005", "account_reporting", account_id="acct_c005_a").json()
    assert body["data"] == {"account_id": "acct_c005_a", "linked": True}
    assert [r["id"] for r in body["evidence"]] == ["acct_c005_a"]


def test_an_account_that_is_not_linked_shows_what_went_to_it(client, auth_headers):
    body = call(client, auth_headers, "c_106", "account_reporting", account_id="acct_external_new_c106").json()
    assert body["data"] == {"account_id": "acct_external_new_c106", "linked": False, "transaction_ids": ["txn_c_106_02"]}
    assert [r["id"] for r in body["evidence"]] == ["acct_c106_a", "txn_c_106_02"]
    assert evidence_by_id(body)["txn_c_106_02"]["data"]["to_account"] == "acct_external_new_c106"


def test_an_account_nothing_went_to(client, auth_headers):
    body = call(client, auth_headers, "c_006", "account_reporting", account_id="acct_external_new_c006").json()
    assert body["data"]["transaction_ids"] == []
    assert [r["id"] for r in body["evidence"]] == ["acct_c006_a"]
