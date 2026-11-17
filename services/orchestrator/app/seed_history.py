"""Seed the closed reviews that customers' histories start from.

    python -m app.seed_history /eval/history.yaml
    python -m app.seed_history /eval/history.yaml /eval/history_eval.yaml --expect-clean

Each entry becomes a closed case and its review, where they are not there yet,
so running it twice changes nothing. Prints {"seeded", "already_there", "embedded"}.
--expect-clean refuses a database that holds reviews that are not seeds, dated
before the data clock, or a seed that is there with other content.
"""

import argparse
import json
import os
from pathlib import Path

from app import history, retrieval
from app.agents import mock
from app.db import SessionLocal

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", help="seed files, such as /eval/history.yaml")
    parser.add_argument(
        "--expect-clean",
        action="store_true",
        help="fail unless every review dated before the data clock is a seed of these files",
    )
    args = parser.parse_args()
    # A file named here is meant to be read. read_seeds skips a missing one, as a listed setting may name it.
    if missing := [path for path in args.paths if not Path(path).is_file()]:
        parser.exit(1, f"Not seeded: no file at {', '.join(missing)}\n")
    # The data clock: the date the customer files say every signal is read on.
    clock = mock._data()["today"] if args.expect_clean else None
    embedder = retrieval.load(os.environ.get("EMBEDDINGS_MODEL"))
    with SessionLocal() as session:
        try:
            counts = history.seed(session, history.read_seeds(args.paths), expect_clean=args.expect_clean, clock=clock)
        except ValueError as error:
            parser.exit(1, f"Not seeded: {error}\n")
        counts["embedded"] = 0
        while embedder is not None and (filled := retrieval.fill_vectors(session, embedder)):
            counts["embedded"] += filled
        session.commit()
    print(json.dumps(counts))
