import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app import runner, runtime
from app.api import cases, signals
from app.db import Base, engine
from app.db import models  # noqa: F401  registers the tables on Base
from contracts import Health

# uvicorn only sets up its own loggers. Without this the app's INFO lines are dropped.
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # No migrations in the POC: all data is generated, so a schema change means drop and reseed.
    Base.metadata.create_all(engine)
    with runtime.case_graph() as graph:
        app.state.graph = graph
        picked_up = []
        if graph is not None:
            # A restart must not leave a case queued, half investigated, or with a stale status.
            loop = asyncio.get_running_loop()
            picked_up = [loop.run_in_executor(None, runner.run_case, graph, case_id) for case_id in runner.unfinished()]
        yield
        # Before the checkpointer closes. If the process is killed first, the next start picks them up again.
        await asyncio.gather(*picked_up, return_exceptions=True)


app = FastAPI(title="Orchestrator Service", lifespan=lifespan)
app.include_router(signals.router)
app.include_router(cases.router)


@app.get("/health", response_model=Health)
def health() -> Health:
    # Fails with a 500 if the database is unreachable, so a broken service is visible.
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return Health(service="orchestrator", status="ok")
