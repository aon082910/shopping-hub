"""Reclaiming disk: history pruning, orphaned media, VACUUM, and backups.

A self-hosted instance runs for years. Three things grow without anything ever
removing them:

* **Price history.** One row per offer per refresh, forever. It is the single
  fastest-growing table and almost nobody looks past the last year of it.
* **Media.** Images are written to disk before their row exists and are shared
  between rows by content hash, so neither a failed ingest nor a product merge
  leaves anything that cleans up after itself.
* **The database file itself.** SQLite does not return freed pages to the
  filesystem; deleting a million rows makes the file no smaller until VACUUM.

None of this is destructive to the catalogue. ``deactivate_stale`` (``prune
--days``) is what retires listings; everything here removes only data that is
already unreachable or older than a retention window you set.

Backups use SQLite's online backup API rather than copying the file. A plain copy
of a database being written to is a corrupt copy; the backup API takes a
transactionally consistent snapshot while the app keeps serving.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import delete, func, select

from ..config import get_settings, load_crawl_config
from ..db.models import Image, Offer, PriceHistory
from ..db.session import session_scope

log = logging.getLogger(__name__)

# A file younger than this is never swept, however unreferenced it looks. The
# image store writes the file and *then* inserts the row; without this window a
# sweep landing between the two would delete a live download.
MEDIA_GRACE_SECONDS = 24 * 3600

BACKUP_PREFIX = "sourcehub-"
BACKUP_SUFFIX = ".db"


@dataclass
class RetentionPolicy:
    """What the scheduled retention pass is allowed to remove."""

    # Keep price history this long. Roughly a year by default: enough to see a
    # seasonal cycle on the product page sparkline, which is what it is for.
    price_history_days: int = 400
    orphan_media: bool = True
    vacuum: bool = True
    # Backups to keep. 0 disables backups entirely.
    backups: int = 7
    backup_dir: str = ""

    @classmethod
    def from_config(cls, cfg: Any | None = None) -> "RetentionPolicy":
        cfg = cfg or load_crawl_config()
        raw = (cfg._d.get("retention") or {}) if hasattr(cfg, "_d") else {}
        base = cls()
        return cls(
            price_history_days=int(
                raw.get("price_history_days", base.price_history_days)
            ),
            orphan_media=bool(raw.get("orphan_media", base.orphan_media)),
            vacuum=bool(raw.get("vacuum", base.vacuum)),
            backups=int(raw.get("backups", base.backups)),
            backup_dir=str(raw.get("backup_dir", base.backup_dir) or ""),
        )


@dataclass
class Report:
    """What a retention pass actually did, for logging and for the CLI."""

    history_rows: int = 0
    media_rows: int = 0
    media_files: int = 0
    media_bytes: int = 0
    vacuum_bytes: int = 0
    backup: Optional[str] = None
    backups_removed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "history_rows": self.history_rows,
            "media_rows": self.media_rows,
            "media_files": self.media_files,
            "media_bytes": self.media_bytes,
            "vacuum_bytes": self.vacuum_bytes,
            "backup": self.backup,
            "backups_removed": self.backups_removed,
            "errors": self.errors,
        }


# --------------------------------------------------------------------- history


def prune_price_history(days: int) -> int:
    """Delete price points older than ``days``. Returns rows removed.

    The newest point per offer is kept regardless of age, so an offer that
    stopped being re-priced still shows a price rather than an empty chart.
    """
    if days <= 0:
        return 0
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)

    with session_scope() as session:
        newest = (
            select(func.max(PriceHistory.id))
            .group_by(PriceHistory.offer_id)
            .scalar_subquery()
        )
        result = session.execute(
            delete(PriceHistory).where(
                PriceHistory.ts < cutoff,
                PriceHistory.id.not_in(newest),
            )
        )
        return int(result.rowcount or 0)


# ----------------------------------------------------------------------- media


def prune_orphan_media(grace_seconds: int = MEDIA_GRACE_SECONDS) -> tuple[int, int, int]:
    """Drop unreachable Image rows, then sweep unreferenced files off disk.

    Returns ``(rows, files, bytes)``.

    Order matters: rows first, so files that only the deleted rows referenced are
    included in the same sweep. Files are matched against the paths *remaining*
    afterwards rather than against the deleted rows, because the image store
    deduplicates by content hash -- several rows routinely point at one file, and
    deleting per-row would delete a file another row still needs.
    """
    root = get_settings().media_path

    with session_scope() as session:
        live_offers = select(Offer.id)
        orphan_ids = session.scalars(
            select(Image.id).where(
                Image.canonical_id.is_(None),
                Image.offer_id.is_not(None),
                Image.offer_id.not_in(live_offers),
            )
        ).all()
        if orphan_ids:
            session.execute(delete(Image).where(Image.id.in_(orphan_ids)))
            session.flush()

        referenced: set[str] = set()
        for local, thumb in session.execute(
            select(Image.local_path, Image.thumb_path)
        ).all():
            if local:
                referenced.add(local.replace("\\", "/"))
            if thumb:
                referenced.add(thumb.replace("\\", "/"))

    files = removed_bytes = 0
    now = time.time()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel in referenced:
            continue
        try:
            stat = path.stat()
            if now - stat.st_mtime < grace_seconds:
                continue  # too new to be sure it is not mid-write
            size = stat.st_size
            path.unlink()
        except OSError as e:
            log.warning("could not remove orphaned media %s: %s", rel, e)
            continue
        files += 1
        removed_bytes += size

    return len(orphan_ids), files, removed_bytes


# ---------------------------------------------------------------------- sqlite


def _sqlite_path() -> Optional[Path]:
    """Filesystem path of the SQLite database, or None if not using SQLite."""
    url = get_settings().db_url
    if not url.startswith("sqlite:"):
        return None
    tail = url.split("sqlite:///", 1)[-1]
    return Path(tail) if tail else None


def vacuum() -> int:
    """Compact the database file. Returns bytes reclaimed (may be negative).

    Runs outside SQLAlchemy's session machinery: VACUUM cannot run inside a
    transaction, and the ORM opens one for essentially everything.
    """
    path = _sqlite_path()
    if path is None or not path.exists():
        return 0
    before = path.stat().st_size
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("VACUUM")
    finally:
        conn.close()
    return before - path.stat().st_size


def backup(dest: Optional[Path] = None, keep: int = 0) -> Path:
    """Snapshot the database with SQLite's online backup API.

    Written to a temporary name and renamed on success, so an interrupted backup
    can never be mistaken for a good one -- which is exactly the moment you would
    find out, months later, that it was truncated.
    """
    src = _sqlite_path()
    if src is None:
        raise RuntimeError("backup only supports SQLite databases")
    if not src.exists():
        raise FileNotFoundError(f"no database at {src}")

    if dest is None:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = default_backup_dir() / f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}"
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    tmp = dest.with_name(dest.name + ".part")
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(str(tmp))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    os.replace(tmp, dest)

    if keep > 0:
        prune_backups(dest.parent, keep)
    return dest


def default_backup_dir() -> Path:
    path = _sqlite_path()
    base = path.parent if path else Path.cwd()
    return base / "backups"


def prune_backups(directory: Path, keep: int) -> int:
    """Keep the ``keep`` newest snapshots in ``directory``. Returns count removed.

    Matched by our own naming so a directory shared with anything else -- or a
    snapshot someone renamed to keep it -- is left alone.
    """
    if keep <= 0:
        return 0
    snaps = sorted(
        (
            p
            for p in directory.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}")
            if p.is_file()
        ),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for stale in snaps[keep:]:
        try:
            stale.unlink()
            removed += 1
        except OSError as e:
            log.warning("could not remove old backup %s: %s", stale.name, e)
    return removed


# ------------------------------------------------------------------ the pass


def run_retention(policy: Optional[RetentionPolicy] = None) -> Report:
    """Back up first, then reclaim. Never lets one failure skip the rest.

    Backup leads deliberately: every other step here deletes something, and a
    snapshot taken beforehand is the one that still has it.
    """
    pol = policy or RetentionPolicy.from_config()
    report = Report()

    if pol.backups > 0:
        try:
            path = backup(keep=pol.backups)
            report.backup = str(path)
            report.backups_removed = prune_backups(
                Path(pol.backup_dir) if pol.backup_dir else default_backup_dir(),
                pol.backups,
            )
        except Exception as e:
            report.errors.append(f"backup: {e}")
            log.exception("retention: backup failed")

    try:
        report.history_rows = prune_price_history(pol.price_history_days)
    except Exception as e:
        report.errors.append(f"price history: {e}")
        log.exception("retention: price history prune failed")

    if pol.orphan_media:
        try:
            rows, files, size = prune_orphan_media()
            report.media_rows, report.media_files, report.media_bytes = rows, files, size
        except Exception as e:
            report.errors.append(f"media: {e}")
            log.exception("retention: media sweep failed")

    # Last: it is the step that turns the deletions above into free space.
    if pol.vacuum:
        try:
            report.vacuum_bytes = vacuum()
        except Exception as e:
            report.errors.append(f"vacuum: {e}")
            log.exception("retention: vacuum failed")

    return report


def human_bytes(n: int) -> str:
    step = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(step) < 1024 or unit == "GB":
            return f"{step:.0f} {unit}" if unit == "B" else f"{step:.1f} {unit}"
        step /= 1024
    return f"{step:.1f} GB"
