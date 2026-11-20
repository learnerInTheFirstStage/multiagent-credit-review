import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# hide_parameters: a failed write's error, and so its log line, would quote the report or the note it carried.
engine = create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True, hide_parameters=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
