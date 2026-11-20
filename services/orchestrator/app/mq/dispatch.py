"""One place that decides where a case runs: in-process or on a worker.

Today only app.api.signals.receive_signal dispatches through here. The other
three handoff sites — app.main (startup pick-up), app.api.cases (analyst
retry after a Card outage), app.runner (retry_due for cases waiting on Bank
or Card) — still run in-process, because they already have the API's graph
and run inline. In MQ mode they're skipped on purpose (see main.py's startup
`if mq_publisher.enabled(): skip`). A worker-only deployment where the API
has no graph would not retry `waiting_for_data` cases automatically: that
belongs to a transactional outbox / periodic sweeper, out of POC scope.

In MQ mode (RABBITMQ_URL set + a successful publish): the case lands in the
'cases' queue and a worker picks it up. In fallback (no broker or publish
raised): the case runs in-process — via FastAPI BackgroundTasks when a
request can hand off, or inline when there is no request (startup, retry
loop). A failed publish degrades to the fallback for that one case; it does
not stall the service.

Not addressed in this POC: 'claim then publish' atomicity. If an orchestrator
restarts after it stored a signal but before it published, the case stays in
QUEUED until someone republishes it. For prod the fix is a transactional
outbox; for the POC, main.py's startup pick-up is skipped in MQ mode on
purpose — cases a worker already took are already in the queue (durable),
and republishing them would double-run.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import BackgroundTasks

from app import runner
from app.mq import publisher

log = logging.getLogger(__name__)


def dispatch(case_id: str, graph, background: Optional[BackgroundTasks] = None) -> None:
    """Hand off a case for processing. Called by every dispatch site.

    - MQ mode (publisher.enabled() and publish succeeds): publish to queue.
    - Fallback with a request context (`background` given): BackgroundTasks.
    - Fallback without a request context (startup, retry loop): run inline.

    The in-process fallback needs a graph. In MQ mode the API process can run
    without one (the worker owns its own), but a publish failure then has
    nothing to fall back to — the case stays QUEUED for a human / the next
    republish. We log loudly so an operator notices.
    """
    if publisher.enabled():
        try:
            publisher.enqueue(case_id)
            log.info("case %s dispatched via rabbitmq", case_id)
            return
        except Exception:  # noqa: BLE001 — a broker hiccup must never stall a case
            log.exception("rabbitmq publish failed for case %s; falling back in-process", case_id)

    if graph is None:
        # Fallback path needs a graph. In MQ mode with no model configured on
        # the API process, a failed publish leaves the case QUEUED until a
        # worker (or a human) republishes it. See transactional-outbox note.
        log.error("case %s stays queued: publish failed and no in-process graph to fall back to", case_id)
        return

    if background is not None:
        background.add_task(runner.run_case, graph, case_id)
    else:
        runner.run_case(graph, case_id)
