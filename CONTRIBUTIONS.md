# Contributions

This repository is a mirror of a 3-person team project. All three members collaborated on design, reviews, and integration; each owned one vertical of the system. The split below reflects who built the bulk of the code in each area. Full per-commit attribution is preserved in `git log`.

## Team

- **Vincent Li** ([@lqc412](https://github.com/lqc412)) — Orchestrator Service: main LangGraph case graph, report writer, check/repair loop, Auth0 integration, Kubernetes + AWS deployment, past-case retrieval (RAG), monthly PDF report.
- **James Z. Zhang** — Card Service: database + seeder, 6 read-only Card tools, Credit Behavior specialist agent, approved-action endpoint, Card agent's DeepEval judge test set.
- **Shaohua Guo** ([@learnerInTheFirstStage](https://github.com/learnerInTheFirstStage)) — Bank Service + Cash Flow specialist + review frontend + async signal bonus: Bank database and seeder (shared YAML), 7 read-only Bank tools, Cash Flow LangGraph specialist, review app (login, queue, case page, decide, SSE live updates), RabbitMQ async signal processing (opt-in).

## Shared work

- `packages/contracts` — shared Pydantic schemas (all three)
- `data/shared_customers.yaml` — 12 demo customers, scenario design agreed across the team
- `eval/` — golden set, grading, and evaluation infrastructure (primarily Vincent, with per-service test sets contributed by their owners)
- `docker-compose.yml` and `gateway/nginx.conf` — Vincent + Shaohua
- Guardrails and the evidence-id convention — designed jointly across PR reviews

## Reviewing

Pull requests across all three areas went through automated review (`@claude review` GitHub Action) plus human peer review from the other two members. The `@claude review` Action lives in `.github/workflows/claude.yml`.

## Attribution verification

Original repository: https://github.com/lqc412/multiagent-credit-review

To verify individual commit attribution:

```bash
# commits per author on main
git log main --format='%an' | sort | uniq -c | sort -rn

# full commits by a specific author
git log main --author='Shaohua Guo\|learnerInTheFirstStage' --oneline
git log main --author='James Z. Zhang' --oneline
git log main --author='Vincent Li\|lqc412' --oneline

# PR merge commits in chronological order
git log main --grep='Merge pull request' --format='%h %an %s'
```
