"""Generate more search queries per situation with Claude, for retrieve.py and score.py.

The hand-written queries in situations.yaml are two per situation, written by one person. This asks a
model other than the system's (JUDGE_MODEL, default claude-opus-5-5; the investigator runs LLM_MODEL)
for more, written from the story of the situation's cases (data/shared_customers.yaml,
eval/extra_customers.yaml), not from the seeded reviews, so they cannot copy a precedent's wording.
Each generated query keeps its situation's labels. Writes generated.yaml, which is kept in git and
reviewed like situations.yaml: a run must not change its queries from one time to the next.

    set -a; source .env; set +a
    eval/retrieval/.venv/bin/python eval/retrieval/generate.py            # writes eval/retrieval/generated.yaml

About 12 calls, one per situation. Spends money; the key is LLM_API_KEY (or ANTHROPIC_API_KEY).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import anthropic
import yaml
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CUSTOMER_FILES = (ROOT / "data" / "shared_customers.yaml", ROOT / "eval" / "extra_customers.yaml")
MODEL = os.environ.get("JUDGE_MODEL", "claude-opus-5-5")
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
PER_SITUATION = 4

# The cause labels the investigator chooses from: a query that names one gives the answer away.
LABELS = ("payroll_or_tax", "funds_moved_out_of_view", "unexplained_outflow", "revenue_loss", "seasonal_dip",
          "insufficient_data", "planned_vendor_payment", "no_clear_cause")

INSTRUCTIONS = f"""You write test queries for a search over closed credit reviews of business customers.

In the system under test, an investigator looks into why a customer's cash fell this week, then
searches past reviews by describing, in one or two sentences, what it has found so far. Write
{PER_SITUATION} such queries for the case below.

Rules:
- State findings about this week's data only: cash, payments, income, the card and its repayments.
- Plain words, as an analyst writes them. Vary the wording and the order between queries: one short,
  one detailed, one that leads with the card side where there is one.
- Do not name a cause category, a recommendation, or a decision (no "keep the limit", "pause",
  "request documents"), and do not say what the outcome should be.
- Do not mention earlier reviews of the customer, other customers, or ids such as c_001.
- Keep any figures the case gives; invent none."""


class Queries(BaseModel):
    queries: list[str] = Field(description=f"exactly {PER_SITUATION} queries, one or two sentences each")


def stories() -> dict[str, str]:
    out = {}
    for path in CUSTOMER_FILES:
        customers = yaml.safe_load(path.read_text())["customers"]
        items = customers.items() if isinstance(customers, dict) else ((c.get("customer_id") or c.get("id"), c)
                                                                        for c in customers)
        for customer_id, customer in items:
            out[customer_id] = " ".join(customer["story"].split())
    return out


def facts(situation: dict, story: dict[str, str]) -> str:
    """The stories of the situation's cases, with the story of any case one of them is told against."""
    ids = list(situation["cases"])
    for case in situation["cases"]:
        ids += [other for other in re.findall(r"c_\d{3}", story[case]) if other not in ids]
    return "\n".join(f"- {story[i]}" for i in ids)


def problems(query: str) -> list[str]:
    found = [label for label in LABELS if label in query or label.replace("_", " ") in query.lower()]
    found += re.findall(r"\bc_\d{3}\b", query)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", action="append", default=[], help="generate for this situation id only")
    parser.add_argument("--out", type=Path, default=HERE / "generated.yaml")
    args = parser.parse_args()
    key = os.environ.get("LLM_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        sys.exit("set LLM_API_KEY or ANTHROPIC_API_KEY (set -a; source .env; set +a)")
    client = anthropic.Anthropic(api_key=key)
    situations = yaml.safe_load((HERE / "situations.yaml").read_text())["situations"]
    story = stories()

    generated, tokens_in, tokens_out = [], 0, 0
    for situation in situations:
        if args.only and situation["id"] not in args.only:
            continue
        response = client.messages.parse(
            model=MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": f"{INSTRUCTIONS}\n\nThe case:\n{facts(situation, story)}"}],
            output_format=Queries,
        )
        tokens_in += response.usage.input_tokens
        tokens_out += response.usage.output_tokens
        for query in response.parsed_output.queries:
            query = " ".join(query.split())
            if bad := problems(query):
                print(f"dropped ({', '.join(bad)}): {query}", file=sys.stderr)
                continue
            generated.append({"situation": situation["id"], "query": query})

    price_in, price_out = PRICES.get(MODEL, (0.0, 0.0))
    cost = (tokens_in * price_in + tokens_out * price_out) / 1e6
    header = (f"# Generated by generate.py with {MODEL}: {len(generated)} queries. Review before use; keep in git.\n"
              f"# Each keeps the labels of its situation in situations.yaml.\n")
    args.out.write_text(header + yaml.safe_dump({"model": MODEL, "queries": generated}, sort_keys=False, width=110))
    print(f"{len(generated)} queries to {args.out}; {tokens_in} tokens in, {tokens_out} out, about ${cost:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
