"""Run the Orchestrator's real past-case search on every query in situations.yaml, for each embedder.

Runs inside the Orchestrator image, on an empty database of its own, so it measures exactly what
SearchPastCases does: app.retrieval.search, with its date, outcome, check and embedder filters, over
the evaluation's seeded reviews. Nothing in the service changes. Prints one JSON document; score.py
reads it. No model is called.

    docker compose -p ragas -f docker-compose.yml -f eval/docker-compose.eval.yml up -d --wait postgres
    mkdir -p eval/retrieval/results
    docker compose -p ragas -f docker-compose.yml -f eval/docker-compose.eval.yml run --rm --no-deps -T \
        -e PYTHONPATH=/app -v "$PWD/eval/retrieval:/eval/retrieval:ro" orchestrator \
        python /eval/retrieval/retrieve.py > eval/retrieval/results/retrieved.json

PYTHONPATH=/app: a script run by path does not have the image's working directory on its path. The
queries are situations.yaml's, and generated.yaml's when it is there. The database must hold no reviews
dated before the data clock but the seeds (history.seed with expect_clean), so a run never measures
somebody's earlier cases.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import yaml

from app import history, retrieval
from app.agents import mock
from app.db import Base, SessionLocal, engine
from app.db import models  # noqa: F401  registers the tables on Base

SEEDS = ["/eval/history.yaml", "/eval/history_eval.yaml"]
EMBEDDERS = ["hash:384", "fastembed:BAAI/bge-small-en-v1.5"]
K = 5  # more than TOP_K (3), so score.py can show what a longer list would add


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--situations", default="/eval/retrieval/situations.yaml")
    parser.add_argument("--generated", default="/eval/retrieval/generated.yaml", help="generate.py's queries, if there")
    parser.add_argument("--embedder", action="append", help=f"repeatable (default: {', '.join(EMBEDDERS)})")
    parser.add_argument("-k", type=int, default=K)
    args = parser.parse_args()
    situations = yaml.safe_load(open(args.situations))["situations"]
    first_case = {s["id"]: s["cases"][0] for s in situations}
    queries = [(s["id"], style, q) for s in situations for style, q in s["queries"].items()]
    if os.path.exists(args.generated):
        queries += [(g["situation"], "generated", g["query"]) for g in yaml.safe_load(open(args.generated))["queries"]]
    clock = mock._data()["today"]  # the data clock every signal is read on

    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        try:
            counts = history.seed(session, history.read_seeds(SEEDS), expect_clean=True, clock=clock)
        except ValueError as error:
            sys.exit(f"Not run: {error}. Start from an empty database (a fresh compose project).")
        session.commit()

    out = {"as_of": str(clock), "k": args.k, "seeded": counts, "runs": []}
    for setting in args.embedder or EMBEDDERS:
        embedder = retrieval.load(setting)
        run = {"embedder": setting, "available": embedder is not None, "results": []}
        if embedder is not None:
            for situation, style, query in queries:
                # The customer only sets same_customer on a result, never the ranking.
                found = retrieval.search(embedder, customer_id=first_case[situation], as_of=str(clock), query=query,
                                         k=args.k)
                # `shown` is what the investigator reads for each one: masked, with no ids. score.py's judge reads it.
                run["results"].append({"situation": situation, "style": style, "query": query,
                                       "returned": found.case_ids, "shown": found.results})
        out["runs"].append(run)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
