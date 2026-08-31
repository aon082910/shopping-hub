"""Adapter health detection.

The failure mode being caught: a rotted selector does not raise. The crawl reports
success and finds nothing, which looks identical to "no results" unless you compare
a site against its own history.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_health_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")

from sourcehub.db.models import CrawlRun  # noqa: E402
from sourcehub.db.session import init_db, session_scope  # noqa: E402
from sourcehub.health import adapter_health, health_summary  # noqa: E402

FAILS: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


def seed_runs(site_key, yields, ok=True, base_hours=200):
    """Write crawl history, oldest first."""
    now = dt.datetime.now(dt.timezone.utc)
    with session_scope() as s:
        for i, n in enumerate(yields):
            ts = now - dt.timedelta(hours=base_hours - i)
            s.add(CrawlRun(
                site_key=site_key, mode="search", query="test",
                started_at=ts, finished_at=ts, ok=ok,
                offers_seen=n, offers_new=n, offers_updated=0,
            ))


def status_of(rows, key):
    return next((r.status for r in rows if r.site_key == key), None)


def run() -> int:
    init_db()

    print()
    print("statuses")
    # Healthy: consistent yield throughout.
    seed_runs("dhgate", [40] * 20)
    # Broken: used to work, recent runs find nothing.
    seed_runs("aliexpress", [50] * 15 + [0] * 5)
    # Degraded: yield collapsed but is not zero.
    seed_runs("alibaba", [60] * 15 + [4] * 5)
    # Blocked: recent runs errored.
    with session_scope() as s:
        now = dt.datetime.now(dt.timezone.utc)
        # Distinct timestamps, oldest first, so "recent" is unambiguous.
        for i in range(15):
            ts = now - dt.timedelta(hours=200 - i)
            s.add(CrawlRun(site_key="banggood", mode="search", started_at=ts,
                           finished_at=ts, ok=True, offers_seen=30))
        for i in range(4):
            ts = now - dt.timedelta(hours=20 - i)
            s.add(CrawlRun(site_key="banggood", mode="search", started_at=ts,
                           finished_at=ts, ok=False, offers_seen=0,
                           error="BlockedError"))
    # New: too little history to judge.
    seed_runs("chinavasion", [25, 25])

    with session_scope() as s:
        rows = adapter_health(s)

    check("healthy site is ok", status_of(rows, "dhgate"), "ok")
    check("zero-yield regression is broken", status_of(rows, "aliexpress"), "broken")
    check("collapsed yield is degraded", status_of(rows, "alibaba"), "degraded")
    check("erroring site is blocked", status_of(rows, "banggood"), "blocked")
    check("thin history is new", status_of(rows, "chinavasion"), "new")
    # An untouched site must not be reported as broken -- never-worked and
    # stopped-working need different responses.
    check("never crawled is idle", status_of(rows, "taobao"), "idle")

    print()
    print("details explain themselves")
    broken = next(r for r in rows if r.site_key == "aliexpress")
    check("broken cites the baseline", "used to average" in broken.detail, True)
    degraded = next(r for r in rows if r.site_key == "alibaba")
    check("degraded cites the ratio", "baseline" in degraded.detail, True)

    print()
    print("summary")
    with session_scope() as s:
        summary = health_summary(s)
    attention = {r.site_key for r in summary["attention"]}
    check("flags exactly the three problems",
          attention, {"aliexpress", "alibaba", "banggood"})
    check("idle sites are not flagged", "taobao" in attention, False)
    check("healthy sites are not flagged", "dhgate" in attention, False)
    check("counts add up",
          sum(summary["counts"].values()), len(summary["sites"]))

    print()
    print("=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("adapter health OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
