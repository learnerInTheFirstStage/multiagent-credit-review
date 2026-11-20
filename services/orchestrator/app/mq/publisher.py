"""Publish case_ids to RabbitMQ so a worker picks them up.

Bonus feature. Signals.py falls back to FastAPI BackgroundTasks when
RABBITMQ_URL is unset or a publish raises, so the rest of the stack still
works. See KAN-74 (and the superseded PR #29) for the full design rationale.

Single process-wide BlockingConnection with a lock around channel use.
pika's BlockingConnection and Channel are not thread-safe, but FastAPI runs
sync endpoints in a threadpool, so the lock is required.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Final

import pika
from pika.exceptions import AMQPError

log = logging.getLogger(__name__)

CASES_QUEUE: Final[str] = "cases"

_lock = threading.Lock()
_connection: pika.BlockingConnection | None = None
_channel = None


def _url() -> str | None:
    return os.environ.get("RABBITMQ_URL") or None


def enabled() -> bool:
    """True if signals.py should publish rather than use BackgroundTasks."""
    return _url() is not None


def _connect() -> None:
    """Open the connection and declare the queue. Caller holds _lock.

    Closes any existing (stale) connection before opening a new one, so a
    reconnect doesn't leak the old socket. Short timeouts so a blackholed
    broker doesn't hold the lock for a request thread forever.
    """
    global _connection, _channel
    url = _url()
    assert url is not None, "enabled() should guard this"
    if _connection is not None:
        try:
            if not _connection.is_closed:
                _connection.close()
        except Exception:  # noqa: BLE001 — closing a half-dead connection can raise; we're replacing it
            log.debug("rabbitmq: ignoring error closing stale connection", exc_info=True)
    params = pika.URLParameters(url)
    params.socket_timeout = 5
    params.blocked_connection_timeout = 10
    _connection = pika.BlockingConnection(params)
    _channel = _connection.channel()
    # durable=True so the queue survives a broker restart; messages still
    # need delivery_mode=2 at publish time to be persisted to disk.
    _channel.queue_declare(queue=CASES_QUEUE, durable=True)
    log.info("rabbitmq: publisher connected, queue=%s declared", CASES_QUEUE)


def enqueue(case_id: str) -> None:
    """Publish one case_id. Raises AMQPError if the broker is unreachable.

    The caller (signals.py) treats a raise as "fall back to BackgroundTasks".
    """
    with _lock:
        global _connection, _channel
        # Lazy connect on first publish, and reconnect if the previous
        # channel/connection died (broker restart, network blip).
        if _channel is None or _connection is None or _connection.is_closed:
            _connect()
        try:
            _channel.basic_publish(
                exchange="",
                routing_key=CASES_QUEUE,
                body=json.dumps({"case_id": case_id}).encode(),
                properties=pika.BasicProperties(
                    delivery_mode=2,     # persistent: on disk until acked
                    content_type="application/json",
                ),
            )
        except AMQPError:
            # One retry on a stale channel: pika doesn't always surface disconnection until publish time.
            log.warning("rabbitmq: publish failed; reconnecting and retrying once")
            _connect()
            _channel.basic_publish(
                exchange="",
                routing_key=CASES_QUEUE,
                body=json.dumps({"case_id": case_id}).encode(),
                properties=pika.BasicProperties(delivery_mode=2, content_type="application/json"),
            )


def close() -> None:
    """Shut down cleanly. Called from app lifespan on shutdown."""
    global _connection, _channel
    with _lock:
        if _connection is not None and not _connection.is_closed:
            _connection.close()
        _connection = None
        _channel = None
