"""The `concurrency` setting must actually do something.

It was config-only dead weight: read in config.py, never used, crawl fully
sequential. These tests hold it to three promises:

  1. detail fetches genuinely overlap when concurrency > 1
  2. every offer is still ingested exactly once, in one thread
  3. a detail fetch that raises does not lose the listing
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_conc_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sourcehub.pipeline.ingest import _batched, _fetch_details_parallel  # noqa: E402
from sourcehub.scrapers.base import RawOffer  # noqa: E402

FAILS: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


def check_true(label, got):
    check(label, bool(got), True)


class SlowAdapter:
    """Stands in for a site adapter whose product page takes 100ms."""

    DELAY = 0.10

    def __init__(self, tracker):
        self.tracker = tracker

    def fetch_detail(self, raw):
        with self.tracker["lock"]:
            self.tracker["active"] += 1
            self.tracker["peak"] = max(self.tracker["peak"], self.tracker["active"])
            self.tracker["threads"].add(threading.get_ident())
        time.sleep(self.DELAY)
        with self.tracker["lock"]:
            self.tracker["active"] -= 1
        raw.detail_fetched = True
        return raw

    def close(self):
        pass


class ExplodingAdapter:
    def fetch_detail(self, raw):
        raise RuntimeError("product page 500")

    def close(self):
        pass


def _tracker():
    return {"lock": threading.Lock(), "active": 0, "peak": 0, "threads": set()}


def _offers(n):
    return [
        RawOffer(site_key="dhgate", site_product_id=f"c-{i}",
                 url=f"https://example.test/{i}", title=f"Item {i}")
        for i in range(n)
    ]


def run() -> int:
    print()
    print("batching")
    check("splits evenly", [len(b) for b in _batched(range(10), 4)], [4, 4, 2])
    check("empty input", list(_batched([], 4)), [])
    check("smaller than batch", [len(b) for b in _batched(range(3), 8)], [3])

    print()
    print("serial baseline (concurrency=1)")
    tracker = _tracker()
    jobs = _offers(8)
    start = time.monotonic()
    out = _fetch_details_parallel([SlowAdapter(tracker)], jobs)
    serial = time.monotonic() - start
    check("all offers returned", len(out), 8)
    check("all enriched", sum(1 for o in out if o.detail_fetched), 8)
    check("never overlapped", tracker["peak"], 1)
    print(f"        {serial:.2f}s")

    print()
    print("parallel (concurrency=4)")
    tracker = _tracker()
    jobs = _offers(8)
    start = time.monotonic()
    out = _fetch_details_parallel([SlowAdapter(tracker) for _ in range(4)], jobs)
    parallel = time.monotonic() - start
    check("all offers returned", len(out), 8)
    check("all enriched", sum(1 for o in out if o.detail_fetched), 8)
    check_true(f"fetches actually overlapped (peak {tracker['peak']})",
               tracker["peak"] > 1)
    check_true(f"used multiple threads ({len(tracker['threads'])})",
               len(tracker["threads"]) > 1)
    # 8 jobs x 100ms: ~0.8s serial, ~0.2s across 4 workers. Assert a real speedup
    # rather than an exact figure, to stay stable on a loaded machine.
    check_true(f"faster than serial ({parallel:.2f}s vs {serial:.2f}s)",
               parallel < serial * 0.7)

    print()
    print("no offer is lost or duplicated")
    ids = sorted(o.site_product_id for o in out)
    check("every id present exactly once", ids, sorted(o.site_product_id for o in jobs))

    print()
    print("a failing product page keeps the listing")
    jobs = _offers(5)
    out = _fetch_details_parallel([ExplodingAdapter() for _ in range(3)], jobs)
    check("all listings survive", len(out), 5)
    check("none marked enriched", sum(1 for o in out if o.detail_fetched), 0)
    check("titles intact", sorted(o.title for o in out),
          sorted(o.title for o in jobs))

    print()
    print("config is wired up")
    from sourcehub.config import load_crawl_config

    cfg = load_crawl_config()
    for site in ("dhgate", "aliexpress", "1688"):
        c = cfg.site(site).get("concurrency")
        check_true(f"{site} declares concurrency ({c})", isinstance(c, int) and c >= 1)

    print()
    print("=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("crawl concurrency OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
