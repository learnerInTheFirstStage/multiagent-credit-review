import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.api import signals
from app.db import Base, engine
from app.db import models  # noqa: F401  registers the tables on Base
from contracts import Health

# uvicorn only sets up its own loggers. Without this the app's INFO lines are dropped.
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # No migrations in the POC: all data is generated, so a schema change means drop and reseed.
    Base.metadata.create_all(engine)
    yield


app = FastAPI(title="Orchestrator Service", lifespan=lifespan)
app.include_router(signals.router)


@app.get("/health", response_model=Health)
def health() -> Health:
    # Fails with a 500 if the database is unreachable, so a broken service is visible.
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return Health(service="orchestrator", status="ok")
