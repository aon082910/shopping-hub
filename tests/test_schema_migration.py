"""init_db() must upgrade an existing database in place, not just create new ones.

Base.metadata.create_all() only creates tables that don't exist yet -- a table
that's already there but missing a column a newer version of the code added is
left exactly as it was, and the next query touching that column fails with "no
such column". There is no separate migration step in this project's actual
deployment (Docker/Unraid: pull a new image, restart), so this has to happen
inside init_db() or a schema change breaks every existing installation the
moment it upgrades. Simulated here by hand-creating an old-shaped `watches`
table and confirming init_db() brings it up to date without touching its data.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_migrate_"))
_DB_PATH = _TMP / "t.db"
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{_DB_PATH.as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sourcehub.db.session import init_db  # noqa: E402

FAILS: list[str] = []


def check(label, got, want=True) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


def main() -> None:
    # A pre-existing "watches" table shaped like the version before on_new_site /
    # known_site_ids existed, with one real row -- the thing an in-place upgrade
    # must not lose.
    con = sqlite3.connect(str(_DB_PATH))
    con.executescript("""
        CREATE TABLE watches (
            id INTEGER PRIMARY KEY,
            canonical_id INTEGER NOT NULL,
            label VARCHAR(120) NOT NULL DEFAULT '',
            target_usd FLOAT,
            enabled BOOLEAN NOT NULL DEFAULT 1
        );
        INSERT INTO watches (id, canonical_id, label, target_usd, enabled)
        VALUES (1, 42, 'pre-existing watch', 9.99, 1);
    """)
    con.commit()
    con.close()

    init_db()

    with sqlite3.connect(str(_DB_PATH)) as con:
        cols = {row[1] for row in con.execute("PRAGMA table_info(watches)")}
        check("on_new_site column added", "on_new_site" in cols)
        check("known_site_ids column added", "known_site_ids" in cols)

        row = con.execute(
            "SELECT canonical_id, label, target_usd, on_new_site, known_site_ids "
            "FROM watches WHERE id = 1"
        ).fetchone()
        check("the pre-existing row survived", row[:3], (42, "pre-existing watch", 9.99))
        check("new NOT NULL boolean column backfilled to a real value", row[3], 0)
        check("new JSON column backfilled to an empty list, not NULL", row[4], "[]")

    print("re-running init_db() again is a no-op, not an error")
    init_db()
    with sqlite3.connect(str(_DB_PATH)) as con:
        count = con.execute("SELECT COUNT(*) FROM watches").fetchone()[0]
        check("no duplicate columns, no duplicate rows", count, 1)

    print("the ORM can read and write through the migrated column")
    from sqlalchemy import select

    from sourcehub.db.models import Watch
    from sourcehub.db.session import session_scope

    with session_scope() as s:
        w = s.scalar(select(Watch).where(Watch.id == 1))
        check("ORM reads the backfilled default", w.on_new_site, False)
        check("ORM reads the backfilled JSON default", w.known_site_ids, [])
        w.on_new_site = True
        w.known_site_ids = [1, 2, 3]

    with session_scope() as s:
        w = s.scalar(select(Watch).where(Watch.id == 1))
        check("write through the migrated column round-trips", w.on_new_site, True)
        check("write through the migrated JSON column round-trips", w.known_site_ids, [1, 2, 3])

    print("\n" + "=" * 60)
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(" -", f)
        sys.exit(1)
    print("schema migration OK")


if __name__ == "__main__":
    main()
