"""Run the Card Service agent's test set: every case through the agent, graded by checks.py.

From the repo root, with the Orchestrator's Python environment (the runner imports the agent):

    # A live Card Service (docker compose up -d postgres card), no model: free and deterministic.
    services/orchestrator/.venv/bin/python eval/card_service/run.py --no-model
    # The same with the model in LLM_MODEL / LLM_API_KEY. Spends money; the owner starts it.
    services/orchestrator/.venv/bin/python eval/card_service/run.py
    # Record Card's answers for the offline test, or replay them instead of calling Card.
    services/orchestrator/.venv/bin/python eval/card_service/run.py --no-model --record
    services/orchestrator/.venv/bin/python eval/card_service/run.py --no-model --replay

The recording holds only the calls code makes with no model (each topic's preflight). Replayed, any
other call fails its case (check `card_calls`) instead of passing as "Card did not answer".

Results: results/<stamp>-<mode>.json and results/latest.md (git-ignored). Exit code 1 if a case failed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "services" / "orchestrator"))
# The agent's package imports the service's settings; the set needs no database of its own.
os.environ.setdefault("DATABASE_URL", "sqlite://")

import httpx  # noqa: E402
import yaml  # noqa: E402

RECORDED = HERE / "recorded.json"


def _load(name: str, path: Path):
    """A module of this folder under its own name, so it never clashes with eval/run.py or eval/grade.py."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checks = _load("card_service_checks", HERE / "checks.py")


def load_cases(path: Path = HERE / "cases.yaml") -> tuple[list[dict], dict]:
    data = yaml.safe_load(path.read_text())
    return data["cases"], data["signal_window"]


def _key(request: httpx.Request) -> str:
    customer, tool = request.url.path.split("/")[2], request.url.path.rsplit("/", 1)[-1]
    query = "&".join(f"{k}={v}" for k, v in sorted(parse_qsl(request.url.query.decode())))
    return f"{customer}/{tool}?{query}"


class Recording(httpx.BaseTransport):
    """Passes calls to Card (or answers from a recording) and keeps every record Card returned.

    `problems` holds what the runner itself got wrong: a replayed call with no recording (the agent
    would quietly report Card unreachable) or a reply that could not be recorded. Each fails the case.
    """

    def __init__(self, *, store: dict, replay: bool, down: bool):
        self.store, self.replay, self.down = store, replay, down
        self.inner = None if replay or down else httpx.HTTPTransport()
        self.returned: dict[str, dict] = {}
        self.problems: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("Card is down for this case", request=request)
        key = _key(request)
        if self.replay:
            if key not in self.store:
                self.problems.append(f"no recording for {key}")
                raise httpx.ConnectError(f"no recording for {key}", request=request)
            status, body = self.store[key]["status"], self.store[key]["body"]
        else:
            response = self.inner.handle_request(request)
            response.read()
            status = response.status_code
            try:
                body = response.json()
            except ValueError:
                # An HTML 500 from a proxy, say: hand it to the agent as it came, and record nothing.
                self.problems.append(f"{key}: Card answered {status} with a body that is not JSON")
                return httpx.Response(status, content=response.content, request=request,
                                      headers={"content-type": response.headers.get("content-type", "text/plain")})
            self.store[key] = {"status": status, "body": body}
        if status == 200:
            for item in body.get("evidence", []):
                self.returned.setdefault(item["id"], item)
        return httpx.Response(status, json=body, request=request)


def run_case(case: dict, window: dict, *, store: dict, replay: bool, model=None) -> tuple[dict, dict, list[str]]:
    """The agent's answer, every record Card returned, and the runner's own problems with Card's calls."""
    from app.agents.card_service import card_service
    from contracts import InvestigateRequest

    transport = Recording(store=store, replay=replay, down=case.get("card") == "down")
    request = InvestigateRequest(case_id=f"case_{case['id']}", customer_id=case["customer"], question=case["question"],
                                 window_start=window["start"], window_end=window["end"], topic=case.get("topic"))
    with httpx.Client(transport=transport) as http:
        response = card_service(request, http=http, model=model)
    return response.model_dump(mode="json"), transport.returned, transport.problems


def run(*, replay: bool = False, record: bool = False, cases: list[dict] | None = None) -> list[dict]:
    all_cases, window = load_cases()
    store = json.loads(RECORDED.read_text()) if replay else {}
    results = []
    for case in cases or all_cases:
        response, returned, problems = run_case(case, window, store=store, replay=replay)
        results.append({**checks.grade(case, response, returned, problems), "about": case.get("about", ""),
                        "response": response})
    if record:
        RECORDED.write_text(json.dumps(store, indent=1, sort_keys=True) + "\n")
    return results


def summary(results: list[dict], mode: str) -> str:
    passed = sum(r["passed"] for r in results)
    lines = [f"# Card Service agent test set, {mode}", "",
             f"{passed} of {len(results)} cases passed.", "", "| Case | Result | Failed checks |", "|---|---|---|"]
    for r in results:
        failed = "; ".join(f"{name}: {why}" for name, why in r["checks"].items() if why)
        lines.append(f"| {r['id']} | {'pass' if r['passed'] else '**FAIL**'} | {failed.replace('|', '/')} |")
    lines += ["", "## Findings per case", ""]
    for r in results:
        lines.append(f"**{r['id']}** {r['about']}")
        for f in r["response"]["findings"]:
            lines.append(f"- {f['claim']} ({', '.join(f['evidence_ids'])})")
        for reason in r["response"]["could_not_determine"]:
            lines.append(f"- could not determine: {reason}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--card-url", default=os.environ.get("CARD_URL", "http://localhost:8003"))
    parser.add_argument("--no-model", action="store_true", help="code answers from the records alone")
    parser.add_argument("--record", action="store_true", help=f"save Card's answers to {RECORDED.name}")
    parser.add_argument("--replay", action="store_true",
                        help=f"answer from {RECORDED.name} instead of calling Card; a call with no recording "
                             "(a model's own tool call, say) fails the case")
    args = parser.parse_args()
    os.environ["CARD_URL"] = args.card_url
    # Card checks the agent's token with the same secret; this is docker-compose.yml's local fallback.
    os.environ.setdefault("JWT_SECRET", "local-jwt-secret-for-development-only")
    if args.no_model:
        os.environ.pop("LLM_MODEL", None)
    mode = "no model" if not os.environ.get("LLM_MODEL") else os.environ["LLM_MODEL"]
    results = run(replay=args.replay, record=args.record)
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    (out / f"{stamp}-{mode.replace(':', '_').replace(' ', '_')}.json").write_text(json.dumps(results, indent=1))
    text = summary(results, mode + (" (replayed)" if args.replay else ""))
    (out / "latest.md").write_text(text)
    print(text.split("\n## ")[0])
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
