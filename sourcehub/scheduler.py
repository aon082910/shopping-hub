"""Background scheduler — this is what makes the catalog update itself.

Four jobs, all cron expressions from ``config.yaml``:

    full_crawl      sweep every enabled site for every seed keyword; new listings
                    are ingested with images and translated automatically
    refresh_prices  re-price known listings without re-running discovery (cheap)
    fx_rates        refresh currency conversion
    rematch         retry matching on listings that never found a sibling, since a
                    product crawled today may be the missing match for one from
                    last week

Run in the foreground with ``python -m sourcehub.cli schedule``. On Windows, wrap
that in Task Scheduler (or NSSM) to survive reboots; on Linux use a systemd unit.
"""

from __future__ import annotations

import logging
import signal
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import load_crawl_config
from .db.session import session_scope
from .pipeline.ingest import crawl_all, deactivate_stale, refresh_prices
from .util.money import refresh_fx_rates

log = logging.getLogger(__name__)


def job_full_crawl() -> None:
    log.info("scheduled: full crawl starting")
    results = crawl_all()
    for key, stats in results.items():
        log.info("  %s: %s", key, stats)
    removed = deactivate_stale(days=30)

    # Immediately, not on its own schedule: an alert that fires six hours late on a
    # listing that has since sold out is worthless.
    from .pipeline.watch import check_watches

    with session_scope() as session:
        triggers = check_watches(session)
    for t in triggers:
        log.info("WATCH HIT: %s at $%.2f on %s", t.product.title_en[:60], t.price, t.site)

    log.info("scheduled: full crawl done, %s stale listings retired, %s watches fired",
             removed, len(triggers))


def job_refresh_prices() -> None:
    log.info("scheduled: price refresh starting")
    stats = refresh_prices(older_than_hours=6, limit=800)
    log.info("scheduled: price refresh done, %s", stats)


def job_fx() -> None:
    with session_scope() as session:
        n = refresh_fx_rates(session)
    log.info("scheduled: %s FX rates refreshed", n)


def job_rematch() -> None:
    from .cli import cmd_rematch

    class _Args:
        limit = 5000

    log.info("scheduled: rematch starting")
    cmd_rematch(_Args())


def job_health_check() -> None:
    """Log loudly when a site stops yielding, so a rotted selector is noticed."""
    from .health import health_summary

    with session_scope() as session:
        summary = health_summary(session)
    for row in summary["attention"]:
        log.error("ADAPTER %s: %s -- %s", row.status.upper(), row.site_name, row.detail)
    if not summary["attention"]:
        log.info("adapter health: all %s sites ok", len(summary["sites"]))


def build_scheduler() -> BackgroundScheduler:
    cfg = load_crawl_config().schedule
    sched = BackgroundScheduler(
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600}
    )

    jobs = [
        ("full_crawl", cfg.get("full_crawl", "0 3 * * *"), job_full_crawl),
        ("refresh_prices", cfg.get("refresh_prices", "0 */6 * * *"), job_refresh_prices),
        ("fx_rates", cfg.get("fx_rates", "30 2 * * *"), job_fx),
        ("rematch", cfg.get("rematch", "0 5 * * 0"), job_rematch),
        ("health_check", cfg.get("health_check", "0 7 * * *"), job_health_check),
    ]
    for name, expr, fn in jobs:
        try:
            sched.add_job(fn, CronTrigger.from_crontab(expr), id=name, name=name)
            log.info("scheduled %-16s %s", name, expr)
        except Exception as e:
            log.error("bad cron expression for %s (%r): %s", name, expr, e)
    return sched


def run_scheduler() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
    )
    sched = build_scheduler()
    sched.start()

    stopping = {"flag": False}

    def _stop(signum, frame):
        stopping["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    try:
        signal.signal(signal.SIGTERM, _stop)
    except (AttributeError, ValueError):
        pass  # not available on all Windows configurations

    print("Scheduler running. Ctrl-C to stop.")
    for job in sched.get_jobs():
        print(f"  {job.name:<16} next run: {job.next_run_time}")

    try:
        while not stopping["flag"]:
            time.sleep(1)
    finally:
        sched.shutdown(wait=False)
        print("Scheduler stopped.")


if __name__ == "__main__":
    run_scheduler()
