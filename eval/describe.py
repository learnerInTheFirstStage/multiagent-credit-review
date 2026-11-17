"""Writes eval/golden-set.md: every case of the golden dataset with what the agent is given.

    cd services/orchestrator && python ../../eval/describe.py > ../../eval/golden-set.md

Run it with the Orchestrator's Python: it uses the service's stand-ins, history and guideline code.
For reading before a run. A person should be able to reach each expected answer
from the facts listed, and from nothing else.
"""

import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "eval"), str(ROOT / "services" / "orchestrator")]

from grade import LINKS  # noqa: E402
from signals import FILES, customers, signal_for  # noqa: E402

os.environ["SHARED_CUSTOMERS_FILE"] = os.pathsep.join(str(path) for path in FILES)
# The seeded reviews go into a database of this process's own, never into one the environment names.
os.environ["DATABASE_URL"] = "sqlite://"

from app import guidelines, history  # noqa: E402
from app.agents import mock  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.graph import causes, facts, needs, plans  # noqa: E402
from app.graph.build import WINDOW_DAYS  # noqa: E402
from contracts import InvestigateRequest, Signal  # noqa: E402

# The review every stack has, then the evaluation's own (spec 0.3, item 1).
HISTORY_FILES = (ROOT / "eval" / "history.yaml", ROOT / "eval" / "history_eval.yaml")
SIDES = {"AskCashFlow": ("Bank side", mock.cash_flow), "AskCreditBehavior": ("Card side", mock.credit_behavior)}
# The records code gives at open or works out itself, which no question has to reach.
OWN = ("hist_", "sig_", "xs_")

golden = yaml.safe_load((ROOT / "eval" / "golden_set.yaml").read_text())
today, data = customers()
seeds = history.read_seeds(HISTORY_FILES)
Base.metadata.create_all(engine)
with SessionLocal() as session:
    history.seed(session, seeds, expect_clean=True, clock=today)
    session.commit()
loaded = guidelines.load()


def ask(customer_id: str, window: dict, tool: str, topic: str, record_id: str | None = None) -> dict:
    request = InvestigateRequest(
        case_id="case_x", customer_id=customer_id, question="-", window_start=window["start"],
        window_end=window["end"], topic=topic, record_id=record_id,
    )  # fmt: skip
    return SIDES[tool][1](request).model_dump(mode="json")


def walk(customer_id: str, asks: list[tuple]) -> tuple[dict, list[dict], list[dict]]:
    """The window and the facts the case opens with, then each question to the stand-in: the answers, the ledger
    and the `asked` entries (spec 1.3), as the graph would hold them."""
    signal = signal_for(data[customer_id], today)
    window = {"start": (today - timedelta(days=WINDOW_DAYS)).isoformat(), "end": today.isoformat()}
    reviews = history.earlier_reviews(customer_id, signal["as_of"], limit=None)
    state = {"facts": [*facts.signal_facts(signal, window), *facts.history_facts(reviews, signal)], "answers": []}
    asked = []
    for tool, topic, record_id in asks:
        response = ask(customer_id, window, tool, topic, record_id)
        state["answers"].append({"agent": response["agent"], "question": "-", "response": response})
        asked.append({
            "tool": tool, "agent": response["agent"], "topic": topic, "tests": causes.TOPIC_TESTS[topic],
            "outcome": "answered", "returned": [item["id"] for item in response["evidence"]],
            **({"record_id": record_id} if record_id else {}),
        })  # fmt: skip
    return state, list(facts.ledger(state).values()), asked


def reached(case: dict) -> bool:
    """Class R of 8.4: the fixed plan returns what the case must cite and reach, and its records carry an accepted
    answer under one of the case's guidelines (8.3, point 3). Otherwise class D."""
    plan = [(step["tool"], step["args"]["topic"], None) for step in plans.FIXED_PLAN]
    _, records, asked = walk(case["id"], plan)
    returned = {evidence_id for entry in asked for evidence_id in entry["returned"]}
    must_reach = case.get("must_reach", [cid for cid in case.get("must_cite", []) if not cid.startswith(OWN)])
    carried = {
        guideline_id
        for recommendation, ids in needs.permitted(loaded, records).items()
        if recommendation in case["recommendations"]
        for guideline_id in ids
    }
    if "needs_human" in case["recommendations"]:
        carried |= set(needs.hand_over(loaded, records, asked))
    return (
        set(must_reach) <= returned
        and set(case.get("must_cite", [])) <= {item["id"] for item in records}
        and bool(carried & set(case["guidelines"]))
    )


def every_topic(customer_id: str) -> list[tuple]:
    """Each topic in catalogue order, the payee's history of each vendor payment in its place."""
    plain = [(tool, topic, None) for tool, topics in causes.TOPICS.items() for topic in topics]
    _, records, _ = walk(customer_id, [step for step in plain if step[:2] not in causes.NEEDS_RECORD])
    vendors = [
        item["id"]
        for item in records
        if item["kind"] == "bank_transaction" and item["data"].get("category") in ("vendor_known", "vendor_new")
    ]
    asks = []
    for tool, topic, _ in plain:
        asks += [(tool, topic, txn) for txn in vendors] if (tool, topic) in causes.NEEDS_RECORD else [(tool, topic, None)]
    return asks


def linked(case: dict) -> str:
    return next((f"{words} {case[key]}" for key, words in LINKS.items() if key in case), "")


def print_seed(seed: dict) -> None:
    decided = " then ".join(f"{d['step']} {d['decision']}" for d in seed["decisions"])
    print(
        f"- {seed['case_id']}, {seed['as_of']}, {', '.join(seed['review_reasons'])}: {seed['likely_cause']}, "
        f"{seed['recommendation']} under {', '.join(seed['guideline_ids'])}, {seed['outcome']} ({decided}). "
        f"{' '.join(seed['claims'])}"
    )
    for decision in seed["decisions"]:
        if decision.get("note"):
            print(f"  - Note, passed as data: `{decision['note']}`")


classes = {case["id"]: "R" if reached(case) else "D" for case in golden["customers"]}

print("# Golden dataset\n")
print("Generated by `python eval/describe.py`. Edit `eval/golden_set.yaml`, not this file.\n")
print(
    "Class R: the fixed list of questions (spec 8.4) returns what the case must cite and reach, and its records carry "
    "an accepted answer. Class D: it does not, because the answer needs a question about one record an earlier "
    "answer listed.\n"
)
print("| Case | Kind | Set against | Class | Review reasons | Engine alone | Accepted cause | Accepted recommendation |")
print("|---|---|---|---|---|---|---|---|")
for case in golden["customers"]:
    signal = signal_for(data[case["id"]], today)
    Signal.model_validate(signal)  # a signal the service would refuse is a bug here
    cut = 1 - signal["engine_limit_cents"] / signal["current_limit_cents"]
    print(
        f"| {case['id']} | {case['category']} | {linked(case)} | {classes[case['id']]} "
        f"| {', '.join(signal['review_reasons'])} | cut {cut:.0%} "
        f"| {' or '.join(case['causes'])} | {' or '.join(case['recommendations'])} |"
    )
print("\n| Request | Kind | What is sent | Expected |")
print("|---|---|---|---|")
for case in golden["requests"]:
    expect = ", ".join(f"{key} {value}" for key, value in case["expect"].items())
    print(f"| {case['id']} | {case['category']} | {case['what']} | {expect} |")
print()


for case in golden["customers"]:
    customer, asks = data[case["id"]], every_topic(case["id"])
    state, _, _ = walk(case["id"], asks)
    print(f"## {case['id']}\n")
    print(f"The one-line story (the agent never sees it): {' '.join(customer['story'].split())}\n")
    if against := linked(case):
        print(f"{against[0].upper()}{against[1:]}.\n")
    print(f"Class {classes[case['id']]}.\n")
    if "must_cite" in case:
        print(f"The report has to cite: {', '.join(case['must_cite'])}.\n")
    if "must_reach" in case:
        print(f"A run counts as tested once a question returned: {', '.join(case['must_reach'])}.\n")
    print(f"Accepted: cause {' or '.join(case['causes'])}, recommendation {' or '.join(case['recommendations'])}.\n")
    print(f"Guidelines a right report may rest on: {', '.join(case['guidelines'])}.\n")
    print(f"A question path that reaches it: {', '.join(case['path'])}.\n")
    print("The signal the case starts with:\n")
    print("````json\n" + json.dumps(signal_for(customer, today), indent=2) + "\n````\n")

    mine = [seed for seed in seeds if seed["customer_id"] == case["id"]]
    if mine:
        print("Earlier reviews, seeded. The notes reach the agent only through search, marked as untrusted:\n")
        for seed in mine:
            print_seed(seed)
        print()
    else:
        print("No earlier review.\n")
    print("What code makes of the history when the case opens, as the agent is shown it:\n")
    shown = [facts.shown(item) for item in state["facts"] if item["id"].startswith("hist_")]
    print("````json\n" + json.dumps(shown, indent=2) + "\n````\n")

    memos = set()
    for tool, (title, _) in SIDES.items():
        print(f"What the {title.lower()} stand-in says on each topic:\n")
        for answer, (asked_tool, topic, record_id) in zip(
            (answer["response"] for answer in state["answers"]), asks, strict=True
        ):
            if asked_tool != tool:
                continue
            print(f"{topic}" + (f" about {record_id}" if record_id else "") + ":")
            for finding in answer["findings"]:
                print(f"- {finding['claim']} ({', '.join(finding['evidence_ids'])})")
            for unknown in answer["could_not_determine"]:
                print(f"- Cannot determine: {unknown}")
            for evidence in answer["evidence"]:
                if "memo" in evidence["data"] and evidence["id"] not in memos:
                    memos.add(evidence["id"])
                    print(f"- Memo on {evidence['id']}, passed as data: `{evidence['data']['memo']}`")
            print()

cases = {case["id"] for case in golden["customers"]}
print("## Seeded reviews of customers that are no case\n")
print("Precedents for the search tool. No customer file holds these customers.\n")
for seed in seeds:
    if seed["customer_id"] not in cases:
        print_seed(seed)
