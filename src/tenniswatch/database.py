"""Database engine configuration."""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def make_session_factory(database_url: str | None = None):
    url = database_url or os.environ["DATABASE_URL"]
    engine = create_engine(url, pool_pre_ping=True)
    return sessionmaker(engine, expire_on_commit=False)

from contextlib import contextmanager
from sqlalchemy import text


@contextmanager
def advisory_job_lock(session, lock_key: int):
    """Serialize a PostgreSQL job across runners; caller must keep this connection open."""
    # Pin one physical connection for the entire session-level lock lifetime.
    # Session.commit() would return its connection to the pool and may later
    # unlock a different connection, leaving an orphaned lock behind.
    with session.get_bind().connect() as connection:
        acquired = bool(connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}).scalar())
        connection.commit()
        try:
            yield acquired
        finally:
            if acquired:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                connection.commit()
