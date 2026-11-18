"""Bank answers in the vocabulary the Orchestrator's rules read: the records of its stand-in.

The Orchestrator's stand-in Cash Flow specialist (services/orchestrator/app/agents/mock.py)
answers from the same data files Bank is seeded from. Its records are what the rules in
services/orchestrator/app/graph were written against. For every customer, and for every topic
and focus the stand-in answers, the Bank tool that serves the topic must return:

- every record the stand-in returns, under the same id and kind, with the same value for
  every key the stand-in sets;
- none of the stand-in's keys for that kind where the stand-in leaves them out (a rule tests
  the presence of a key as much as its value);
- no record of a rule-read kind whose id the stand-in does not have. The rules match records by
  kind, so an extra bank_transaction (the old seeder's filler) changes the case.

Bank may add keys of its own that the stand-in never uses for that kind (a date, the weekly series).

Both modules are loaded from their files, read-only: Bank's package is also called `app`.
"""

import importlib.util
import os
from datetime import date, timedelta

import pytest

from tests.conftest import ALL_CUSTOMERS, EXTRA_FILE, REPO, TEAM_FILE


def _load(name: str, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def standin():
    previous = os.environ.get("SHARED_CUSTOMERS_FILE")
    os.environ["SHARED_CUSTOMERS_FILE"] = os.pathsep.join([str(TEAM_FILE), str(EXTRA_FILE)])
    module = _load("standin_mock", REPO / "services" / "orchestrator" / "app" / "agents" / "mock.py")
    module._read.cache_clear()
    yield module
    if previous is None:
        os.environ.pop("SHARED_CUSTOMERS_FILE", None)
    else:
        os.environ["SHARED_CUSTOMERS_FILE"] = previous


_causes = _load("standin_causes", REPO / "services" / "orchestrator" / "app" / "graph" / "causes.py")
# The kinds the rules read from the bank side.
BANK_KINDS = {kind for kind, (tool, _) in _causes.KIND_TOPIC.items() if tool == "AskCashFlow"}

# The stand-in's topic -> the Bank tool that answers it, and how its focus becomes a query.
# The same table is in services/bank/README.md, "Tool interface for the Cash Flow agent".
TOPIC_TOOLS = {
    "transactions": ("transactions", ("window_start", "window_end")),
    "income": ("income_trend", ()),
    "payee_history": ("payee_history", ("record_id",)),
    "accounts": ("account_reporting", ("account_id",)),
    "reconciliation": ("balance_vs_txn", ("window_start", "window_end")),
    "prior_year": ("prior_year_compare", ()),
}
_PARAM = {"window_start": "start", "window_end": "end", "record_id": "record_id", "account_id": "account_id"}


def _questions(standin, cid: str) -> list[tuple[str, dict]]:
    """Every topic, with every focus the stand-in branches on."""
    data = standin._data()
    customer = data["customers"][cid]
    week = {"window_start": data["week"][0], "window_end": data["week"][1]}
    events = customer["signal_window"]["bank_events"]
    txns = [f"txn_{cid}_{number:02d}" for number in range(1, len(events) + 1)]
    linked = [account["id"] for account in customer["snapshot"]["bank"]["linked_accounts"]]
    elsewhere = sorted({event["to_account"] for event in events if "to_account" in event})
    questions = [("transactions", week), ("income", {}), ("reconciliation", week), ("prior_year", {})]
    questions += [("payee_history", {"record_id": txn}) for txn in [*txns, f"txn_{cid}_99"]]
    questions += [("accounts", {**week, "account_id": account}) for account in [None, *linked, *elsewhere, "acct_nobody"]]
    return questions


def _standin_answer(standin, cid: str, topic: str | None, focus: dict) -> list[dict]:
    from contracts import InvestigateRequest

    data = standin._data()
    request = InvestigateRequest(
        case_id="case_parity",
        customer_id=cid,
        question="parity",
        topic=topic,
        window_start=str(focus.get("window_start", data["week"][0])),
        window_end=str(focus.get("window_end", data["week"][1])),
        account_id=focus.get("account_id"),
        record_id=focus.get("record_id"),
    )
    response = standin.cash_flow(request)
    return [evidence.model_dump() for evidence in response.evidence]


def _bank_answer(client, headers, cid: str, tool: str, focus: dict, keys: tuple[str, ...]) -> list[dict]:
    params = {_PARAM[key]: str(focus[key]) for key in keys if focus.get(key) is not None}
    response = client.get(f"/customers/{cid}/tools/{tool}", params=params, headers=headers)
    if response.status_code == 404 and params.get("record_id") == f"txn_{cid}_99":
        return []  # the made-up payment: the stand-in has nothing to say either
    assert response.status_code == 200, (tool, params, response.text)
    return response.json()["evidence"]


def _keys_by_kind(standin) -> dict[str, set[str]]:
    """Every key the stand-in ever sets on a record of each kind, over every customer."""
    keys: dict[str, set[str]] = {}
    for cid in standin.customer_ids():
        for record in _standin_answer(standin, cid, None, {}):
            keys.setdefault(record["kind"], set()).update(record["data"])
        for topic, focus in _questions(standin, cid):
            for record in _standin_answer(standin, cid, topic, focus):
                keys.setdefault(record["kind"], set()).update(record["data"])
    return keys


@pytest.fixture(scope="module")
def stand_in_keys(standin):
    return _keys_by_kind(standin)


def _differences(expected: dict, got: dict | None, kind_keys: set[str]) -> list[str]:
    if got is None:
        return [f"{expected['id']}: missing"]
    found = []
    if got["kind"] != expected["kind"]:
        found.append(f"{expected['id']}: kind {got['kind']} where the stand-in has {expected['kind']}")
    for key, value in expected["data"].items():
        if key not in got["data"]:
            found.append(f"{expected['id']}: no {key} (stand-in: {value!r})")
        elif got["data"][key] != value:
            found.append(f"{expected['id']}: {key} = {got['data'][key]!r}, stand-in {value!r}")
    for key in sorted((kind_keys - set(expected["data"])) & set(got["data"])):
        found.append(f"{expected['id']}: {key} = {got['data'][key]!r}, which the stand-in leaves out")
    return found


def test_the_stand_in_answers_for_every_seeded_customer(standin):
    assert sorted(standin.customer_ids()) == sorted(ALL_CUSTOMERS)


@pytest.mark.parametrize("cid", ALL_CUSTOMERS)
def test_bank_returns_the_stand_ins_records(cid, standin, stand_in_keys, client, auth_headers):
    everything = {record["id"]: record for record in _standin_answer(standin, cid, None, {})}
    problems = []
    for topic, focus in _questions(standin, cid):
        tool, keys = TOPIC_TOOLS[topic]
        expected = _standin_answer(standin, cid, topic, focus)
        known = everything | {record["id"]: record for record in expected}
        got = {record["id"]: record for record in _bank_answer(client, auth_headers, cid, tool, focus, keys)}
        asked = f"{topic} {focus.get('record_id') or focus.get('account_id') or ''}".strip()
        for record in expected:
            problems += [f"[{asked}] {line}" for line in _differences(record, got.get(record["id"]), stand_in_keys[record["kind"]])]
        for record in got.values():
            if record["kind"] not in BANK_KINDS:
                continue
            if record["id"] not in known:
                problems.append(f"[{asked}] {record['id']} ({record['kind']}): Bank-only record of a kind the rules read")
                continue
            problems += [
                f"[{asked}] {line}"
                for line in _differences(known[record["id"]], record, stand_in_keys[known[record["id"]]["kind"]])
            ]
    assert not problems, "\n".join(dict.fromkeys(problems))


@pytest.mark.parametrize("cid", ALL_CUSTOMERS)
def test_bank_holds_every_record_the_stand_in_holds(cid, standin, stand_in_keys, client, auth_headers):
    """With no topic the stand-in returns every fact it holds. Bank's tools between them hold each one."""
    expected = _standin_answer(standin, cid, None, {})
    got = {}
    data = standin._data()
    week = {"window_start": data["week"][0], "window_end": data["week"][1]}
    for topic, focus in [*_questions(standin, cid), ("transactions", week)]:
        tool, keys = TOPIC_TOOLS[topic]
        for record in _bank_answer(client, auth_headers, cid, tool, focus, keys):
            got.setdefault(record["id"], record)
    problems = [line for record in expected for line in _differences(record, got.get(record["id"]), stand_in_keys[record["kind"]])]
    assert not problems, "\n".join(problems)


def test_the_canonical_window_is_the_stand_ins_week(standin, client, auth_headers):
    """No dates and the review week's dates are the same question, with the same ids."""
    start, end = standin._data()["week"]
    assert end - start == timedelta(days=7) and end == date(2026, 11, 10)
    for tool in ("transactions", "balance_vs_txn", "cash_summary"):
        bare = client.get(f"/customers/c_001/tools/{tool}", headers=auth_headers).json()
        dated = client.get(
            f"/customers/c_001/tools/{tool}", params={"start": str(start), "end": str(end)}, headers=auth_headers
        ).json()
        assert bare["evidence"] == dated["evidence"]
