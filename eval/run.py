"""Run the golden dataset against a running stack, over HTTP, and grade it.

One arm per fresh stack, on a fresh volume. INVESTIGATION_FLOW names the stack's arm (spec 8.4), and
SPECIALISTS who answers its questions: empty is the real Cash Flow and Card agents, stand_in the stand-ins.

    INVESTIGATION_FLOW=fixed_rules docker compose -p goldenset -f docker-compose.yml -f eval/docker-compose.eval.yml up -d --build postgres bank card orchestrator
    python eval/run.py --arm fixed_rules --container "docker compose -p goldenset exec -T orchestrator"

With SPECIALISTS=stand_in on the first line, pass --specialists stand_in on the second.

Before the first signal the runner stops unless the stack runs the arm and the specialists asked for,
no case is open, and the database is clean: both seed files are seeded again with
--expect-clean, and the seeded cases are read back through the service.

Every customer case is one signal sent to POST /signals, waited for, read from
GET /cases/{id} and GET /cases/{id}/path, and graded on its answer and on its path.
The case is then rejected, which closes it: a customer has one open case at a time,
so the next run of that customer needs it closed. Runs of one customer go one after
another, customers side by side.

This spends money on the model the stack is set to: about 80 runs for the whole set.
Results go to eval/results/<run id>/: runs.jsonl as each run ends, summary.md at the end.

With LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY (and LANGFUSE_BASE_URL) in the runner's environment and the
langfuse package installed, the model calls, tokens and cost of every case are read from LangFuse once the runs
are over, into usage.json. Without them the summary says they were not recorded, and the runs grade the same.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from grade import grade, grade_path, summary  # noqa: E402
from signals import customers, signal_for  # noqa: E402

WORKING = ("queued", "investigating")
# Bank or Card gave nothing, and the case waits to ask again (services/orchestrator/app/runner.py).
WAITING_FOR_DATA = "waiting_for_data"
ARMS = ("agent", "fixed", "fixed_rules")
# Who answers the graph's questions, as SPECIALISTS names it in the service (app/runtime.py specialists()).
SPECIALISTS = {
    "real": "the Cash Flow and Card agents, which call Bank and Card",
    "stand_in": "the stand-ins, which answer from the data files",
}
# The seed files where the overlay mounts them in the container, and where they are in this checkout.
SEED_FILES = {
    "/eval/history.yaml": ROOT / "eval" / "history.yaml",
    "/eval/history_eval.yaml": ROOT / "eval" / "history_eval.yaml",
}
# LangFuse takes traces in with a delay. Usage is read this many seconds after the last run.
USAGE_WAIT = 30
# Run inside the container: what the stack runs with, as one JSON object on the last line of its output.
STACK = """
import hashlib, json, os
from pathlib import Path
from app import policy, retrieval
embedder = retrieval.load(os.environ.get("EMBEDDINGS_MODEL"))
print(json.dumps({
    "model": os.environ.get("LLM_MODEL") or "none set",
    "structured_output": os.environ.get("LLM_STRUCTURED_OUTPUT") or "default",
    "flow": os.environ.get("INVESTIGATION_FLOW", ""),
    "specialists": os.environ.get("SPECIALISTS", ""),
    "embedder": embedder.name if embedder else None,
    "policy": {name: value for name, value in vars(policy).items() if name.isupper()},
    "prompts": hashlib.sha256((Path(policy.__file__).parent / "graph" / "prompts.py").read_bytes()).hexdigest()[:12],
}))
"""


def inside(container: str, *command: str) -> str:
    """Run a command in the Orchestrator's container. Tokens are made there, with its secret."""
    done = subprocess.run([*shlex.split(container), *command], check=True, capture_output=True, text=True)
    return done.stdout.strip()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def stack(container: str, arm: str, specialists: str) -> dict:
    """What the stack runs with, read inside its container, for the run's `about`.
    Stops when it runs another arm or other specialists."""
    found = json.loads(inside(container, "python", "-c", STACK).splitlines()[-1])
    flow = found["flow"] or "agent"  # unset or empty is the agent, as in the service
    if flow not in ARMS:
        sys.exit(
            f"The stack runs INVESTIGATION_FLOW={found['flow']}, which names no arm ({', '.join(ARMS)}, or empty for "
            "agent). The service runs such a value as the agent, but a misspelt arm is not counted as one. "
            "Start a fresh stack with one of them."
        )
    if flow != arm:
        sys.exit(
            f"The stack runs INVESTIGATION_FLOW={found['flow'] or '(unset)'}, not the {arm} arm. "
            "Start a fresh stack for each arm."
        )
    # Unset or empty is the real agents, as in the service.
    answering = found["specialists"] or "real"
    if answering not in SPECIALISTS:
        sys.exit(
            f"The stack runs SPECIALISTS={found['specialists']}, which names neither real nor stand_in (or empty for "
            "real). The service runs such a value as the real agents, but a misspelt stand_in is not counted as one. "
            "Start a fresh stack with one of them."
        )
    if answering != specialists:
        sys.exit(
            f"The stack runs SPECIALISTS={found['specialists'] or '(unset)'}, not the {specialists} specialists. "
            "Its results would not compare with runs of the other kind. Start a fresh stack, or pass --specialists "
            f"{answering}."
        )
    if arm != "agent":
        search = "off: this arm asks a fixed list and never searches"
    else:
        search = f"on, {found['embedder']}" if found["embedder"] else "off: no embedder could be loaded"
    return {
        "Arm": arm,
        "Specialists": f"{specialists}: {SPECIALISTS[specialists]}",
        "Model": f"{found['model']} | structured output: {found['structured_output']}",
        "Search": search,
        # Every value: POLICY_VERSION alone would not change when a share line or a switch does.
        "Policy": json.dumps(found["policy"], sort_keys=True),
        "Prompts": f"sha256 {found['prompts']} of app/graph/prompts.py",
    }


def seed_history(client: httpx.Client, container: str) -> str:
    """Before the first signal: the seeded reviews are there, and no review that is not a seed is history.

    The service seeds HISTORY_SEED_FILE at start, so this is called once it answers. The command runs again with
    --expect-clean: it adds a seed that is missing and refuses a database that holds somebody's earlier runs as
    history. The seeded cases are then read back through the service the signals go to.
    """
    try:
        output = inside(container, "python", "-m", "app.seed_history", *SEED_FILES, "--expect-clean")
    except subprocess.CalledProcessError as error:
        sys.exit(f"{(error.stderr or '').strip() or error}. Run on a fresh stack.")
    counts = json.loads(output.splitlines()[-1])
    seeds = [entry for path in SEED_FILES.values() for entry in yaml.safe_load(path.read_text())["reviews"]]
    customer_ids = sorted({seed["customer_id"] for seed in seeds})
    token = inside(container, "python", "-m", "app.dev_token", "--role", "analyst", "--sub", "u_eval_seeds",
                   "--customers", *customer_ids)  # fmt: skip
    listed = set()
    for outcome in sorted({seed["outcome"] for seed in seeds}):
        page = client.get("/cases", params={"status": outcome, "limit": 200}, headers=bearer(token)).json()
        listed |= {row["case_id"] for row in page.get("cases", [])}
    if missing := [seed["case_id"] for seed in seeds if seed["case_id"] not in listed]:
        sys.exit(f"The service does not list these seeded reviews: {', '.join(missing)}. Is --api the stack of --container?")
    return (
        f"{len(seeds)}: {counts['already_there']} there at start, {counts['seeded']} added by the runner, "
        f"{counts['embedded']} embedded"
    )


def steered(data: dict[str, dict]) -> dict[str, list[str]]:
    """Per customer, the memos of its bank feed: text written to steer the reviewer (c_009, c_203). A `note` is a
    plain description and is not one."""
    found = {}
    for cid, customer in data.items():
        events = (customer.get("signal_window") or {}).get("bank_events") or []
        if memos := [" ".join(str(event["memo"]).split()) for event in events if event.get("memo")]:
            found[cid] = memos
    return found


def wall_time(client: httpx.Client, token: str, case_id: str) -> float | None:
    """Seconds from the case's opening to its report, from the service's own timestamps on the case list. Read
    before the decision, which moves updated_at. None when the list does not show the case."""
    page = client.get("/cases", params={"status": "needs_approval", "limit": 200}, headers=bearer(token)).json()
    row = next((row for row in page.get("cases", []) if row["case_id"] == case_id), None)
    if row is None:
        return None
    opened, reported = datetime.fromisoformat(row["created_at"]), datetime.fromisoformat(row["updated_at"])
    return round((reported - opened).total_seconds(), 1)


def langfuse_api() -> tuple[object | None, str]:
    """LangFuse's API client from the runner's environment, with tracing off since the runner only reads, or None;
    and what the summary says of usage."""
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None, "not recorded: LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not set where the runner runs"
    try:
        from langfuse import Langfuse

        api = Langfuse(tracing_enabled=False).api
    except ImportError:
        return None, "not recorded: the langfuse package is not installed where the runner runs"
    except Exception as error:  # after the paid runs: a client that cannot be made loses usage, not the summary
        return None, f"not recorded: the LangFuse client could not be made ({type(error).__name__}: {error})"
    return api, f"read from LangFuse {USAGE_WAIT} seconds after the last run, session = case id"


def _tokens(usage: dict, side: str) -> int | None:
    """Every token of one side of a call. LangFuse splits parts out of input and output (input_cache_read,
    output_reasoning) and takes them off the base count, so they are added back. A priority tier is no such part."""
    if not isinstance(usage.get(side), int):
        return None
    parts = (value for key, value in usage.items() if key.startswith(f"{side}_") and "priority" not in key)
    return usage[side] + sum(value for value in parts if isinstance(value, int))


def generations(api, session_id: str) -> list[dict]:
    """Every GENERATION of one LangFuse session, from every page, as plain records."""
    found, cursor = [], None
    while True:
        page = api.observations.get_many(
            session_id=session_id, type="GENERATION", fields="core,basic,usage,model", limit=100, cursor=cursor
        )
        for observation in page.data:
            usage = observation.usage_details or {}
            found.append({
                "id": observation.id,
                "type": observation.type,
                "parent": observation.parent_observation_id,
                "input": _tokens(usage, "input"),
                "output": _tokens(usage, "output"),
                "cost": observation.total_cost,
            })  # fmt: skip
        cursor = page.meta.cursor
        if not cursor or not page.data:
            return found


def _total(values: list) -> float | None:
    """A sum, or None when a part is missing: a missing cost is not $0."""
    return None if None in values else sum(values)


def usage_of(observations: list[dict]) -> dict | None:
    """Model calls, tokens and cost of one case, from its generations. None when there is none yet.

    Every model call is recorded twice: the LangChain callback's generation, and the Anthropic SDK's generation
    inside it. Only the outer one counts: one whose parent is not itself a generation. Its cost counts the thinking
    tokens, which are billed as output; the inner one's leaves them out."""
    ids = {item["id"] for item in observations if item["type"] == "GENERATION"}
    outer = [item for item in observations if item["type"] == "GENERATION" and item["parent"] not in ids]
    if not outer:
        return None
    return {
        "calls": len(outer),
        "input_tokens": _total([item["input"] for item in outer]),
        "output_tokens": _total([item["output"] for item in outer]),
        "cost_usd": _total([item["cost"] for item in outer]),
    }


def read_usage(api, runs: list[dict]) -> dict[str, dict]:
    """The usage of every run that opened a case, put on its record, and by case id for usage.json.
    After two failures in a row the rest is not asked for: a LangFuse that hangs would hold up the summary."""
    found, failed = {}, 0
    for run in runs:
        if "case_id" not in run:
            continue
        if failed >= 2:
            found[run["case_id"]] = {"not_recorded": "not asked: LangFuse failed twice in a row"}
            continue
        try:
            usage = usage_of(generations(api, run["case_id"]))
            failed = 0
        except Exception as error:  # LangFuse failing loses usage, never a paid run
            found[run["case_id"]], failed = {"not_recorded": f"{type(error).__name__}: {error}"}, failed + 1
            continue
        if usage is None:
            found[run["case_id"]] = {"not_recorded": "LangFuse holds no generation for this case"}
        else:
            found[run["case_id"]] = run["usage"] = usage
    return found


def outage_of(page: dict) -> str | None:
    """Why a case waits for Bank or Card, or failed for want of them, as the service says it. None otherwise."""
    if page.get("status") == WAITING_FOR_DATA:
        return (page.get("outage") or {}).get("message") or "the case waits for Bank or Card"
    if page.get("status") == "failed" and page.get("outage"):
        return page["outage"]["message"]
    return None


def one_run(client: httpx.Client, tokens: dict, case: dict, signal: dict, rep: int, timeout: int) -> dict:
    """One case through the system once. Returns its record, with `closed` saying whether the case was closed."""
    record = {"case": case["id"], "rep": rep, "closed": True}
    started = time.monotonic()
    sent = client.post("/signals", json=signal, headers=bearer(tokens["service"]))
    if sent.status_code >= 300 or sent.json()["result"] != "opened":
        # "joined" means the customer already had an open case, which would be graded as this run's.
        return {**record, "closed": False, "error": f"the signal was not a new case: http {sent.status_code} {sent.text[:200]}"}
    record["case_id"] = case_id = sent.json()["case_id"]

    while True:
        page = client.get(f"/cases/{case_id}", headers=bearer(tokens["analyst"])).json()
        if page.get("status") not in WORKING:
            break
        if time.monotonic() - started > timeout:
            return {**record, "closed": False, "error": f"still {page['status']} after {timeout} seconds"}
        time.sleep(2)
    record["seconds"] = round(time.monotonic() - started)

    if outage := outage_of(page):
        # Bank or Card gave nothing: the case waits for it, or failed for want of it. Read from the case's own status,
        # so a path page that cannot be read hides nothing. Not graded: it would measure the stack, not the agent.
        # A waiting case is open, so the customer's next run is not sent.
        return {**record, "closed": page["status"] == "failed", "outage": outage,
                "error": f"Bank or Card did not answer, so the run is not graded: {outage}"}
    if page.get("status") != "needs_approval" or not page.get("report"):
        # failed: the model or a specialist raised. The service log has the reason.
        return {**record, "closed": page.get("status") == "failed", "error": f"the case ended as {page.get('status', page)}"}
    # The path first: its records are what a spec in must_cite is matched against. Whatever goes wrong with it, the
    # answer is graded all the same (a spec then is not), and the case still closed.
    path = records = None
    try:
        answer = client.get(f"/cases/{case_id}/path", headers=bearer(tokens["analyst"]))
        if answer.status_code == 200:
            path = answer.json()
            records = path.get("records")
        else:
            record["path_error"] = f"http {answer.status_code} {answer.text[:200]}"
    except Exception as error:
        path, record["path_error"] = None, f"{type(error).__name__}: {error}"
    record |= {"report": page["report"], "grades": grade(case, page["report"], records)}
    if path is not None:
        try:
            record |= {"path": path, "path_grades": grade_path(case, page["report"], path)}
        except Exception as error:
            record["path_error"] = f"{type(error).__name__}: {error}"
    try:
        if (seconds := wall_time(client, tokens["analyst"], case_id)) is not None:
            record["wall_seconds"] = seconds
    except Exception as error:  # the summary then times the run by the runner's clock
        record["wall_error"] = f"{type(error).__name__}: {error}"

    closed = client.post(f"/cases/{case_id}/decision", json={"decision": "reject"}, headers=bearer(tokens["analyst"]))
    if closed.status_code != 200 or closed.json()["status"] != "rejected":
        record |= {"closed": False, "not_closed": f"http {closed.status_code} {closed.text[:200]}"}
    return record


def runs_of(client: httpx.Client, tokens: dict, case: dict, signal: dict, run_id: str, reps: int, timeout: int) -> Iterator[dict]:
    """Every run of one customer, one after another. Nothing more is sent once a run leaves its case open."""
    for rep in range(1, reps + 1):
        try:
            record = one_run(client, tokens, case, {**signal, "signal_id": f"sig_{run_id}_{case['id']}_{rep}"}, rep, timeout)
        except Exception as error:  # whatever broke, the other customers' paid runs are kept
            record = {"case": case["id"], "rep": rep, "closed": False, "error": f"{type(error).__name__}: {error}"}
        yield record
        if not record["closed"]:
            # The next signal would join the open case and be graded as a run of its own.
            for later in range(rep + 1, reps + 1):
                yield {"case": case["id"], "rep": later, "error": f"not run: run {rep} left the case open"}
            return


def refusals(client: httpx.Client, tokens: dict, golden: dict, valid: dict, case_id: str | None, run_id: str) -> list[dict]:
    """The requests the service has to refuse. `valid` is a signal it would accept."""

    def signal(n: str, **changes) -> dict:
        return {**valid, "signal_id": f"sig_{run_id}_{n}", **changes}

    def send(body: dict, token: str) -> httpx.Response:
        return client.post("/signals", json=body, headers=bearer(token))

    def read(headers: dict) -> httpx.Response:
        if case_id is None:
            raise LookupError("no run opened a case to ask for")
        return client.get(f"/cases/{case_id}", headers=headers)

    how = {
        "t_01": lambda: send(signal("t_01", review_reasons=[]), tokens["service"]),
        "t_02": lambda: send(signal("t_02", customer_id="c_099"), tokens["service"]),
        "t_03": lambda: read({}),
        "t_04": lambda: read(bearer(tokens["other_analyst"])),
        "t_05": lambda: send(signal("t_05"), tokens["analyst"]),
    }
    records = []
    for request in golden["requests"]:
        try:
            answer = how[request["id"]]()
            body = answer.json() if answer.headers.get("content-type", "").startswith("application/json") else {}
        except (LookupError, ValueError, httpx.HTTPError) as error:
            records.append({**request, "error": str(error)})
            continue
        got = {"status": answer.status_code, "case_opened": isinstance(body, dict) and "case_id" in body}
        got = {key: got[key] for key in request["expect"]}
        records.append({**request, "got": got, "passed": got == request["expect"]})
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8004", help="where the Orchestrator answers")
    parser.add_argument("--container", required=True, help='how to run a command in its container, such as "docker compose exec -T orchestrator"')
    parser.add_argument("--reps", type=int, default=3, help="runs of each case (default 3)")
    parser.add_argument("--only", nargs="*", help="run these customer cases only")
    parser.add_argument("--workers", type=int, default=5, help="customers run side by side (default 5)")
    parser.add_argument("--timeout", type=int, default=420, help="seconds to wait for one investigation")
    parser.add_argument("--out", type=Path, default=ROOT / "eval" / "results")
    parser.add_argument("--arm", required=True, choices=ARMS, help="the flow the stack runs: one arm per fresh stack")
    parser.add_argument("--specialists", default="real", choices=SPECIALISTS,
                        help="who the stack has answer its questions (SPECIALISTS): real (default) or stand_in")  # fmt: skip
    args = parser.parse_args()

    golden = yaml.safe_load((ROOT / "eval" / "golden_set.yaml").read_text())
    today, data = customers()
    cases = [case for case in golden["customers"] if not args.only or case["id"] in args.only]
    ids = [case["id"] for case in cases]
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{args.arm}"
    about = {
        "run_id": run_id,
        **stack(args.container, args.arm, args.specialists),
        "Code": subprocess.run(["git", "describe", "--always", "--dirty"], cwd=ROOT, capture_output=True, text=True).stdout.strip(),
    }
    tokens = {
        "service": inside(args.container, "python", "-m", "app.dev_token", "--role", "service", "--sub", "eval-signals"),
        "analyst": inside(args.container, "python", "-m", "app.dev_token", "--role", "analyst", "--sub", "u_eval_analyst", "--customers", *ids),
        "other_analyst": inside(args.container, "python", "-m", "app.dev_token", "--role", "analyst", "--sub", "u_eval_other", "--customers", "c_none"),
    }
    folder = args.out / run_id
    runs, lock = [], threading.Lock()

    with httpx.Client(base_url=args.api, timeout=60) as client:
        waiting = client.get("/cases", params={"limit": 1}, headers=bearer(tokens["analyst"])).json()
        if waiting.get("total") != 0:
            # A signal for a customer with an open case joins that case, so no run could be told from an old one.
            sys.exit(f"These customers already have open cases here, or the list could not be read: {waiting}. Run on a fresh stack.")
        # The service has answered, so it has made its tables and seeded at start.
        about["Seeded reviews"] = seed_history(client, args.container)
        folder.mkdir(parents=True)

        with (folder / "runs.jsonl").open("w") as log:

            def one_customer(case: dict) -> None:
                for record in runs_of(client, tokens, case, signal_for(data[case["id"]], today), run_id, args.reps, args.timeout):
                    record = {"arm": args.arm, "specialists": args.specialists, **record}
                    with lock:
                        runs.append(record)
                        log.write(json.dumps(record) + "\n")
                        log.flush()
                        got = record["report"]["recommendation"] if "grades" in record else record["error"]
                        print(f"{case['id']} run {record['rep']}: {got}", flush=True)

            with ThreadPoolExecutor(args.workers) as pool:
                list(pool.map(one_customer, cases))

        case_id = next((run["case_id"] for run in runs if "case_id" in run), None)
        valid = signal_for(data[ids[0]], today)
        requests = refusals(client, tokens, golden, valid, case_id, run_id)

    (folder / "requests.json").write_text(json.dumps(requests, indent=2) + "\n")
    try:  # after the paid runs: whatever goes wrong here loses usage, never the summary
        api, usage_note = langfuse_api()
        if api is not None:
            print(f"Reading usage from LangFuse in {USAGE_WAIT} seconds.", flush=True)
            time.sleep(USAGE_WAIT)
            usage = read_usage(api, runs)
            (folder / "usage.json").write_text(json.dumps(usage, indent=2) + "\n")
            if missing := sum("not_recorded" in found for found in usage.values()):
                usage_note += f"; not recorded for {missing} of {len(usage)} cases, usage.json says why"
    except Exception as error:
        usage_note = f"not recorded: {type(error).__name__}: {error}"
    text = summary(golden, runs, requests, args.reps, about, steering=steered(data), usage_note=usage_note)
    (folder / "summary.md").write_text(text)
    print(f"\n{text}\nWritten to {folder}")
    if down := [run for run in runs if run.get("outage")]:
        sys.exit(f"Bank or Card did not answer during {len(down)} run(s), which are not graded: the results are not "
                 f"a measure of the agent. Check the stack and run again. First: {down[0]['outage']}")


if __name__ == "__main__":
    main()
