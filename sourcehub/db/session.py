from __future__ import annotations

import contextlib
import datetime as dt
import json
import logging
from collections.abc import Iterator

from sqlalchemy import JSON, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from ..config import get_settings

log = logging.getLogger(__name__)


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


def _column_default_sql(col) -> str:
    """A SQL-literal DEFAULT clause for one column, if it needs one.

    Only NOT NULL columns need this -- SQLite refuses to ADD COLUMN a NOT NULL
    field to a table that already has rows without one. The model's Python-side
    ``default`` (used by the ORM on INSERT) isn't itself SQL, so it has to be
    rendered by hand; JSON columns store as TEXT, so a list/dict default is
    JSON-encoded first.
    """
    if col.nullable:
        return ""
    default = col.default
    value = None
    if default is not None:
        if getattr(default, "is_scalar", False):
            value = default.arg
        elif getattr(default, "is_callable", False):
            fn = default.arg
            # SQLAlchemy calls a callable default with an ExecutionContext when
            # the callable declares a parameter (none of ours do) and with no
            # arguments otherwise -- ``utcnow`` takes zero, ``list``/``dict``
            # tolerate either, so try the no-arg form first rather than assuming.
            try:
                value = fn()
            except TypeError:
                try:
                    value = fn(None)
                except Exception:
                    value = None
    if isinstance(col.type, JSON):
        return f" DEFAULT '{json.dumps(value if value is not None else [])}'"
    if isinstance(value, dt.datetime):
        # SQLite refuses CURRENT_TIMESTAMP (and friends) here specifically: ADD
        # COLUMN requires a *constant* default to backfill existing rows with, and
        # those keywords count as non-constant even though they'd only ever be
        # evaluated once. A literal in the exact string form SQLAlchemy's own
        # DateTime type reads back (naive "YYYY-MM-DD HH:MM:SS.ffffff", matching
        # what's already on disk in every existing timestamp column) is constant.
        return " DEFAULT '" + value.strftime("%Y-%m-%d %H:%M:%S.%f") + "'"
    if isinstance(value, bool):
        return f" DEFAULT {1 if value else 0}"
    if isinstance(value, (int, float)):
        return f" DEFAULT {value}"
    if isinstance(value, str):
        return " DEFAULT '" + value.replace("'", "''") + "'"
    # No usable Python-side default on a NOT NULL column that isn't one of the
    # above -- every column this project actually defines that way has a real
    # default, so this is a last-resort fallback, not an expected path.
    return " DEFAULT 0"


def _ensure_columns(engine) -> None:
    """Add columns the models declare that an existing database predates.

    ``Base.metadata.create_all()`` only creates tables that don't exist yet -- a
    table that is already there but is missing a column a newer version of the
    code added is untouched, and the next query to reference that column fails
    with "no such column" instead of migrating. There is no separate migration
    step in this project's workflow (a Docker/Unraid user just pulls a new image
    and restarts), so upgrading in place has to happen here or not at all.

    SQLite-only: the rest of this module already assumes SQLite throughout
    (WAL pragmas, etc.), and ``PRAGMA table_info`` / bare ``ALTER TABLE ADD
    COLUMN`` are SQLite-specific syntax.
    """
    if not str(engine.url).startswith("sqlite"):
        return
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {
                row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table.name}")')
            }
            if not existing:
                continue  # the table itself is new; create_all() already made it
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl_type = col.type.compile(dialect=engine.dialect)
                default_sql = _column_default_sql(col)
                log.info("migrating: adding %s.%s (%s)", table.name, col.name, ddl_type)
                conn.exec_driver_sql(
                    f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}{default_sql}'
                )
        conn.commit()


def init_db() -> None:
    """Create tables, FTS index, and seed reference data. Idempotent."""
    from . import models  # noqa: F401  (registers mappers)
    from .search import ensure_fts
    from .seed import seed_reference_data

    engine = get_engine()
    Base.metadata.create_all(engine)
    _ensure_columns(engine)
    ensure_fts(engine)
    with session_scope() as s:
        seed_reference_data(s)
