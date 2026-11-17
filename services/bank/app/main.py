import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.api.tools import router as tools_router
from app.db import Base, SessionLocal, engine
from app.db.seed import seed_if_empty
from contracts import Health


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Analysis tools refuse tokens nobody can check (API doc section 5).
    if not os.environ.get("JWT_SECRET"):
        raise RuntimeError("JWT_SECRET is not set")
    # Create any missing tables; seed only when the database is empty.
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        seed_if_empty(session)
    yield


app = FastAPI(title="Bank Service", lifespan=lifespan)
app.include_router(tools_router)


@app.get("/health", response_model=Health)
def health() -> Health:
    # Fails with a 500 if the database is unreachable, so a broken service is visible.
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return Health(service="bank", status="ok")
