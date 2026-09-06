"""Crawl on demand, driven by what people actually search for.

Without this the catalogue only ever contains what someone thought to crawl in
advance, so a search for anything else returns nothing and looks broken.

Four things make this safe to wire to a public search box:

* **It never blocks the request.** A crawl takes minutes; a search takes
  milliseconds. The search returns what is already in the database and the crawl
  happens behind it.
* **One worker, serially.** Ten people searching ten things does not mean ten
  simultaneous crawls across seventeen marketplaces. It means a queue.
* **A persisted cooldown.** The same keyword is not re-crawled for
  `cooldown_hours`, no matter how many times it is searched or how often the
  process restarts.
* **A per-caller budget.** The cooldown is per *keyword*, which stops the same
  search repeating but does nothing about one client asking for a hundred
  different ones. `per_client_hourly` caps how many crawls a single caller can
  start, so a bot walking a wordlist cannot monopolise the worker.
"""

from __future__ import annotations

import datetime as dt
import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import load_crawl_config
from ..db.models import SearchDemand
from ..db.session import session_scope

log = logging.getLogger(__name__)

_WS = re.compile(r"\s+")


def normalize(keyword: str) -> str:
    """Casefold and collapse whitespace so near-identical searches share state."""
    return _WS.sub(" ", (keyword or "").strip()).casefold()


@dataclass
class LiveSearchPolicy:
    """When an incoming search is allowed to start a crawl."""

    enabled: bool = True
    # Only crawl when the local catalogue is this thin. A search that already has
    # good answers does not need to touch the network.
    min_results: int = 5
    cooldown_hours: int = 24
    # Deliberately shallow: one page, no detail fetches. The goal is that results
    # appear within a minute or two, not that the crawl is exhaustive. The
    # scheduled full crawl deepens whatever proves popular.
    max_pages: int = 1
    fetch_details: bool = False
    max_queue: int = 20
    min_chars: int = 3
    # Crawls one caller may start per hour. Only requests that would actually
    # begin a crawl count against it -- a repeat of something already queued or
    # still in cooldown is free, because it costs no traffic.
    per_client_hourly: int = 6
    sites: list[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, cfg: Any | None = None) -> "LiveSearchPolicy":
        cfg = cfg or load_crawl_config()
        raw = (cfg._d.get("live_search") or {}) if hasattr(cfg, "_d") else {}
        base = cls()
        return cls(
            enabled=bool(raw.get("enabled", base.enabled)),
            min_results=int(raw.get("min_results", base.min_results)),
            cooldown_hours=int(raw.get("cooldown_hours", base.cooldown_hours)),
            max_pages=int(raw.get("max_pages", base.max_pages)),
            fetch_details=bool(raw.get("fetch_details", base.fetch_details)),
            max_queue=int(raw.get("max_queue", base.max_queue)),
            min_chars=int(raw.get("min_chars", base.min_chars)),
            per_client_hourly=int(raw.get("per_client_hourly", base.per_client_hourly)),
            sites=list(raw.get("sites") or []),
        )


# Outcomes of asking for a keyword to be crawled. Returned to the web layer so it
# can tell the user what is happening rather than silently doing nothing.
OFF = "off"                # feature disabled in config
TOO_SHORT = "too_short"    # query too short to be worth a crawl
HAVE_RESULTS = "have_results"
COOLDOWN = "cooldown"      # crawled recently
PENDING = "pending"        # already queued or running
QUEUED = "queued"          # accepted just now
BUSY = "busy"              # queue full
THROTTLED = "throttled"    # this caller has started enough crawls for one hour

# Upper bound on how many callers we keep a window for. A public instance will be
# scanned by things that rotate source addresses; without a cap the ledger is an
# unbounded dict keyed by attacker-controlled input.
MAX_TRACKED_CLIENTS = 4096


class CrawlQueue:
    """A single background worker draining a bounded queue of keywords."""

    def __init__(self) -> None:
        self._q: queue.Queue[str] = queue.Queue()
        # The policy in force when a keyword was accepted, so the worker crawls
        # with the same settings the caller was gated against rather than
        # silently re-reading config and doing something else.
        self._policies: dict[str, LiveSearchPolicy] = {}
        # Client key -> timestamps of the crawls it started, newest last. A plain
        # sliding window rather than a token bucket: the window is what the limit
        # is actually phrased as ("six an hour"), so there is nothing to translate.
        self._starts: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        # Keyword -> "queued" | "running". In-process view; the database row is
        # the durable record.
        self._inflight: dict[str, str] = {}
        self._stop = threading.Event()

    # ---------------------------------------------------------------- state
    def status(self, keyword: str) -> str | None:
        return self._inflight.get(normalize(keyword))

    def depth(self) -> int:
        return self._q.qsize()

    def inflight(self) -> dict[str, str]:
        return dict(self._inflight)

    # ---------------------------------------------------------------- submit
    def submit(self, keyword: str, *, local_results: int = 0,
               policy: LiveSearchPolicy | None = None,
               client: str | None = None) -> str:
        """Consider crawling `keyword`. Returns one of the outcome constants."""
        pol = policy or LiveSearchPolicy.from_config()
        norm = normalize(keyword)

        if not pol.enabled:
            return OFF
        if len(norm) < pol.min_chars:
            return TOO_SHORT
        if local_results >= pol.min_results:
            return HAVE_RESULTS

        with self._lock:
            if norm in self._inflight:
                return PENDING
            if self._q.qsize() >= pol.max_queue:
                return BUSY
            if self._recently_crawled(norm, pol.cooldown_hours):
                return COOLDOWN
            # Checked last, so only a request that would genuinely put traffic on
            # the wire spends any of the caller's budget.
            if not self._charge_client(client, pol.per_client_hourly):
                log.info("live search: %r throttled for client %s", norm, client)
                return THROTTLED
            self._inflight[norm] = QUEUED
            self._policies[norm] = pol
            self._record_request(norm, keyword, status=QUEUED)
            self._q.put(norm)
            self._ensure_worker()
        log.info("live search: queued %r (queue depth %d)", norm, self._q.qsize())
        return QUEUED

    # --------------------------------------------------------------- throttle
    def _charge_client(self, client: str | None, hourly: int) -> bool:
        """Spend one of `client`'s hourly crawls. False when it has none left.

        Callers with no identity (the CLI, the scheduler, tests) are never
        throttled: the limit exists to stop one *remote* caller monopolising the
        worker, and there is nothing to attribute a local call to.
        """
        if not client or hourly <= 0:
            return True

        now = time.monotonic()
        cutoff = now - 3600.0
        window = [t for t in self._starts.get(client, ()) if t > cutoff]
        if len(window) >= hourly:
            self._starts[client] = window
            return False

        window.append(now)
        self._starts[client] = window
        if len(self._starts) > MAX_TRACKED_CLIENTS:
            self._evict_clients(cutoff)
        return True

    def _evict_clients(self, cutoff: float) -> None:
        """Drop windows that have fully expired; if still over, drop the stalest.

        Evicting a live window hands that client a fresh budget, so expired
        entries go first and only genuine pressure costs anyone accuracy.
        """
        self._starts = {k: v for k, v in self._starts.items() if v and v[-1] > cutoff}
        if len(self._starts) <= MAX_TRACKED_CLIENTS:
            return
        stalest = sorted(self._starts, key=lambda k: self._starts[k][-1])
        for key in stalest[: len(self._starts) - MAX_TRACKED_CLIENTS]:
            self._starts.pop(key, None)

    # ---------------------------------------------------------------- worker
    def _ensure_worker(self) -> None:
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._run, name="live-search", daemon=True
            )
            self._worker.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                norm = self._q.get(timeout=30)
            except queue.Empty:
                return  # idle: let the thread die, submit() restarts it
            try:
                self._inflight[norm] = "running"
                self._crawl(norm)
            except Exception:
                log.exception("live search crawl failed for %r", norm)
            finally:
                self._inflight.pop(norm, None)
                self._policies.pop(norm, None)
                self._q.task_done()

    def _crawl(self, norm: str) -> None:
        # Imported here: ingest pulls in the scraper stack, which is heavy and not
        # needed to serve a page that never triggers a crawl.
        from .ingest import crawl_all

        pol = self._policies.get(norm) or LiveSearchPolicy.from_config()
        self._set_status(norm, "running")
        log.info("live search: crawling %r", norm)

        found = 0
        error: str | None = None
        try:
            stats = crawl_all(
                pol.sites or None,
                [norm],
                max_pages=pol.max_pages,
                fetch_details=pol.fetch_details,
            )
            found = sum(getattr(s, "seen", 0) for s in stats.values())
            log.info("live search: %r found %d listings", norm, found)
        except Exception as e:
            error = str(e)[:500]
            log.exception("live search: %r failed", norm)

        self._finish(norm, found=found, error=error)

    # ---------------------------------------------------------------- storage
    def _recently_crawled(self, norm: str, cooldown_hours: int) -> bool:
        with session_scope() as session:
            row = session.query(SearchDemand).filter_by(keyword=norm).one_or_none()
            if row is None or row.last_crawled is None:
                return False
            age = _utcnow() - _aware(row.last_crawled)
            return age < dt.timedelta(hours=cooldown_hours)

    def _record_request(self, norm: str, display: str, *, status: str) -> None:
        with session_scope() as session:
            row = session.query(SearchDemand).filter_by(keyword=norm).one_or_none()
            if row is None:
                session.add(
                    SearchDemand(
                        keyword=norm, display=display.strip()[:255],
                        last_status=status, request_count=1,
                    )
                )
            else:
                row.last_requested = _utcnow()
                row.request_count += 1
                row.last_status = status

    def _set_status(self, norm: str, status: str) -> None:
        with session_scope() as session:
            row = session.query(SearchDemand).filter_by(keyword=norm).one_or_none()
            if row is not None:
                row.last_status = status

    def _finish(self, norm: str, *, found: int, error: str | None) -> None:
        with session_scope() as session:
            row = session.query(SearchDemand).filter_by(keyword=norm).one_or_none()
            if row is None:
                return
            # Stamp last_crawled even on failure: a site that just blocked us will
            # block us again in a minute, and retrying every keystroke is how you
            # turn one block into a ban.
            row.last_crawled = _utcnow()
            row.crawl_count += 1
            row.offers_found = found
            row.last_status = "failed" if error else "done"
            row.last_error = error


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


# One queue per process.
QUEUE = CrawlQueue()


def request_crawl(keyword: str, local_results: int = 0,
                  client: str | None = None) -> str:
    return QUEUE.submit(keyword, local_results=local_results, client=client)


def crawl_status(keyword: str) -> dict[str, Any]:
    """What the UI needs to decide whether to keep polling."""
    norm = normalize(keyword)
    state = QUEUE.status(norm)
    with session_scope() as session:
        row = session.query(SearchDemand).filter_by(keyword=norm).one_or_none()
        return {
            "keyword": norm,
            "state": state or (row.last_status if row else None),
            "active": state is not None,
            "found": (row.offers_found if row else 0),
            "last_crawled": (row.last_crawled.isoformat() if row and row.last_crawled else None),
            "error": (row.last_error if row else None),
            "queue_depth": QUEUE.depth(),
        }
