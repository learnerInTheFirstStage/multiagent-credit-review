# scripts

- `smoke.sh`: the skeleton's properties (routing, database isolation) on a running stack.
- `e2e.py`: one case through the whole running system with the stack's real model.

## e2e.py

One customer's signal goes to the Orchestrator. The agent asks the real Cash Flow and Card specialists, which call Bank and Card. The checked report is printed with its question path, the analyst decides, a manager approves when the case needs it, Card carries out the action, and Card's snapshot is checked against the report. It spends money on the configured model. Run it on a stack of its own, from the checkout root. From the main checkout `--env-file` can be left out; a worktree has no `.env`, so point it at the main checkout's:

```bash
docker compose -p e2e --env-file <main checkout>/.env -f docker-compose.yml -f eval/docker-compose.eval.yml up -d --build
python scripts/e2e.py                  # c_010, the analyst approves
docker compose -p e2e down -v
```

| Option | Default | |
|---|---|---|
| `--customer` | `c_010` | any customer in `data/shared_customers.yaml` or `eval/extra_customers.yaml` |
| `--project` | `e2e` | the compose project, for the two container commands below |
| `--orchestrator-url`, `--card-url` | `http://127.0.0.1:8004`, `:8003` | |
| `--container`, `--card-container` | `docker compose -p <project> exec -T orchestrator` / `card` | where tokens are made, with the stack's own `JWT_SECRET`. `--container ''` makes them here with `JWT_SECRET` (default the compose fallback) |
| `--decide` | `approve` | what the analyst decides; `reject` closes the case and nothing reaches Card |
| `--timeout` | `600` | seconds to wait for the investigation |

Expected for c_010: `pause_card` citing G-08b, so `u_analyst` approves, then `u_manager`, and Card's `card_status` goes from `active` to `paused`. A `reduce_limit` report must leave Card's limit at the report's `engine_limit_cents`; any other recommendation, or a rejection, must leave Card unchanged.

Exit status: 0 every check passed, 1 a check failed or a request went wrong, 2 the case did not finish in time. Without `LLM_MODEL` the case waits as queued and the run ends with 2. The stack uses the main stack's host ports, so it cannot run beside it. Run `down -v` between runs, since a customer has one open case at a time and Card keeps the action; the script stops before the signal when it finds an open case or a card that is not in its seeded state. It also prints `eval/grade.py`'s grades of the report when the golden set holds the customer, for reading only.
