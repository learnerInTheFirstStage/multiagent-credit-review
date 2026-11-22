"""The log line every service writes: one JSON object per line on stdout, which Docker collects.

Langfuse keeps what the models were given and what they said. These logs keep what happened: each request,
each case run, each failure. So a line carries ids, status codes and counts, never customer names, account
numbers, tokens, or text that a model or a customer wrote.

Each service calls setup_logging once when its app module loads and adds RequestLog to its app.
"""

import json
import logging
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime

from pydantic import ValidationError

# The ids that every line logged in the current request or case run carries: request_id, case_id.
_bound: ContextVar[dict] = ContextVar("log_fields", default={})

# What a LogRecord holds of its own. Anything else on a record came from extra=, but for uvicorn's copy of its
# message with terminal colours.
_RECORD_FIELDS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "color_message"}

_requests = logging.getLogger("request")


@contextmanager
def bind(**fields):
    """Put these fields on every line logged inside the block, in this thread or task only."""
    token = _bound.set({**_bound.get(), **fields})
    try:
        yield
    finally:
        _bound.reset(token)


def trace_headers() -> dict[str, str]:
    """The header that passes the current request id on to another service, whose RequestLog then logs it."""
    request_id = _bound.get().get("request_id")
    return {"X-Request-ID": request_id} if request_id else {}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        line = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "message": record.getMessage(),
            **_bound.get(),
            **{key: value for key, value in vars(record).items() if key not in _RECORD_FIELDS},
        }
        if record.exc_info:
            line["error"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str)

    def formatException(self, exc_info) -> str:
        """The traceback, but a ValidationError anywhere in its chain says which fields failed and how, not the
        input it quotes, which can be a model's answer or a customer's text. Other errors keep their message."""
        text = super().formatException(exc_info)
        error, seen = exc_info[1], set()
        while error is not None and id(error) not in seen:
            seen.add(id(error))
            if isinstance(error, ValidationError):
                fields = "; ".join(f"{'.'.join(map(str, part['loc']))}: {part['type']}" for part in error.errors())
                text = text.replace(str(error), f"validation errors for {error.title}: {fields}")
            error = error.__cause__ or error.__context__
        return text


def setup_logging(service: str, level: int = logging.INFO) -> None:
    """Send every logger's lines, uvicorn's included, to stdout as JSON."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # uvicorn sets up its own text handlers before it loads the app. Its lines go through ours instead.
    uvicorn = logging.getLogger("uvicorn")
    uvicorn.handlers.clear()
    uvicorn.propagate = True
    # RequestLog writes the line per request, with its id and duration, in place of uvicorn's.
    logging.getLogger("uvicorn.access").disabled = True
    # httpx logs every URL it calls, and an agent's tool call puts text the model chose in the query string.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


class RequestLog:
    """ASGI middleware: one line per request with its method, path, status and duration.

    The request id is the gateway's X-Request-ID, or a new one for a call that did not come through the gateway.
    Every line logged while the request runs carries it, those of its background tasks too. The line is written
    once the response is sent, so a background task that runs on afterwards does not count in its duration.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        # The id goes on to Bank and Card as a header, and httpx refuses a value that is not ASCII. The gateway
        # sends 32 hex characters; a caller that bypassed it may send anything.
        if not (request_id.isascii() and request_id.isprintable() and 0 < len(request_id) <= 128):
            request_id = uuid.uuid4().hex
        started = time.perf_counter()
        status, logged = 500, False

        async def send_and_log(message):
            nonlocal status, logged
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body") and not logged:
                logged = True
                _log_request(scope, status, started)

        token = _bound.set({**_bound.get(), "request_id": request_id})
        try:
            await self.app(scope, receive, send_and_log)
        except Exception:
            if not logged:
                _log_request(scope, 500, started)
            # Not reset: the server logs the error next, in this same context, so its line carries the request id.
            raise
        _bound.reset(token)


def _log_request(scope, status: int, started: float) -> None:
    if scope["path"] == "/health" and status < 400:
        return  # Docker's health check, every few seconds
    fields = {
        "method": scope["method"],
        "path": scope["path"],
        "status": status,
        "duration_ms": round((time.perf_counter() - started) * 1000),
    }
    # Set by the router once it has matched a route such as /cases/{case_id}.
    if case_id := scope.get("path_params", {}).get("case_id"):
        fields["case_id"] = case_id
    level = logging.ERROR if status >= 500 else logging.INFO
    _requests.log(level, "%s %s %d", scope["method"], scope["path"], status, extra=fields)
