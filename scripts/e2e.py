"""One case through the whole running system, end to end, with the stack's real model.

The signal opens a case. The Orchestrator agent investigates it by asking the real Cash Flow and Card specialists,
which call Bank and Card. Code checks the report, the analyst decides, a manager approves when the case needs it,
Card carries out the action, and Card's snapshot is read again and checked against the report.

This spends money on the model the stack is set to: one investigation, that is the Orchestrator agent's questions
(at most 6), each real specialist's own model calls, the gap review and up to two report drafts. The specialists
are the real ones unless the stack runs with SPECIALISTS=stand_in; the script prints what the stack runs before it
sends the signal.

Run it from the checkout root on a stack of its own, started with your env file. From the main checkout compose
reads the repo-root .env, so --env-file can be left out; a worktree has no .env, so point it at the main checkout's:

    docker compose -p e2e --env-file <main checkout>/.env -f docker-compose.yml -f eval/docker-compose.eval.yml up -d --build
    python scripts/e2e.py                        # c_010, the analyst approves
    python scripts/e2e.py --decide reject        # the analyst rejects: nothing reaches Card
    docker compose -p e2e down -v

The stack takes the same host ports as the main one (Orchestrator 127.0.0.1:8004, Bank 8002, Card 8003, the
gateway 8080), so it cannot run beside another stack of this repo. Use `down -v` between runs: a customer has one
open case at a time, and after a pause or a cut Card no longer starts from its seeded state. The script stops
before the signal when Card's card is not active or its limit is not the signal's.

Expected for c_010 (Quarry Hill Brewing): payroll and tax explain the bank side, but the last repayment failed and
is not paid since. The report recommends pause_card citing G-08b. Every card pause needs the manager step, so
u_analyst approves, then u_manager approves, and Card's card_status goes from active to paused. The golden set accepts
pause_card only (since 2026-11-17), so a reduce_limit report is graded wrong, but the script still checks Card against
whatever the report recommends:

- pause_card: card_status was active and is paused, the limit unchanged.
- reduce_limit: credit_limit_cents is the report's engine_limit_cents, card_status unchanged.
- keep_limit, request_documents, needs_human, or a rejection: nothing changed on Card and no action was sent. An
  approved request_documents leaves the case awaiting_documents: the script does not record what the documents showed.

Tokens are made inside the containers (`app.dev_token` in the Orchestrator's and in Card's), so they carry the
stack's own JWT_SECRET. With --container '' they are made here instead, with JWT_SECRET from the environment
(default the compose fallback; export it when the .env sets one). The people are u_analyst (analyst) and u_manager (manager); the Orchestrator and
Card check a token's signature and claims only, there is no user list.

Without a model (no LLM_MODEL) the case waits as queued and the script stops at --timeout. It stays open on that
stack until `down -v`.

When Bank or Card gives nothing at all, the case waits as waiting_for_data and the Orchestrator asks again later.
The script keeps waiting, prints why each time the reason changes, and at --timeout fails with that reason. A case
that failed because Bank or Card did not answer fails the run with the reason. The run also fails, before anything
is decided, when the case's path page cannot be read.

Exit status: 0 every check passed, 1 a check failed or a request went wrong, 2 the case did not finish in time.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from grade import grade, grade_path  # noqa: E402
from run import WAITING_FOR_DATA, WORKING, bearer, inside, outage_of  # noqa: E402
from signals import customers, signal_for  # noqa: E402

ANALYST, MANAGER = "u_analyst", "u_manager"
COMPOSE_JWT_SECRET = "local-jwt-secret-for-development-only"  # docker-compose.yml's fallback
ACTIONS = ("reduce_limit", "pause_card")  # the recommendations Card carries out
# Run inside the Orchestrator's container: what the stack runs with. Names and switches only, never a key.
STACK = """
import json, os
print(json.dumps({
    "model": os.environ.get("LLM_MODEL") or None,
    "specialists": "stand_in" if os.environ.get("SPECIALISTS") == "stand_in" else "real",
    "flow": os.environ.get("INVESTIGATION_FLOW") or "agent",
    "tracing": bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")),
}))
"""


class Failed(Exception):
    """A step went wrong. The message says which step and why."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def money(cents: int | None) -> str:
    return "-" if cents is None else f"{cents} (${cents / 100:,.2f})"


def why(response: httpx.Response) -> str:
    """A service's own words: the Orchestrator's {"error", "message"}, or Card's {"detail": {...}}."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    detail = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("error") or detail)[:300]
    return str(detail)[:300]


def stack_line(found: dict | None) -> str:
    if found is None:
        return "not read (no --container)"
    model = found["model"] or "NONE: no LLM_MODEL, so the case will wait as queued and this run will time out"
    return (f"model={model} | specialists={found['specialists']} | flow={found['flow']} | "
            f"tracing={'on' if found['tracing'] else 'off'}")


def make_tokens(customer: str, container: str, card_container: str) -> dict[str, str]:
    """The signal sender, the two people, and Card's snapshot reader."""
    people = {
        "service": ("service", "e2e-signals", []),
        "analyst": ("analyst", ANALYST, [customer]),
        "manager": ("manager", MANAGER, [customer]),
    }
    if container:
        tokens = {
            name: inside(container, "python", "-m", "app.dev_token", "--role", role, "--sub", sub, "--customers", *seen)
            for name, (role, sub, seen) in people.items()
        }
        tokens["card"] = inside(card_container, "python", "-m", "app.dev_token",
                                "--sub", "e2e-snapshots", "--scope", "snapshots:read")
        return tokens
    import jwt

    secret = os.environ.get("JWT_SECRET") or COMPOSE_JWT_SECRET
    expires = int(time.time()) + 4 * 3600
    tokens = {
        name: jwt.encode({"sub": sub, "role": role, "customers": seen, "exp": expires}, secret, algorithm="HS256")
        for name, (role, sub, seen) in people.items()
    }
    tokens["card"] = jwt.encode({"sub": "e2e-snapshots", "role": "service", "customers": [],
                                 "scope": ["snapshots:read"], "exp": expires}, secret, algorithm="HS256")
    return tokens


def card_snapshot(client: httpx.Client, card_url: str, token: str, customer: str) -> dict:
    answer = client.get(f"{card_url}/snapshots", params={"customer_id": customer}, headers=bearer(token))
    if answer.status_code != 200:
        raise Failed(f"Card's snapshot of {customer}: http {answer.status_code} {why(answer)}")
    body = answer.json()
    return {**body["customers"][0], "as_of": body["as_of"]}


def card_line(snapshot: dict) -> str:
    return (f"{snapshot['customer_id']} card_status={snapshot['card_status']} "
            f"credit_limit_cents={money(snapshot['credit_limit_cents'])} balance_cents={money(snapshot['balance_cents'])} "
            f"funding={snapshot['funding_account_id']} as_of={snapshot['as_of']}")


def wait(client: httpx.Client, case_id: str, token: str, timeout: int, project: str) -> dict:
    """Poll the case page until the case leaves queued, investigating and waiting_for_data. Prints each status it
    passes, and why the case waits for Bank or Card each time that changes. A case that failed for want of Bank
    or Card fails the run with the reason."""
    started, seen, said = time.monotonic(), None, None
    while True:
        answer = client.get(f"/cases/{case_id}", headers=bearer(token))
        if answer.status_code != 200:
            raise Failed(f"reading case {case_id}: http {answer.status_code} {why(answer)}")
        page = answer.json()
        if page["status"] != seen:
            seen = page["status"]
            print(f"case:       {case_id} is {seen} at {round(time.monotonic() - started)}s")
        outage = outage_of(page)
        if outage and outage != said:
            said = outage
            print(f"outage:     {outage}")
        if seen == "failed" and outage:
            raise Failed(f"case {case_id} failed because Bank or Card did not answer: {outage}. Fix the stack, then "
                         f"POST /cases/{case_id}/retry runs it again from that question")
        if seen not in (*WORKING, WAITING_FOR_DATA):
            return page
        if time.monotonic() - started >= timeout:
            if seen == "queued":
                reason = ("nothing picked it up, which is what a stack with no LLM_MODEL does. The case stays open, "
                          "so the next signal for this customer on this stack joins it")
            elif seen == WAITING_FOR_DATA:
                reason = (f"Bank or Card does not answer: {outage}. docker compose -p {project} logs bank card "
                          "orchestrator")
            else:
                reason = f"the model or a specialist is slow or stuck: docker compose -p {project} logs orchestrator"
            raise Failed(f"case {case_id} is still {seen} after {timeout}s: {reason}. "
                         f"docker compose -p {project} down -v clears the stack", code=2)
        time.sleep(3)


def report_lines(report: dict) -> list[str]:
    lines = [
        f"report:     recommendation={report['recommendation']} likely_cause={report['likely_cause']} "
        f"confidence={report['confidence']} guidelines={','.join(report['guideline_ids']) or '-'} "
        f"engine_limit_cents={money(report.get('engine_limit_cents'))}",
        f"  summary: {report['summary']}",
    ]
    lines += [f"  evidence {n}: {item['claim']} [{', '.join(item['evidence_ids'])}]"
              for n, item in enumerate(report["evidence"], 1)]
    lines += [f"  reason {item['reason']}: {item['finding']} [{', '.join(item['evidence_ids']) or '-'}]"
              for item in report.get("review_reasons", [])]
    lines += [f"  ruled out {item['cause']}: {item['reason']} [{', '.join(item['evidence_ids']) or '-'}]"
              for item in report.get("ruled_out", [])]
    if report.get("next_action"):
        lines.append(f"  next action: {report['next_action']}")
    return lines


def path_lines(path: dict, case_id: str, tracing: str) -> list[str]:
    drafts = path.get("drafts", [])
    lines = [
        f"path:       flow={path.get('flow')} rounds={path.get('rounds')} questions={len(path.get('questions', []))} "
        f"refused={path.get('refusals')} searches={len(path.get('searches', []))} "
        f"drafts={len(drafts)} ({', '.join(str(draft.get('result')) for draft in drafts) or '-'}) "
        f"handed_over_by={path.get('handed_over_by') or '-'}"
    ]
    for n, entry in enumerate(path.get("questions", []), 1):
        focus = " ".join(f"{key}={entry[key]}" for key in ("record_id", "account_id") if entry.get(key))
        lines.append(f"  question {n}: {entry.get('tool')} topic={entry.get('topic', '-')} "
                     f"outcome={entry.get('outcome')} returned={len(entry.get('returned', []))} {focus}".rstrip())
    # runner.thread names the case's LangFuse session by its case id.
    lines.append(f"langfuse:   session {case_id} ({tracing})")
    return lines


def next_decision(step: str | None, decide: str) -> tuple[str, str] | None:
    """Who answers the step the case waits on, and with what: (token name, decision). None when nothing waits, or
    when the case waits for the customer's documents: a person records what they showed."""
    if step is None or step == "documents":
        return None
    if step == "analyst":
        return "analyst", decide
    if step == "manager":
        return "manager", "approve"  # a different person from the analyst
    raise Failed(f"the case waits on the {step} step: an approval is recorded but Card has not confirmed the action")


def decide(client: httpx.Client, case_id: str, page: dict, tokens: dict, decision: str) -> dict:
    """Answer every step the case waits on, analyst first. Returns the case page after the last answer."""
    answered = []
    while (move := next_decision(page.get("waiting_for"), decision)) is not None:
        who, answer = move
        step = page["waiting_for"]
        if step in answered:
            raise Failed(f"the {step} step still waits after {who} answered it")
        sub = ANALYST if who == "analyst" else MANAGER
        response = client.post(f"/cases/{case_id}/decision", json={"decision": answer}, headers=bearer(tokens[who]))
        if response.status_code == 502:
            raise Failed(f"{step} step: {sub} {answer}d, but Card did not confirm the action: {why(response)}. "
                         "The approval is recorded and the case waits on the retry step")
        if response.status_code != 200:
            raise Failed(f"{step} step: {sub} ({who}) could not {answer}: http {response.status_code} {why(response)}")
        page = response.json()
        answered.append(step)
        then = f", waiting for the {page['waiting_for']} step" if page.get("waiting_for") else ""
        print(f"decision:   {step} step, {sub} ({who}) {answer}s; the case is now {page['status']}{then}")
    return page


def action_line(action: dict | None, recommendation: str) -> str:
    if not action:
        return f"action:     nothing sent to Card ({recommendation})"
    result = action.get("result") or {}
    return (f"action:     {action['type']} limit_cents={money(action.get('limit_cents'))} "
            f"approval_id={action.get('approval_id')} approved_by={action.get('approved_by')}; Card answered "
            f"{result.get('status')} by {result.get('approver_role')} {result.get('details')}")


def card_problems(report: dict, page: dict, before: dict, after: dict, decision: str) -> list[str]:
    """What does not fit between the report, the decision, the case's action and Card before and after. Empty is a pass."""
    problems = []
    recommendation, action = report["recommendation"], page.get("action")
    ended, waits = ("rejected" if decision == "reject" else "done"), None
    if decision != "reject" and recommendation == "request_documents":
        ended, waits = "awaiting_documents", "documents"  # approved, it waits for the customer
    if page["status"] != ended or page.get("waiting_for") != waits:
        problems.append(f"the case ended {page['status']} (waiting for {page.get('waiting_for')}), not {ended}")
    changed = {field: (before[field], after[field]) for field in ("card_status", "credit_limit_cents")
               if before[field] != after[field]}

    if decision == "reject" or recommendation not in ACTIONS:
        if action:
            problems.append(f"an action went to Card for a {recommendation} report the analyst chose to {decision}: {action}")
        problems += [f"Card's {field} changed from {old} to {new}, but nothing was to change"
                     for field, (old, new) in changed.items()]
        return problems

    if not action or action.get("type") != recommendation:
        problems.append(f"the case's action is {action and action.get('type')}, not {recommendation}")
    elif (action.get("result") or {}).get("status") != "applied":
        problems.append(f"Card's receipt says {(action.get('result') or {}).get('status')}, not applied")
    if recommendation == "pause_card":
        if before["card_status"] != "active":
            problems.append(f"the card was {before['card_status']} before the case, so the pause cannot be told apart")
        if after["card_status"] != "paused":
            problems.append(f"Card's card_status is {after['card_status']} after an approved pause_card, not paused")
        if "credit_limit_cents" in changed:
            problems.append(f"Card's limit changed {changed['credit_limit_cents']} on a pause")
    else:
        target = report["engine_limit_cents"]
        if after["credit_limit_cents"] != target:
            problems.append(f"Card's credit_limit_cents is {after['credit_limit_cents']} after an approved "
                            f"reduce_limit, not the report's engine_limit_cents {target}")
        if action and action.get("limit_cents") != target:
            problems.append(f"the action sent limit_cents {action.get('limit_cents')}, not the report's {target}")
        if "card_status" in changed:
            problems.append(f"Card's card_status changed {changed['card_status']} on a reduction")
    return problems


def golden_line(customer: str, report: dict, path: dict | None) -> str | None:
    """eval/grade.py's grades of this one report, when the golden set holds the customer. Shown only: no check fails on it."""
    try:
        golden = yaml.safe_load((ROOT / "eval" / "golden_set.yaml").read_text())["customers"]
    except (OSError, KeyError, TypeError, yaml.YAMLError):
        return None
    case = next((case for case in golden if case["id"] == customer), None)
    if case is None:
        return None
    try:
        grades = grade(case, report, path.get("records") if path else None)
        if path is not None:
            grades["guideline"] = grade_path(case, report, path)["guideline"]
    except Exception as error:  # the grader is someone else's code: its failure must not end a paid run
        return f"golden:     not graded: {type(error).__name__}: {error}"
    marks = " ".join(f"{check}={'-' if right is None else 'right' if right else 'wrong'}" for check, right in grades.items())
    return f"golden:     {marks} (eval/grade.py on this one run; not a check)"


def run(args: argparse.Namespace) -> list[str]:
    """The case, step by step. Returns the problems found; raises Failed when it cannot go on."""
    container = f"docker compose -p {args.project} exec -T orchestrator" if args.container is None else args.container
    card_container = (f"docker compose -p {args.project} exec -T card"
                      if args.card_container is None else args.card_container)
    today, data = customers()
    if args.customer not in data:
        raise Failed(f"{args.customer} is in neither data/shared_customers.yaml nor eval/extra_customers.yaml")

    found = json.loads(inside(container, "python", "-c", STACK).splitlines()[-1]) if container else None
    print(f"stack:      {stack_line(found)}")
    if found is None:
        tracing = "whether this stack traces is unknown"
    elif found["tracing"]:
        tracing = "LangFuse names the session by the case id"
    else:
        tracing = "not traced: this stack has no LangFuse keys"
    tokens = make_tokens(args.customer, container, card_container)
    print(f"tokens:     service e2e-signals, analyst {ANALYST}, manager {MANAGER} (both for {args.customer}), "
          f"Card snapshots:read; made {'in the containers' if container else 'here with JWT_SECRET'}")

    problems = []
    with httpx.Client(base_url=args.orchestrator_url, timeout=60) as client:
        before = card_snapshot(client, args.card_url, tokens["card"], args.customer)
        print(f"card before: {card_line(before)}")

        open_cases = client.get("/cases", headers=bearer(tokens["analyst"]))
        if open_cases.status_code != 200:
            raise Failed(f"listing open cases: http {open_cases.status_code} {why(open_cases)}")
        if listed := open_cases.json()["cases"]:
            raise Failed(f"{args.customer} already has an open case {listed[0]['case_id']} ({listed[0]['status']}) on "
                         f"this stack, and a new signal would join it. docker compose -p {args.project} down -v")

        signal = {**signal_for(data[args.customer], today),
                  "signal_id": f"sig_e2e_{datetime.now():%Y%m%d%H%M%S}_{args.customer}"}
        # Checked before the signal, so a stack an earlier run changed costs no investigation. Card seeds every card
        # active, from the same data file the signal is built from.
        if before["card_status"] != "active" or signal["current_limit_cents"] != before["credit_limit_cents"]:
            raise Failed(f"Card shows {args.customer} {before['card_status']} with credit_limit_cents "
                         f"{before['credit_limit_cents']}, not active with the signal's {signal['current_limit_cents']}: "
                         f"an earlier run changed this stack. docker compose -p {args.project} down -v")
        bank = signal["snapshot"]["bank"]
        print(f"signal:     {signal['signal_id']} {args.customer} as_of={signal['as_of']} "
              f"reasons={','.join(signal['review_reasons'])} current_limit_cents={money(signal['current_limit_cents'])} "
              f"engine_limit_cents={money(signal['engine_limit_cents'])} cash_now_cents={money(bank['cash_now_cents'])} "
              f"cash_7d_ago_cents={money(bank['cash_7d_ago_cents'])}")
        sent = client.post("/signals", json=signal, headers=bearer(tokens["service"]))
        if sent.status_code != 201 or sent.json().get("result") != "opened":
            raise Failed(f"the signal opened no new case: http {sent.status_code} {sent.text[:300]}")
        case_id = sent.json()["case_id"]
        print(f"case:       {case_id} opened for {args.customer}, waiting up to {args.timeout}s")

        page = wait(client, case_id, tokens["analyst"], args.timeout, args.project)
        if page["status"] != "needs_approval" or not page.get("report") or page.get("waiting_for") != "analyst":
            raise Failed(f"case {case_id} ended {page['status']} (waiting for {page.get('waiting_for')}) with "
                         f"{'a' if page.get('report') else 'no'} report. A failed case means the model or a specialist "
                         f"raised: docker compose -p {args.project} logs orchestrator")
        report = page["report"]
        print("\n".join(report_lines(report)))

        answer = client.get(f"/cases/{case_id}/path", headers=bearer(tokens["analyst"]))
        if answer.status_code != 200:
            # Before anything is decided: a run whose questions cannot be shown is not a run this script can pass.
            raise Failed(f"the case's path could not be read: http {answer.status_code} {why(answer)}. Nothing was "
                         f"decided; case {case_id} stays open. docker compose -p {args.project} logs orchestrator")
        try:
            path = answer.json()
        except ValueError:
            raise Failed(f"the case's path is not JSON: {answer.text[:200]}. Nothing was decided") from None
        print("\n".join(path_lines(path, case_id, tracing)))
        if line := golden_line(args.customer, report, path):
            print(line)

        try:
            page = decide(client, case_id, page, tokens, args.decide)
        except Failed:
            # A 502 that was not a refusal may follow an action Card applied and lost its answer to: show Card as it is.
            try:
                print(f"card after:  {card_line(card_snapshot(client, args.card_url, tokens['card'], args.customer))}")
            except (Failed, httpx.HTTPError) as error:
                print(f"card after:  could not be read: {error}")
            raise
        print(action_line(page.get("action"), report["recommendation"]))

        after = card_snapshot(client, args.card_url, tokens["card"], args.customer)
        print(f"card after:  {card_line(after)}")
        problems += card_problems(report, page, before, after, args.decide)
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--customer", default="c_010", help="the customer whose signal is sent (default c_010)")
    parser.add_argument("--project", default="e2e", help="the compose project of the stack (default e2e)")
    parser.add_argument("--orchestrator-url", default="http://127.0.0.1:8004")
    parser.add_argument("--card-url", default="http://127.0.0.1:8003")
    parser.add_argument("--container", help="how to run a command in the Orchestrator's container (default "
                        "\"docker compose -p <project> exec -T orchestrator\"; '' makes the tokens here)")
    parser.add_argument("--card-container", help="the same for Card's (default \"docker compose -p <project> exec -T card\")")
    parser.add_argument("--decide", choices=("approve", "reject"), default="approve", help="what the analyst decides")
    parser.add_argument("--timeout", type=int, default=600, help="seconds to wait for the investigation (default 600)")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # each step shows as it happens, in order with a FAIL on stderr

    try:
        problems = run(args)
    except Failed as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(error.code)
    except subprocess.CalledProcessError as error:
        print(f"FAIL: {' '.join(error.cmd[:6])} ... exited {error.returncode}: {(error.stderr or '').strip()[:300]}. "
              f"Is the stack -p {args.project} up?", file=sys.stderr)
        sys.exit(1)
    except httpx.HTTPError as error:
        where = error.request.url if isinstance(error, httpx.RequestError) else ""
        print(f"FAIL: {type(error).__name__} {where}: {error}. Is the stack -p {args.project} up?", file=sys.stderr)
        sys.exit(1)
    if problems:
        print("FAIL:\n" + "\n".join(f"  - {problem}" for problem in problems), file=sys.stderr)
        sys.exit(1)
    print(f"PASS: {args.customer} went from signal to Card and Card matches the report")


if __name__ == "__main__":
    main()
