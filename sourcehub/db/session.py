from __future__ import annotations

import contextlib
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine():
    global _engine, _SessionFactory
    if _engine is not None:
        return _engine

    url = get_settings().db_url
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        # check_same_thread off so the scheduler thread can share the engine
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}

    _engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            # WAL lets the crawler write while the web UI reads.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

    _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session() -> Session:
    get_engine()
    assert _SessionFactory is not None
    return _SessionFactory()


@contextlib.contextmanager
def session_scope() -> Iterator[Session]:
    s = get_session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db() -> None:
    """Create tables, FTS index, and seed reference data. Idempotent."""
    from . import models  # noqa: F401  (registers mappers)
    from .search import ensure_fts
    from .seed import seed_reference_data

    engine = get_engine()
    Base.metadata.create_all(engine)
    ensure_fts(engine)
    with session_scope() as s:
        seed_reference_data(s)
