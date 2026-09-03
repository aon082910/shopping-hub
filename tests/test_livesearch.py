"""Search-triggered crawling: the gating that keeps it from hammering sites."""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_live_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sourcehub.db.models import SearchDemand  # noqa: E402
from sourcehub.db.session import init_db, session_scope  # noqa: E402
from sourcehub.pipeline import ondemand  # noqa: E402
from sourcehub.pipeline.ondemand import (  # noqa: E402
    BUSY,
    COOLDOWN,
    HAVE_RESULTS,
    OFF,
    PENDING,
    QUEUED,
    TOO_SHORT,
    CrawlQueue,
    LiveSearchPolicy,
    normalize,
)

POL = LiveSearchPolicy(enabled=True, min_results=5, cooldown_hours=24,
                       max_pages=1, fetch_details=False, max_queue=3, min_chars=3)


def fresh() -> CrawlQueue:
    """A queue whose worker never starts, so submit() can be tested alone."""
    q = CrawlQueue()
    q._ensure_worker = lambda: None  # type: ignore[method-assign]
    return q


def test_normalize():
    assert normalize("  USB   Hub ") == "usb hub"
    assert normalize("USB HUB") == normalize("usb hub")
    print("  normalize collapses case and whitespace")


def test_gating():
    q = fresh()
    assert q.submit("usb hub", local_results=0,
                    policy=LiveSearchPolicy(enabled=False)) == OFF
    assert q.submit("ab", local_results=0, policy=POL) == TOO_SHORT
    # The important one: a query that already has answers must not hit the network.
    assert q.submit("usb hub", local_results=50, policy=POL) == HAVE_RESULTS
    print("  disabled / too short / already-answered queries do not queue")


def test_queues_once_then_dedupes():
    q = fresh()
    assert q.submit("led strip", local_results=0, policy=POL) == QUEUED
    # Same keyword again, and a differently-cased variant, must not double-queue.
    assert q.submit("led strip", local_results=0, policy=POL) == PENDING
    assert q.submit("LED   Strip", local_results=0, policy=POL) == PENDING
    assert q.depth() == 1
    print("  repeat searches collapse onto one queued crawl")


def test_queue_bound():
    q = fresh()
    for i in range(POL.max_queue):
        assert q.submit(f"widget {i}", local_results=0, policy=POL) == QUEUED
    # Past the bound it refuses rather than growing without limit.
    assert q.submit("one too many", local_results=0, policy=POL) == BUSY
    print(f"  queue refuses work past max_queue={POL.max_queue}")


def test_cooldown_is_persisted():
    q = fresh()
    with session_scope() as s:
        s.add(SearchDemand(
            keyword="recent thing", display="recent thing",
            last_crawled=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1),
            last_status="done",
        ))
    assert q.submit("recent thing", local_results=0, policy=POL) == COOLDOWN

    # Same row, but crawled long enough ago that it is allowed again.
    with session_scope() as s:
        row = s.query(SearchDemand).filter_by(keyword="recent thing").one()
        row.last_crawled = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)
    assert fresh().submit("recent thing", local_results=0, policy=POL) == QUEUED
    print("  cooldown blocks a recent keyword and expires on schedule")


def test_failed_crawl_still_sets_cooldown():
    """A blocked site blocks again a minute later; retrying every search is how a
    block becomes a ban."""
    q = fresh()
    q._record_request("boom", "boom", status=QUEUED)
    q._finish("boom", found=0, error="blocked")
    with session_scope() as s:
        row = s.query(SearchDemand).filter_by(keyword="boom").one()
        assert row.last_status == "failed"
        assert row.last_crawled is not None
    assert fresh().submit("boom", local_results=0, policy=POL) == COOLDOWN
    print("  a failed crawl still starts the cooldown")


def test_request_counting():
    q = fresh()
    q._record_request("demand", "demand", status=QUEUED)
    q._record_request("demand", "demand", status=QUEUED)
    q._record_request("demand", "demand", status=QUEUED)
    with session_scope() as s:
        assert s.query(SearchDemand).filter_by(keyword="demand").one().request_count == 3
    print("  repeat demand is counted, so popular keywords are visible")


def test_worker_uses_the_policy_it_was_queued_with():
    """The worker must not re-read config and crawl something other than what the
    caller was gated against -- that silently ignores a narrowed site list."""
    q = fresh()
    pol = LiveSearchPolicy(enabled=True, min_results=5, cooldown_hours=24,
                           max_pages=1, fetch_details=False, max_queue=3,
                           sites=["dhgate"])
    assert q.submit("scoped crawl", local_results=0, policy=pol) == QUEUED
    stored = q._policies[normalize("scoped crawl")]
    assert stored.sites == ["dhgate"], stored.sites
    print("  worker keeps the submitted policy (site scope is honoured)")


if __name__ == "__main__":
    init_db()
    test_normalize()
    test_gating()
    test_queues_once_then_dedupes()
    test_queue_bound()
    test_cooldown_is_persisted()
    test_failed_crawl_still_sets_cooldown()
    test_request_counting()
    test_worker_uses_the_policy_it_was_queued_with()
    print("live search OK")
