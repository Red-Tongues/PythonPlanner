"""Engine and session helpers.

SQLite connections get (A5): ``foreign_keys=ON`` (enforce FKs), ``journal_mode=WAL`` (readers
never block the kernel's writer — the GUI/CLI can watch a running loop) and ``busy_timeout=5000``
(wait up to 5 s for a lock instead of failing immediately), and ``recursive_triggers=ON`` so a
conflict-replacing ``INSERT OR REPLACE`` fires DELETE triggers (R1 fix 3).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.db.guards import GuardedSession

SQLITE_PRAGMAS: tuple[str, ...] = (
    "foreign_keys = ON",
    "journal_mode = WAL",
    "busy_timeout = 5000",
    # R1 fix 3: without this, INSERT OR REPLACE deletes the conflicting row WITHOUT firing DELETE
    # triggers, which would let it replace a locked packet or an append-only row.
    "recursive_triggers = ON",
)


def make_engine(database_url: str, **kwargs: object) -> Engine:
    """Create an engine. SQLite gets foreign keys switched on and its parent folder created."""
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, **kwargs)  # type: ignore[arg-type]

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            for pragma in SQLITE_PRAGMAS:
                cursor.execute(f"PRAGMA {pragma}")
            cursor.close()

        return engine
    return create_engine(url, **kwargs)  # type: ignore[arg-type]


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, class_=GuardedSession, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transaction boundary: commit on success, roll back on any error."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
