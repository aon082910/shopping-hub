"""Manual, whole-catalog crawl for one site, triggered from the admin page.

Everything else that starts a crawl is either scheduled (``scheduler.py``'s
nightly ``full_crawl`` job) or reactive to what a visitor searched for
(``ondemand.py``, one shallow page for a single keyword someone typed). This is
the third case: an admin clicking "Crawl" on one site because they want its
whole catalog built now rather than waiting for 3am.

"Whole catalog" means whatever the site actually supports: if the adapter
implements ``category_seeds()``/``crawl_category()`` (a real, bounded walk of
its own catalog -- a sitemap, a category tree), that runs instead of the
keyword sweep, since it finds everything rather than only what a fixed keyword
list happens to guess. Most marketplaces here have no such thing (eBay,
AliExpress, Taobao... there is no "all products" on a site with tens of
millions of listings), so they fall back to sweeping every seed keyword in
config.yaml, exactly like the nightly job.

A request only starts a crawl; it never waits for one, since a full sweep can
run for hours. State lives in memory (a crawl in progress is visible either
way, as a running ``CrawlRun`` row on the admin page), which means it only
coordinates clicks within this one process -- the same limitation
``ondemand.CrawlQueue`` already has, and for the same reason: this is a
single-process app.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

_running: set[str] = set()
_lock = threading.Lock()


def is_running(site_key: str) -> bool:
    with _lock:
        return site_key in _running


def running_sites() -> set[str]:
    with _lock:
        return set(_running)


def start_full_crawl(site_key: str) -> bool:
    """Kick off a full-catalog crawl for ``site_key`` in the background.

    Returns False without starting anything if one is already running for this
    site; True once a worker thread has been handed the job.
    """
    with _lock:
        if site_key in _running:
            return False
        _running.add(site_key)

    def _run() -> None:
        # Imported here, not at module load: ingest pulls in the whole scraper
        # stack, which the web process otherwise has no reason to load.
        from ..scrapers.registry import get_adapter
        from .ingest import crawl_site, crawl_site_categories

        adapter = get_adapter(site_key)
        try:
            has_categories = bool(adapter.category_seeds())
        finally:
            adapter.close()

        mode = "category" if has_categories else "keyword"
        log.info("[%s] manual full-catalog crawl starting (%s mode)", site_key, mode)
        try:
            stats = (
                crawl_site_categories(site_key) if has_categories else crawl_site(site_key)
            )
            log.info("[%s] manual full-catalog crawl done (%s mode): %s", site_key, mode, stats)
        except Exception:
            log.exception("[%s] manual full-catalog crawl failed", site_key)
        finally:
            with _lock:
                _running.discard(site_key)

    threading.Thread(target=_run, name=f"full-crawl-{site_key}", daemon=True).start()
    return True
