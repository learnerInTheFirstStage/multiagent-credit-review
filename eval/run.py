"""Run the golden dataset against a running stack, over HTTP, and grade it.

    docker compose -p goldenset -f docker-compose.yml -f eval/docker-compose.eval.yml up -d --build postgres card orchestrator
    python eval/run.py --container "docker compose -p goldenset exec -T orchestrator"

Every customer case is one signal sent to POST /signals, waited for, read from
GET /cases/{id} and graded. The case is then rejected, which closes it: a
customer has one open case at a time, so the next run of that customer needs it
closed. Runs of one customer go one after another, customers side by side.

This spends money on the model the stack is set to: about 60 runs for the whole set.
Results go to eval/results/<run id>/: runs.jsonl as each run ends, summary.md at the end.
"""

import argparse
import json
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

from grade import grade, summary  # noqa: E402
from signals import customers, signal_for  # noqa: E402

WORKING = ("queued", "investigating")


def inside(container: str, *command: str) -> str:
    """Run a command in the Orchestrator's container. Tokens are made there, with its secret."""
    done = subprocess.run([*shlex.split(container), *command], check=True, capture_output=True, text=True)
    return done.stdout.strip()


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


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

    if page.get("status") != "needs_approval" or not page.get("report"):
        # failed: the model or a specialist raised. The service log has the reason.
        return {**record, "closed": page.get("status") == "failed", "error": f"the case ended as {page.get('status', page)}"}
    record |= {"report": page["report"], "grades": grade(case, page["report"])}

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
    args = parser.parse_args()

    golden = yaml.safe_load((ROOT / "eval" / "golden_set.yaml").read_text())
    today, data = customers()
    cases = [case for case in golden["customers"] if not args.only or case["id"] in args.only]
    ids = [case["id"] for case in cases]
    tokens = {
        "service": inside(args.container, "python", "-m", "app.dev_token", "--role", "service", "--sub", "eval-signals"),
        "analyst": inside(args.container, "python", "-m", "app.dev_token", "--role", "analyst", "--sub", "u_eval_analyst", "--customers", *ids),
        "other_analyst": inside(args.container, "python", "-m", "app.dev_token", "--role", "analyst", "--sub", "u_eval_other", "--customers", "c_none"),
    }
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = args.out / run_id
    folder.mkdir(parents=True)
    about = {
        "run_id": run_id,
        "Model": inside(args.container, "python", "-c", "import os; print(os.environ.get('LLM_MODEL') or 'none set', '| structured output:', os.environ.get('LLM_STRUCTURED_OUTPUT') or 'default')"),
        "Code": subprocess.run(["git", "describe", "--always", "--dirty"], cwd=ROOT, capture_output=True, text=True).stdout.strip(),
    }
    runs, lock = [], threading.Lock()

    with httpx.Client(base_url=args.api, timeout=60) as client, (folder / "runs.jsonl").open("w") as log:
        waiting = client.get("/cases", params={"limit": 1}, headers=bearer(tokens["analyst"])).json()
        if waiting.get("total") != 0:
            # A signal for a customer with an open case joins that case, so no run could be told from an old one.
            sys.exit(f"These customers already have open cases here, or the list could not be read: {waiting}. Run on a fresh stack.")

        def one_customer(case: dict) -> None:
            for record in runs_of(client, tokens, case, signal_for(data[case["id"]], today), run_id, args.reps, args.timeout):
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
    text = summary(golden, runs, requests, args.reps, about)
    (folder / "summary.md").write_text(text)
    print(f"\n{text}\nWritten to {folder}")


if __name__ == "__main__":
    main()
