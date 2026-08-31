"""Price watches and notification delivery.

Checking is cheap (the price rollups are already denormalized on the product), so
this runs after every crawl rather than on its own schedule -- a watch that fires
six hours late on a listing that sold out is worthless.

Two deliberate choices:

* **Watches target products, not offers.** Sellers delist and relist constantly;
  a watch pinned to an offer id would silently stop firing.
* **Re-arming.** A watch that stays triggered would notify on every single crawl
  for as long as the price stayed low. It only fires again after the price has
  risen back above the target, so you get one alert per genuine drop.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import CanonicalProduct, Offer, Site, Watch

log = logging.getLogger(__name__)


@dataclass
class Trigger:
    watch: Watch
    product: CanonicalProduct
    price: float
    previous: Optional[float]
    site: str

    @property
    def drop(self) -> Optional[float]:
        if self.previous is None:
            return None
        return round(self.previous - self.price, 2)


def current_price(session: Session, watch: Watch) -> tuple[Optional[float], str]:
    """Best price for a watch, honouring its direct-only and landed settings."""
    stmt = (
        select(Offer, Site.name)
        .join(Site, Site.id == Offer.site_id)
        .where(Offer.canonical_id == watch.canonical_id, Offer.is_active.is_(True))
    )
    if watch.direct_only:
        stmt = stmt.where(Site.needs_agent.is_(False))

    best_price, best_site = None, ""
    for offer, site_name in session.execute(stmt).all():
        value = offer.landed_cost_usd if watch.use_landed else offer.price_usd
        if value is None or value <= 0:
            continue
        if best_price is None or value < best_price:
            best_price, best_site = value, site_name
    return best_price, best_site


def check_watches(session: Session, notify: bool = True) -> list[Trigger]:
    """Evaluate every enabled watch. Returns the ones that fired."""
    triggers: list[Trigger] = []
    now = dt.datetime.now(dt.timezone.utc)

    for watch in session.scalars(select(Watch).where(Watch.enabled.is_(True))).all():
        price, site = current_price(session, watch)
        if price is None:
            continue

        product = session.get(CanonicalProduct, watch.canonical_id)
        if product is None:
            continue

        previous = watch.last_price_usd
        if watch.baseline_usd is None:
            watch.baseline_usd = price

        target = watch.target_usd
        below = target is not None and price <= target
        # Re-arm rule: only fire on a fresh crossing, not on every crawl while the
        # price happens to sit below the target.
        was_below = previous is not None and target is not None and previous <= target

        # --- restock watch -------------------------------------------------
        if watch.on_restock:
            in_stock = any(
                o.in_stock for o in session.scalars(
                    select(Offer).where(
                        Offer.canonical_id == watch.canonical_id,
                        Offer.is_active.is_(True),
                    )
                ).all()
            )
            came_back = in_stock and watch.last_in_stock is False
            watch.last_in_stock = in_stock
            if came_back:
                watch.last_triggered_at = now
                watch.trigger_count += 1
                trigger = Trigger(watch, product, price, previous, site)
                triggers.append(trigger)
                if notify:
                    deliver(trigger)
                watch.last_price_usd = price
                continue

        watch.last_price_usd = price
        if below and not was_below:
            watch.last_triggered_at = now
            watch.trigger_count += 1
            trigger = Trigger(watch, product, price, previous, site)
            triggers.append(trigger)
            if notify:
                deliver(trigger)

    return triggers


def deliver(trigger: Trigger) -> bool:
    """POST a webhook, if the watch has one. Slack/Discord/Teams all accept this."""
    url = trigger.watch.notify_url
    if not url:
        log.info("watch hit: %s at $%.2f on %s (no webhook configured)",
                 trigger.product.title_en[:60], trigger.price, trigger.site)
        return False

    body = {
        "text": (
            f"{trigger.product.title_en[:90]} is ${trigger.price:.2f} on "
            f"{trigger.site} (target ${trigger.watch.target_usd:.2f})"
        ),
        "product": trigger.product.title_en,
        "slug": trigger.product.slug,
        "price_usd": trigger.price,
        "previous_usd": trigger.previous,
        "site": trigger.site,
        "target_usd": trigger.watch.target_usd,
    }
    try:
        import httpx

        httpx.post(url, json=body, timeout=15).raise_for_status()
        return True
    except Exception as e:
        # A dead webhook must not take the crawl down with it.
        log.warning("watch webhook failed (%s): %s", url[:60], e)
        return False
