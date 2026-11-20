"""Case worker: consume case_ids from RabbitMQ and run them through the graph.

    python -m app.mq.worker

Same image as the orchestrator, different entrypoint. Scale with
`docker compose up --scale worker=N`. Each worker holds its own graph
instance (Postgres checkpointer + LangFuse callbacks) so model calls go
through the same tracing path as the API's BackgroundTasks fallback.

Message format: `{"case_id": "..."}` as JSON bytes.

- Success → basic_ack.
- CaseWaiting (raised inside runner.run_case): the DB row is already set to
  waiting_for_data, so we ack and let the orchestrator's retry loop pick it
  up. Note: that retry loop lives on the orchestrator API process; in a
  worker-only deployment nothing retries waiting_for_data cases. This is a
  known POC limitation (see dispatch.py docstring).
- Any other exception: basic_nack without requeue. There is no DLQ in this
  POC, so the message is dropped and must be reconciled manually from the
  case's status in the DB. A production DLQ would be configured at queue
  declare with `x-dead-letter-exchange`.
"""

from __future__ import annotations

import json
import logging
import os
import signal as signal_module
import sys
import threading
import time
from contextlib import contextmanager, nullcontext

import pika
from pika.exceptions import AMQPError

from contracts.logs import setup_logging

from app import runner, runtime
from app.mq.publisher import CASES_QUEUE

# Same JSON log format as the orchestrator's main process, so a worker's lines
# show up alongside the API's under the same request_id when a case was first
# accepted there.
setup_logging("orchestrator-worker")
log = logging.getLogger(__name__)

# Only one case in flight per worker. Scale with multiple worker containers rather
# than threading inside one: LangGraph + Postgres checkpointer are not designed for
# concurrent access from one graph instance.
PREFETCH = 1


def _mock_enabled() -> bool:
    """When set, the worker sleeps instead of invoking the graph.

    Used for the MQ pipe-validation smoke test: proves publish/consume/ack/
    durability work without spending LLM credits. The graph itself is not
    built either, so LLM_MODEL need not be set.
    """
    return os.environ.get("MOCK_PROCESSING", "").lower() in ("1", "true", "yes")


def _mock_seconds() -> float:
    """How long a mocked case pretends to take. Default 150 s to shake out
    the pika BlockingConnection heartbeat behaviour that a short mock misses.
    Tests and smoke runs override to a few seconds via the compose override."""
    try:
        return float(os.environ.get("MOCK_SECONDS", "150"))
    except ValueError:
        return 150.0


def _on_message(graph, channel, method, _properties, body) -> None:
    """Callback for each message. Runs the case on this thread."""
    try:
        payload = json.loads(body.decode())
        case_id = payload["case_id"]
    except (ValueError, KeyError):
        log.exception("worker: unreadable message body, discarding: %r", body[:200])
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    log.info("worker: picked up case %s", case_id)
    try:
        if _mock_enabled():
            seconds = _mock_seconds()
            log.info("worker: MOCK_PROCESSING on, sleeping %.1fs for case %s", seconds, case_id)
            time.sleep(seconds)
        else:
            runner.run_case(graph, case_id)
    except Exception:  # noqa: BLE001
        # runner.run_case already handles CaseWaiting internally (sets the case's
        # waiting_for_data status). Any exception that escapes here is unexpected
        # — log and don't requeue, so a bad case doesn't loop forever.
        log.exception("worker: case %s raised; message will not be requeued", case_id)
        channel.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
        return

    channel.basic_ack(delivery_tag=method.delivery_tag)
    log.info("worker: case %s done, acked", case_id)


@contextmanager
def _graph_context():
    """Build the graph for real processing, or yield None in mock mode."""
    if _mock_enabled():
        yield None
        return
    with runtime.case_graph() as graph:
        yield graph


def main() -> int:
    url = os.environ.get("RABBITMQ_URL")
    if not url:
        log.error("worker: RABBITMQ_URL is not set, exiting")
        return 1

    with _graph_context() as graph:
        if not _mock_enabled() and graph is None:
            log.error("worker: no model is configured (LLM_MODEL unset), exiting")
            return 1

        if _mock_enabled():
            log.warning("worker: MOCK_PROCESSING on — the graph is not built and no LLM is called")

        stopping = threading.Event()

        # heartbeat=1800 (30 minutes) sets a window longer than any real case
        # should take, so the BlockingConnection callback has time to finish
        # before the broker drops the link. We can't set 0 (disable) because
        # AMQP negotiation lets the broker override 0 back to its default
        # (RabbitMQ's is 60 s), which would re-introduce the drop-and-redeliver
        # bug. Explicit large value wins the negotiation.
        #
        # Trade-off: a truly dead broker is only noticed on the next consume
        # or publish, up to 30 min later. Production should run per-case work
        # in a thread and ack via connection.add_callback_threadsafe so the
        # main thread keeps processing heartbeats. Out of POC scope.
        url_params = pika.URLParameters(url)
        url_params.heartbeat = 1800
        url_params.blocked_connection_timeout = 30
        connection = pika.BlockingConnection(url_params)
        channel = connection.channel()
        channel.queue_declare(queue=CASES_QUEUE, durable=True)
        channel.basic_qos(prefetch_count=PREFETCH)
        log.info("worker: connected, consuming from %s", CASES_QUEUE)

        # Graceful shutdown on SIGTERM / SIGINT. SIGTERM may land while
        # start_consuming is idle on an empty queue — if the handler only
        # set a flag, the worker would wait until the next message arrived
        # to notice, which with compose's 10-minute stop_grace_period means
        # `docker compose down` hangs 10 minutes on an idle worker. So the
        # handler schedules stop_consuming on the connection's own thread
        # via add_callback_threadsafe, which the IOLoop picks up on its
        # next tick regardless of whether a message is in flight.
        def _stop(_signum, _frame):
            log.info("worker: stop requested")
            stopping.set()
            try:
                connection.add_callback_threadsafe(channel.stop_consuming)
            except Exception:  # noqa: BLE001 — a half-dead connection on shutdown is fine to ignore
                log.debug("worker: could not schedule stop_consuming", exc_info=True)

        signal_module.signal(signal_module.SIGTERM, _stop)
        signal_module.signal(signal_module.SIGINT, _stop)

        def _wrapped(ch, method, properties, body):
            if stopping.is_set():
                # Requeue so another worker picks this up: this one is shutting down.
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=True)
                ch.stop_consuming()
                return
            _on_message(graph, ch, method, properties, body)
            if stopping.is_set():
                ch.stop_consuming()

        channel.basic_consume(queue=CASES_QUEUE, on_message_callback=_wrapped)

        try:
            channel.start_consuming()
        except AMQPError:
            log.exception("worker: AMQP error, exiting")
            return 1
        finally:
            try:
                connection.close()
            except Exception:  # noqa: BLE001
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
