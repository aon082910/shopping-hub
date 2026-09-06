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
    THROTTLED,
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


def test_one_client_cannot_own_the_worker():
    """The cooldown is per keyword, so without this one caller walks a wordlist.

    Distinct keywords each pass the cooldown check individually; only the
    per-client budget notices that they all came from the same place.
    """
    q = fresh()
    pol = LiveSearchPolicy(enabled=True, min_results=5, cooldown_hours=24,
                           max_pages=1, max_queue=50, min_chars=3,
                           per_client_hourly=3)

    got = [q.submit(f"widget {i}", local_results=0, policy=pol, client="10.0.0.9")
           for i in range(5)]
    assert got[:3] == [QUEUED] * 3, f"first three should queue, got {got[:3]}"
    assert got[3:] == [THROTTLED] * 2, f"rest should throttle, got {got[3:]}"

    # A different caller is unaffected -- the limit is per client, not global.
    assert q.submit("widget 9", local_results=0, policy=pol, client="10.0.0.10") == QUEUED
    print("  one client's budget does not spend another's")


def test_free_outcomes_do_not_spend_budget():
    """Only work that would really hit the network is charged for.

    Otherwise refreshing a page whose keyword is already queued would burn the
    budget on requests that cause no traffic at all.
    """
    q = fresh()
    pol = LiveSearchPolicy(enabled=True, min_results=5, cooldown_hours=24,
                           max_pages=1, max_queue=50, min_chars=3,
                           per_client_hourly=2)
    client = "10.0.0.11"

    assert q.submit("usb hub", local_results=0, policy=pol, client=client) == QUEUED
    # Repeats of an in-flight keyword, and queries already answered locally.
    for _ in range(6):
        assert q.submit("usb hub", local_results=0, policy=pol, client=client) == PENDING
        assert q.submit("hdmi cable", local_results=99, policy=pol,
                        client=client) == HAVE_RESULTS

    # One of the two crawls is still unspent, despite fourteen extra requests.
    assert q.submit("sd card", local_results=0, policy=pol, client=client) == QUEUED
    assert q.submit("psu 12v", local_results=0, policy=pol, client=client) == THROTTLED
    print("  repeats and locally-answered queries are free")


def test_local_callers_are_never_throttled():
    """The CLI and scheduler have no client identity and must not be limited."""
    q = fresh()
    pol = LiveSearchPolicy(enabled=True, min_results=5, cooldown_hours=24,
                           max_pages=1, max_queue=50, min_chars=3,
                           per_client_hourly=1)
    got = [q.submit(f"part {i}", local_results=0, policy=pol) for i in range(4)]
    assert got == [QUEUED] * 4, f"unattributed calls were throttled: {got}"
    print("  callers with no identity are not rate limited")


def test_throttle_ledger_stays_bounded():
    """A scanner rotating source addresses must not grow the ledger forever."""
    q = fresh()
    pol = LiveSearchPolicy(enabled=True, min_results=5, cooldown_hours=24,
                           max_pages=1, max_queue=100000, min_chars=3,
                           per_client_hourly=1)
    for i in range(ondemand.MAX_TRACKED_CLIENTS + 500):
        q.submit(f"k{i}", local_results=0, policy=pol, client=f"198.51.100.{i}")
    assert len(q._starts) <= ondemand.MAX_TRACKED_CLIENTS, (
        f"ledger grew to {len(q._starts)} entries"
    )
    print("  the per-client ledger is capped")


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
    test_one_client_cannot_own_the_worker()
    test_free_outcomes_do_not_spend_budget()
    test_local_callers_are_never_throttled()
    test_throttle_ledger_stays_bounded()
    print("live search OK")
