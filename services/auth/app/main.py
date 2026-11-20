import os

from fastapi import FastAPI
from sqlalchemy import create_engine, text

from contracts import Health
from contracts.logs import RequestLog, setup_logging

setup_logging("auth")
engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)
app = FastAPI(title="Auth Service")
app.add_middleware(RequestLog)


@app.get("/health", response_model=Health)
def health() -> Health:
    # Fails with a 500 if the database is unreachable, so a broken service is visible.
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return Health(service="auth", status="ok")
