"""Retention: what it reclaims, and -- more importantly -- what it must not.

Every test here is really the same test asked from a different angle: this is the
only code in the project that deletes things, so the interesting assertions are
the negative ones.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_ret_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sourcehub.config import get_settings  # noqa: E402
from sourcehub.db.models import CanonicalProduct, Image, Offer, PriceHistory, Site  # noqa: E402
from sourcehub.db.session import init_db, session_scope  # noqa: E402
from sourcehub.pipeline import retention  # noqa: E402


def _seed() -> tuple[int, int]:
    """One product, one live offer, and a spread of price history. Returns ids."""
    init_db()
    with session_scope() as session:
        site = session.query(Site).first()
        if site is None:
            site = Site(key="test", name="Test")
            session.add(site)
            session.flush()
        product = CanonicalProduct(slug=f"p{time.time_ns()}", title_en="Widget")
        session.add(product)
        session.flush()
        offer = Offer(
            site_id=site.id, canonical_id=product.id,
            url=f"https://example.test/{time.time_ns()}", title_raw="Widget",
            site_product_id=str(time.time_ns()),
        )
        session.add(offer)
        session.flush()

        now = dt.datetime.now(dt.timezone.utc)
        for age_days, price in ((900, 10.0), (500, 9.0), (30, 8.0), (0, 7.0)):
            session.add(
                PriceHistory(
                    offer_id=offer.id, price_usd=price,
                    ts=now - dt.timedelta(days=age_days),
                )
            )
        session.flush()
        return product.id, offer.id


def test_history_prune_keeps_the_window_and_the_newest():
    _pid, offer_id = _seed()
    removed = retention.prune_price_history(days=400)

    with session_scope() as session:
        left = session.query(PriceHistory).filter_by(offer_id=offer_id).all()
        ages = sorted(p.price_usd for p in left)

    # 900 and 500 days both sit outside a 400-day window; 30 and 0 are inside it.
    assert removed == 2, f"expected the two points past the window to go, removed {removed}"
    assert ages == [7.0, 8.0], f"wrong rows survived: {ages}"
    print("  history prune removes only rows past the window")


def test_history_prune_never_empties_an_offer():
    """An offer nobody re-prices must keep a price, however old it is."""
    _pid, offer_id = _seed()
    with session_scope() as session:
        session.query(PriceHistory).filter_by(offer_id=offer_id).delete()
        session.add(
            PriceHistory(
                offer_id=offer_id, price_usd=5.0,
                ts=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5000),
            )
        )

    retention.prune_price_history(days=1)

    with session_scope() as session:
        left = session.query(PriceHistory).filter_by(offer_id=offer_id).count()
    assert left == 1, "the last price point for an offer must never be deleted"
    print("  the newest point survives regardless of age")


def test_media_sweep_spares_shared_and_recent_files():
    """The two ways a naive sweep eats live data.

    Images are deduplicated by content hash, so one file is routinely referenced
    by several rows -- deleting per-row would take a file another row still needs.
    And the store writes the file before inserting its row, so anything young is
    treated as possibly mid-write.
    """
    product_id, offer_id = _seed()
    root = get_settings().media_path
    (root / "ab").mkdir(parents=True, exist_ok=True)

    shared = root / "ab" / "shared.jpg"
    orphan = root / "ab" / "orphan.jpg"
    fresh = root / "ab" / "fresh.jpg"
    for f in (shared, orphan, fresh):
        f.write_bytes(b"x" * 32)

    old = time.time() - 10 * 24 * 3600
    os.utime(shared, (old, old))
    os.utime(orphan, (old, old))   # old and unreferenced -> should go
    # `fresh` keeps its current mtime: unreferenced, but inside the grace window.

    with session_scope() as session:
        for _ in range(2):  # two rows, one file -- the dedupe case
            session.add(
                Image(
                    canonical_id=product_id, offer_id=offer_id,
                    src_url="https://example.test/i.jpg",
                    local_path="ab/shared.jpg", sha256="deadbeef",
                )
            )

    rows, files, _size = retention.prune_orphan_media()

    assert shared.exists(), "a file referenced by a surviving row was deleted"
    assert fresh.exists(), "a file inside the grace window was deleted"
    assert not orphan.exists(), "the genuinely orphaned file was not removed"
    assert files == 1, f"expected exactly one file removed, got {files}"
    assert rows == 0, f"no image row was unreachable, but {rows} were deleted"
    print("  media sweep spares shared and recently-written files")


def test_backup_is_consistent_and_rotates():
    _seed()
    out = _TMP / "snaps"
    first = retention.backup(dest=out / "sourcehub-1.db")

    conn = sqlite3.connect(str(first))
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("select count(*) from canonical_products").fetchone()[0] >= 1
    finally:
        conn.close()

    for n in (2, 3, 4):
        path = out / f"sourcehub-{n}.db"
        retention.backup(dest=path)
        os.utime(path, (time.time() + n, time.time() + n))  # deterministic order

    removed = retention.prune_backups(out, keep=2)
    left = sorted(p.name for p in out.glob("sourcehub-*.db"))
    assert removed == 2, f"expected 2 rotated out, got {removed}"
    assert left == ["sourcehub-3.db", "sourcehub-4.db"], f"kept the wrong ones: {left}"
    print("  backups are readable snapshots and rotate oldest-first")


def test_partial_backup_is_never_left_behind():
    """A .part file must not be mistaken for a snapshot by the rotation glob."""
    _seed()
    out = _TMP / "partial"
    retention.backup(dest=out / "sourcehub-a.db")
    (out / "sourcehub-b.db.part").write_bytes(b"truncated")

    kept = sorted(p.name for p in out.glob(f"{retention.BACKUP_PREFIX}*{retention.BACKUP_SUFFIX}"))
    assert kept == ["sourcehub-a.db"], f"the .part file was counted as a backup: {kept}"
    print("  an interrupted backup is not counted as one")


if __name__ == "__main__":
    for fn in (
        test_history_prune_keeps_the_window_and_the_newest,
        test_history_prune_never_empties_an_offer,
        test_media_sweep_spares_shared_and_recent_files,
        test_backup_is_consistent_and_rotates,
        test_partial_backup_is_never_left_behind,
    ):
        fn()
    print("retention OK")
