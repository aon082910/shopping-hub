"""Adapter health: notice when a site quietly stops returning listings.

A rotted selector does not raise. The crawl completes, reports success, and finds
nothing -- which is indistinguishable from "no results for that keyword" unless you
compare against what the site used to yield. That is the gap this closes: it reads
the ``crawl_runs`` ledger and compares each site's recent yield to its own history.

Statuses:

    ok        recent runs are finding listings
    degraded  yield has collapsed against this site's own baseline
    broken    recent runs found nothing at all, but this site used to work
    blocked   recent runs raised (anti-bot, login wall, network)
    idle      never crawled, or not crawled recently enough to judge
    new       has history but too little to compare against

"broken" is deliberately distinguished from "idle": a site that has *never* worked
is a setup problem, while one that worked last week and yields zero today is a
regression, and they need different responses.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db.models import CrawlRun, Offer, Site

RECENT_RUNS = 5
BASELINE_RUNS = 20
DEGRADED_RATIO = 0.35     # recent yield below this fraction of baseline = degraded
STALE_AFTER_DAYS = 3


@dataclass
class SiteHealth:
    site_key: str
    site_name: str
    status: str
    recent_runs: int
    recent_avg: float
    baseline_avg: float
    failures: int
    last_run: Optional[dt.datetime]
    active_offers: int
    detail: str = ""

    @property
    def needs_attention(self) -> bool:
        return self.status in ("broken", "degraded", "blocked")


def _avg(values: list[int]) -> float:
    return sum(values) / len(values) if values else 0.0


def adapter_health(session: Session) -> list[SiteHealth]:
    offer_counts = dict(
        session.execute(
            select(Offer.site_id, func.count(Offer.id))
            .where(Offer.is_active.is_(True))
            .group_by(Offer.site_id)
        ).all()
    )
    now = dt.datetime.now(dt.timezone.utc)
    out: list[SiteHealth] = []

    for site in session.scalars(select(Site).order_by(Site.name)).all():
        runs = session.scalars(
            select(CrawlRun)
            .where(CrawlRun.site_key == site.key, CrawlRun.finished_at.is_not(None))
            # id breaks ties: runs started in the same second would otherwise
            # come back in arbitrary order, making 'recent' nondeterministic.
            .order_by(CrawlRun.started_at.desc(), CrawlRun.id.desc())
            .limit(BASELINE_RUNS)
        ).all()

        active = int(offer_counts.get(site.id, 0) or 0)
        if not runs:
            out.append(SiteHealth(site.key, site.name, "idle", 0, 0.0, 0.0, 0,
                                  None, active, "never crawled"))
            continue

        recent = runs[:RECENT_RUNS]
        baseline = runs[RECENT_RUNS:] or recent
        recent_avg = _avg([r.offers_seen or 0 for r in recent])
        baseline_avg = _avg([r.offers_seen or 0 for r in baseline])
        failures = sum(1 for r in recent if r.ok is False)
        last_run = runs[0].started_at

        age_days = (now - last_run).days if last_run else 999

        if failures >= max(2, len(recent) // 2):
            status = "blocked"
            detail = f"{failures}/{len(recent)} recent runs errored"
        elif recent_avg == 0 and baseline_avg > 0:
            status = "broken"
            detail = (f"0 listings across {len(recent)} runs; "
                      f"used to average {baseline_avg:.0f}")
        elif recent_avg == 0:
            status = "idle" if active == 0 else "broken"
            detail = "no listings found, and no history to compare against"
        elif len(runs) <= RECENT_RUNS:
            status = "new"
            detail = f"only {len(runs)} run(s) so far, averaging {recent_avg:.0f}"
        elif baseline_avg > 0 and recent_avg < baseline_avg * DEGRADED_RATIO:
            status = "degraded"
            detail = (f"averaging {recent_avg:.0f} vs a baseline of "
                      f"{baseline_avg:.0f} ({recent_avg / baseline_avg:.0%})")
        elif age_days > STALE_AFTER_DAYS:
            status = "ok"
            detail = f"last crawled {age_days}d ago"
        else:
            status = "ok"
            detail = f"averaging {recent_avg:.0f} listings per run"

        out.append(SiteHealth(site.key, site.name, status, len(recent), recent_avg,
                              baseline_avg, failures, last_run, active, detail))
    return out


def health_summary(session: Session) -> dict:
    rows = adapter_health(session)
    return {
        "sites": rows,
        "attention": [r for r in rows if r.needs_attention],
        "counts": {
            s: sum(1 for r in rows if r.status == s)
            for s in ("ok", "degraded", "broken", "blocked", "idle", "new")
        },
    }
