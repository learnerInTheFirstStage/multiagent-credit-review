import asyncio
import logging
from http import HTTPStatus
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from sqlalchemy import text

from app import history, runner, runtime
from app.api import cases, dev_login, signals
from app.db import Base, SessionLocal, engine
from app.db import models  # noqa: F401  registers the tables on Base
from contracts import Health

# uvicorn only sets up its own loggers. Without this the app's INFO lines are dropped.
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # No migrations in the POC: all data is generated, so a schema change means drop and reseed.
    Base.metadata.create_all(engine)
    # The closed reviews customers' histories start from. Needs no model, and inserts nothing it already has.
    try:
        with SessionLocal() as session:
            counts = history.seed(session, history.read_seeds(runtime.history_seed_files()), expect_clean=False)
            session.commit()
        log.info("Seeded reviews: %d new, %d already there", counts["seeded"], counts["already_there"])
    except Exception:  # a broken seed file must not keep the service from starting
        log.exception("The seeded reviews could not be loaded. The service starts without them.")
    with runtime.case_graph() as graph:
        app.state.graph = graph
        picked_up, retrying, stop = [], None, asyncio.Event()
        if graph is not None:
            # A closed case must not read as "no earlier review" for want of its review.
            runner.keep_missing_reviews(graph)
            # A restart must not leave a case queued, half investigated, or with a stale status.
            loop = asyncio.get_running_loop()
            picked_up = [loop.run_in_executor(None, runner.run_case, graph, case_id) for case_id in runner.unfinished()]
            # Cases waiting for Bank or Card run again when their retry is due, first those that came due while
            # the service was down.
            first = loop.run_in_executor(None, _retry_due, graph, stop)
            retrying = asyncio.create_task(_retry_waiting(graph, stop, first, runtime.retry_check_seconds()))
        yield
        # Before the checkpointer closes. If the process is killed first, the next start picks them up again.
        stop.set()
        await asyncio.gather(*picked_up, *([retrying] if retrying else []), return_exceptions=True)


def _retry_due(graph, stop: asyncio.Event) -> None:
    try:
        runner.retry_due(graph, stopping=stop.is_set)
    except Exception:  # a database that is down for a moment must not end the loop
        log.exception("Cases waiting for Bank or Card could not be looked up. Trying again later.")


async def _retry_waiting(graph, stop: asyncio.Event, first, every: float) -> None:
    """Run runner.retry_due every `every` seconds until the service stops, one run at a time."""
    loop = asyncio.get_running_loop()
    await first
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), every)
        except TimeoutError:
            await loop.run_in_executor(None, _retry_due, graph, stop)


app = FastAPI(title="Orchestrator Service", lifespan=lifespan)
app.include_router(signals.router)
app.include_router(cases.router)
app.include_router(dev_login.router)

# Every error has one shape: {"error": ..., "message": ...}. API doc section 8.
ERROR_NAMES = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "invalid_request",
    502: "card_unavailable",
    503: "unavailable",
}


def _error(code: int, message: str, headers: dict | None = None) -> JSONResponse:
    name = ERROR_NAMES.get(code) or HTTPStatus(code).phrase.lower().replace(" ", "_")
    return JSONResponse({"error": name, "message": message}, status_code=code, headers=headers)


@app.exception_handler(HTTPException)
async def http_error(request: Request, error: HTTPException) -> JSONResponse:
    return _error(error.status_code, str(error.detail), error.headers)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
    # Where each problem is and what it is, as one line of text.
    problems = [f"{'.'.join(str(part) for part in problem['loc'])}: {problem['msg']}" for problem in error.errors()]
    return _error(422, "; ".join(problems))


@app.exception_handler(Exception)
async def internal_error(request: Request, error: Exception) -> JSONResponse:
    # The details go to the server's log, never to the caller.
    return _error(500, "The service failed to handle the request")


@app.get("/health", response_model=Health)
def health() -> Health:
    # Fails with a 500 if the database is unreachable, so a broken service is visible.
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return Health(service="orchestrator", status="ok")
