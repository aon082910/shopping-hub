"""The crawl -> normalize -> translate -> match -> publish pipeline.

One raw listing goes through:

    RawOffer (adapter)
      -> upsert Offer            (dedupe on site + site_product_id)
      -> normalize prices to USD (FX), compute landed cost
      -> download images         (content-addressed, perceptual-hashed)
      -> translate to English    (cached; only new strings cost anything)
      -> match to a CanonicalProduct
      -> rebuild the product     (merged specs, price rollups, primary image)
      -> reindex for search

Everything is idempotent: re-running the same crawl updates rows in place, appends
one price-history point per change, and never duplicates a product.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import CrawlConfig, load_crawl_config
from ..db.models import (
    CanonicalProduct,
    CrawlRun,
    Offer,
    OfferSpec,
    OfferVariant,
    Supplier,
    PriceHistory,
    PriceTier,
    Site,
)
from ..db.search import index_product
from ..db.session import session_scope
from ..scrapers import RawOffer, SiteAdapter, get_adapter
from ..util.money import FxConverter
from ..util.text import find_gtin, normalize_gtin, normalize_spec_key, truncate
from .categories import Categorizer, recount_categories
from .images import ImageStore
from .matching import MatchEngine, rebuild_product
from .translate import Translator

log = logging.getLogger(__name__)


@dataclass
class IngestStats:
    seen: int = 0
    new: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0

    def __str__(self) -> str:
        return (
            f"seen={self.seen} new={self.new} updated={self.updated} "
            f"skipped={self.skipped} errors={self.errors}"
        )


class IngestContext:
    """Shared per-run services. Building these once per run rather than per offer
    matters: the FX table, category index and translation cache are all reused."""

    def __init__(self, session: Session, config: CrawlConfig | None = None):
        self.session = session
        self.config = config or load_crawl_config()
        self.fx = FxConverter.from_db(session)
        self.translator = Translator(session)
        self.images = ImageStore(session)
        self.matcher = MatchEngine(session, self.config)
        self.categorizer = Categorizer(session)
        self._sites: dict[str, Site] = {
            s.key: s for s in session.scalars(select(Site)).all()
        }

    def site(self, key: str) -> Site:
        site = self._sites.get(key)
        if site is None:
            raise KeyError(f"site {key!r} is not seeded; run init-db")
        return site

    def close(self) -> None:
        self.images.close()


# --------------------------------------------------------------------- one offer


def ingest_offer(ctx: IngestContext, raw: RawOffer, *, fetch_images: bool = True) -> Offer:
    session = ctx.session
    site = ctx.site(raw.site_key)

    offer = session.scalar(
        select(Offer).where(
            Offer.site_id == site.id, Offer.site_product_id == str(raw.site_product_id)
        )
    )
    is_new = offer is None
    if offer is None:
        offer = Offer(site_id=site.id, site_product_id=str(raw.site_product_id))
        session.add(offer)

    previous_price = offer.price_usd
    previous_landed = offer.landed_cost_usd

    _apply_raw(ctx, offer, raw, site)
    session.flush()  # need offer.id for specs/images

    if raw.specs:
        _replace_specs(session, offer, raw.specs)
    if raw.variants:
        _replace_variants(session, offer, raw.variants, ctx.fx)
        session.expire(offer, ["variants"])
    if raw.tiers:
        _replace_tiers(session, offer, raw.tiers, ctx.fx)
        # Tiers were inserted by offer_id, not through the relationship, so the
        # loaded collection is stale -- refresh before _recompute_costs reads it.
        session.expire(offer, ["tiers"])

    if fetch_images and raw.image_urls:
        ctx.images.ingest_many(
            raw.image_urls, offer_id=offer.id, referer=offer.url, limit=8
        )

    specs = list(session.scalars(select(OfferSpec).where(OfferSpec.offer_id == offer.id)).all())
    variants = list(
        session.scalars(select(OfferVariant).where(OfferVariant.offer_id == offer.id)).all()
    )
    ctx.translator.translate_offer(offer, specs, variants)
    for s in specs:
        s.key_norm = normalize_spec_key(s.key_en or s.key_raw)

    _harvest_identifiers(offer, specs)
    _upsert_supplier(session, offer, site)
    _recompute_costs(offer, ctx.fx)
    session.flush()

    if previous_price != offer.price_usd or previous_landed != offer.landed_cost_usd or is_new:
        session.add(
            PriceHistory(
                offer_id=offer.id,
                price_usd=offer.price_usd,
                landed_cost_usd=offer.landed_cost_usd,
                moq=offer.moq,
                in_stock=offer.in_stock,
            )
        )

    # --- attach to a canonical product ---------------------------------
    if offer.canonical_id is None:
        result = ctx.matcher.match(offer, specs)
        product = ctx.matcher.apply(offer, result)
    else:
        product = session.get(CanonicalProduct, offer.canonical_id)

    session.flush()
    if product is not None:
        rebuild_product(session, product)
        if product.category_id is None:
            category = ctx.categorizer.resolve(
                site_id=site.id,
                raw_path=offer.raw_category_path,
                title=product.title_en,
            )
            if category is not None:
                product.category_id = category.id
        session.flush()
        index_product(session, product)

    return offer


def _apply_raw(ctx: IngestContext, offer: Offer, raw: RawOffer, site: Site) -> None:
    now = dt.datetime.now(dt.timezone.utc)

    offer.url = raw.url[:1024]
    offer.title_raw = (raw.title or "")[:512]
    offer.currency = (raw.currency or site.home_currency or "USD").upper()
    # A zero or negative price is never real -- it comes from an adapter scraping
    # "$0", a "Save $0" badge, or an empty node. Left alone it would win every
    # cheapest-price comparison in the catalog and quietly poison the rollups, so
    # it is treated as "not extracted" rather than as a price.
    offer.price_min = raw.price_min if (raw.price_min or 0) > 0 else None
    offer.price_max = raw.price_max if (raw.price_max or 0) > 0 else None
    if offer.price_max is not None and offer.price_min is not None             and offer.price_max <= offer.price_min:
        offer.price_max = None
    offer.moq = max(1, int(raw.moq or 1))
    offer.moq_unit = raw.moq_unit or "piece"
    offer.lead_time_days = raw.lead_time_days

    offer.shipping_free = bool(raw.shipping_free)
    offer.shipping_from = truncate(raw.shipping_from, 80)
    offer.shipping_note_raw = truncate(raw.shipping_note, 512)
    offer.fees_note_raw = truncate(raw.fees_note, 512)
    if raw.shipping_cost is not None:
        offer.shipping_cost_usd = ctx.fx.to_usd(
            raw.shipping_cost, raw.shipping_currency or offer.currency
        )
    elif raw.shipping_free:
        offer.shipping_cost_usd = 0.0

    offer.brand = truncate(raw.brand, 120)
    offer.model = truncate(raw.model, 160)
    offer.mpn = truncate(raw.mpn, 160)
    offer.gtin = normalize_gtin(raw.gtin)

    offer.seller_name = truncate(raw.seller_name, 200)
    offer.seller_url = (raw.seller_url or None) and raw.seller_url[:1024]
    offer.seller_years = raw.seller_years
    offer.is_verified_supplier = bool(raw.is_verified_supplier)
    offer.rating = raw.rating
    offer.review_count = raw.review_count
    offer.orders_count = raw.orders_count

    if raw.description:
        offer.description_raw = raw.description[:20000]
    if raw.category_path:
        offer.raw_category_path = raw.category_path[:512]

    offer.in_stock = bool(raw.in_stock)
    offer.is_active = True
    offer.last_seen = now
    if offer.first_seen is None:
        offer.first_seen = now
    if raw.detail_fetched:
        offer.detail_fetched_at = now
    if raw.raw:
        offer.raw_payload = raw.raw


def _replace_specs(session: Session, offer: Offer, raw_specs: Sequence) -> None:
    session.execute(
        OfferSpec.__table__.delete().where(OfferSpec.offer_id == offer.id)
    )
    seen: set[str] = set()
    for spec in raw_specs:
        key = (spec.key or "").strip()
        norm = normalize_spec_key(key)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        session.add(
            OfferSpec(
                offer_id=offer.id,
                key_raw=key[:200],
                key_norm=norm[:200],
                value_raw=(spec.value or "")[:1000],
                position=spec.position,
            )
        )


def _replace_tiers(session: Session, offer: Offer, raw_tiers: Sequence, fx: FxConverter) -> None:
    session.execute(PriceTier.__table__.delete().where(PriceTier.offer_id == offer.id))
    for tier in sorted(raw_tiers, key=lambda t: t.min_qty):
        session.add(
            PriceTier(
                offer_id=offer.id,
                min_qty=tier.min_qty,
                max_qty=tier.max_qty,
                price=tier.price,
                currency=tier.currency,
                price_usd=fx.to_usd(tier.price, tier.currency),
            )
        )


def _replace_variants(session: Session, offer: Offer, raw_variants, fx: FxConverter) -> None:
    session.execute(OfferVariant.__table__.delete().where(OfferVariant.offer_id == offer.id))
    seen: set[str] = set()
    for pos, v in enumerate(raw_variants):
        sku = (v.sku or v.name or "").strip()[:160]
        if not sku or sku in seen:
            continue
        seen.add(sku)
        currency = (v.currency or offer.currency or "USD").upper()
        session.add(
            OfferVariant(
                offer_id=offer.id,
                sku=sku,
                name_raw=(v.name or sku)[:300],
                attrs=dict(v.attrs or {}),
                price=v.price,
                currency=currency,
                price_usd=fx.to_usd(v.price, currency),
                stock=v.stock,
                in_stock=bool(v.in_stock),
                image_url=(v.image_url or None) and v.image_url[:1024],
                position=pos,
            )
        )
    offer.variant_count = len(seen)


def _upsert_supplier(session: Session, offer: Offer, site: Site) -> None:
    """Attach the offer to a Supplier row, creating it on first sight."""
    from ..util.text import clean

    name = clean(offer.seller_name or "")
    if not name:
        offer.supplier_id = None
        return

    norm = name.lower().strip()[:200]
    supplier = session.scalar(
        select(Supplier).where(Supplier.site_id == site.id, Supplier.name_norm == norm)
    )
    now = dt.datetime.now(dt.timezone.utc)
    if supplier is None:
        supplier = Supplier(
            site_id=site.id, name=name[:200], name_norm=norm,
            url=offer.seller_url, first_seen=now, last_seen=now,
        )
        session.add(supplier)
        session.flush()
    else:
        supplier.last_seen = now
        supplier.url = supplier.url or offer.seller_url

    # Best-known values win: a listing that omits the rating should not erase one
    # recorded from a listing that had it.
    supplier.years_active = offer.seller_years or supplier.years_active
    supplier.rating = offer.rating if offer.rating is not None else supplier.rating
    supplier.is_verified = supplier.is_verified or bool(offer.is_verified_supplier)
    supplier.country = supplier.country or offer.shipping_from
    offer.supplier_id = supplier.id


def _recount_suppliers(session: Session) -> None:
    from sqlalchemy import func as sa_func

    counts = dict(
        session.execute(
            select(Offer.supplier_id, sa_func.count(Offer.id))
            .where(Offer.supplier_id.is_not(None), Offer.is_active.is_(True))
            .group_by(Offer.supplier_id)
        ).all()
    )
    for supplier in session.scalars(select(Supplier)).all():
        supplier.offer_count = int(counts.get(supplier.id, 0) or 0)


def _harvest_identifiers(offer: Offer, specs: Iterable[OfferSpec]) -> None:
    """Pull brand/model/MPN/GTIN out of the spec table when the card didn't have them."""
    for s in specs:
        key = (s.key_norm or "").lower()
        val = (s.value_en or s.value_raw or "").strip()
        if not val:
            continue
        if key == "brand" and not offer.brand:
            offer.brand = val[:120]
        elif key == "model" and not offer.model:
            offer.model = val[:160]
            offer.mpn = offer.mpn or val[:160]
        elif key in ("upc", "ean", "gtin", "barcode") and not offer.gtin:
            offer.gtin = normalize_gtin(val)

    if not offer.gtin:
        offer.gtin = find_gtin(offer.title_en or offer.title_raw)


def _recompute_costs(offer: Offer, fx: FxConverter) -> None:
    """Unit price in USD, plus the landed cost of one minimum order.

    ``landed_cost_usd`` is the honest comparison number: a $0.90 item at MOQ 500
    is a $450 purchase, which is not competitive with a $6 single unit no matter
    what the unit price says.
    """
    offer.fx_rate = fx.rate_for(offer.currency)
    unit = offer.price_min

    # A listing with SKUs has no single price. Take the cheapest purchasable variant
    # as the headline and the dearest as the ceiling -- that is what the site itself
    # advertises, and it keeps the comparison honest about the spread within a listing.
    variant_prices = [v.price for v in offer.variants if v.price and v.in_stock]
    if variant_prices:
        unit = min(variant_prices)
        offer.price_min = unit
        offer.price_max = max(variant_prices) if len(set(variant_prices)) > 1 else None

    # If there's a tier ladder, the price you actually pay at MOQ is the tier
    # covering MOQ -- not the headline "as low as" price.
    if offer.tiers:
        applicable = [t for t in offer.tiers if t.min_qty <= (offer.moq or 1)]
        chosen = max(applicable, key=lambda t: t.min_qty) if applicable else offer.tiers[0]
        unit = chosen.price
        offer.currency = chosen.currency or offer.currency

    offer.price_usd = fx.to_usd(unit, offer.currency)

    if offer.price_usd is None:
        offer.landed_cost_usd = None
        return

    subtotal = offer.price_usd * max(1, offer.moq or 1)
    shipping = offer.shipping_cost_usd if offer.shipping_cost_usd is not None else 0.0

    # Duty is only included when you have configured a rate table (duty.yaml).
    # Off, duty_usd stays None and the UI says duty is excluded -- which is honest,
    # unlike quietly assuming zero.
    from ..duty import load_duty_table

    rate, duty = load_duty_table().estimate(subtotal, offer.raw_category_path)
    offer.duty_rate, offer.duty_usd = rate, duty
    offer.landed_cost_usd = round(subtotal + shipping + (duty or 0.0), 4)


def _batched(iterable, size: int):
    """Yield lists of at most ``size`` items from an iterator."""
    batch: list = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# ------------------------------------------------------------ detail concurrency


def _fetch_details_parallel(
    adapters: list[SiteAdapter], jobs: list[RawOffer]
) -> list[RawOffer]:
    """Fetch product pages for a batch of offers concurrently.

    Only the *network* half is parallelized. Ingest keeps running on the calling
    thread against one session, because SQLAlchemy sessions are not thread-safe and
    SQLite takes one writer at a time -- parallel writes would buy nothing but lock
    contention.

    Each worker gets its own adapter (hence its own HTTP session): ``curl_cffi``
    sessions are not documented as thread-safe. Politeness is unaffected because the
    rate limiter in ``util.http`` is process-global and keyed by host, so N workers
    still start at most one request per ``delay_seconds`` against a site. The gain is
    that response latency overlaps the wait instead of adding to it.

    Returns offers in completion order; failures come back unenriched rather than
    dropped, so a dead product page costs its listing data but not the listing.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not jobs:
        return []
    if len(adapters) == 1:
        return [_safe_detail(adapters[0], job) for job in jobs]

    out: list[RawOffer] = []
    with ThreadPoolExecutor(max_workers=len(adapters)) as pool:
        futures = {
            pool.submit(_safe_detail, adapters[i % len(adapters)], job): job
            for i, job in enumerate(jobs)
        }
        for future in as_completed(futures):
            try:
                out.append(future.result())
            except Exception as e:  # pragma: no cover - _safe_detail swallows
                log.debug("detail worker raised: %s", e)
                out.append(futures[future])
    return out


def _safe_detail(adapter: SiteAdapter, raw: RawOffer) -> RawOffer:
    try:
        return adapter.fetch_detail(raw)
    except Exception as e:
        log.debug("[%s] detail failed for %s: %s", raw.site_key, raw.site_product_id, e)
        return raw


# ------------------------------------------------------------------- crawl loop


def crawl_site(
    site_key: str,
    keywords: Sequence[str] | None = None,
    *,
    max_pages: int | None = None,
    fetch_details: bool = True,
    detail_limit: int | None = None,
    config: CrawlConfig | None = None,
) -> IngestStats:
    """Search one site for each keyword and ingest everything found."""
    cfg = config or load_crawl_config()
    keywords = list(keywords or cfg.keywords)
    stats = IngestStats()

    site_cfg = cfg.site(site_key)
    workers = max(1, int(site_cfg.get("concurrency", 1)))

    # Playwright's sync API is thread-bound: a browser session belongs to the thread
    # that created it and cannot be driven from a worker thread. Any site that
    # renders pages in a browser therefore has to enrich serially. No real loss --
    # a browser page load dwarfs the per-host delay that concurrency exists to hide.
    renders_in_browser = (
        str(site_cfg.get("render", "http")).lower() == "browser"
        or str(site_cfg.get("driver", "")).lower() in ("browser", "hybrid")
        or str(site_cfg.get("search_driver", "")).lower() == "browser"
        or str(site_cfg.get("detail_driver", "")).lower() == "browser"
    )
    if renders_in_browser and workers > 1:
        log.info("[%s] browser rendering forces serial detail fetches "
                 "(concurrency %s ignored)", site_key, workers)
        workers = 1

    batch_size = max(1, workers * 4)

    adapter: SiteAdapter = get_adapter(site_key, cfg)
    # Extra adapters only exist to give each detail worker its own HTTP session.
    detail_adapters = [adapter] + [
        get_adapter(site_key, cfg) for _ in range(workers - 1)
    ] if fetch_details else [adapter]

    try:
        for keyword in keywords:
            run_id = _start_run(site_key, "search", keyword)
            kw_stats = IngestStats()
            try:
                with session_scope() as session:
                    ctx = IngestContext(session, cfg)
                    try:
                        details_done = 0
                        for batch in _batched(adapter.search(keyword, max_pages), batch_size):
                            kw_stats.seen += len(batch)

                            # Deciding what needs enriching touches the DB, so it
                            # happens here on the owning thread, before any fan-out.
                            to_fetch, passthrough = [], []
                            for raw in batch:
                                wants = (
                                    fetch_details
                                    and (detail_limit is None or details_done < detail_limit)
                                    and _needs_detail(session, ctx, raw)
                                )
                                if wants:
                                    to_fetch.append(raw)
                                    details_done += 1
                                else:
                                    passthrough.append(raw)

                            enriched = _fetch_details_parallel(detail_adapters, to_fetch)

                            for raw in enriched + passthrough:
                                try:
                                    existed = _offer_exists(session, ctx, raw)
                                    ingest_offer(ctx, raw)
                                    if existed:
                                        kw_stats.updated += 1
                                    else:
                                        kw_stats.new += 1
                                except Exception as e:
                                    kw_stats.errors += 1
                                    log.warning(
                                        "[%s] failed to ingest %s: %s",
                                        site_key, raw.site_product_id, e,
                                        exc_info=log.isEnabledFor(10),
                                    )
                            # Commit per batch so a late crash doesn't lose the run.
                            session.commit()
                    finally:
                        ctx.close()
                _finish_run(run_id, True, kw_stats)
            except Exception as e:
                log.error("[%s] crawl failed for %r: %s", site_key, keyword, e)
                _finish_run(run_id, False, kw_stats, str(e))

            log.info("[%s] %r -> %s", site_key, keyword, kw_stats)
            for field in ("seen", "new", "updated", "skipped", "errors"):
                setattr(stats, field, getattr(stats, field) + getattr(kw_stats, field))
    finally:
        for a in detail_adapters:
            a.close()

    with session_scope() as session:
        recount_categories(session)
        _recount_suppliers(session)

    return stats


def crawl_all(
    site_keys: Sequence[str] | None = None,
    keywords: Sequence[str] | None = None,
    *,
    max_pages: int | None = None,
    fetch_details: bool = True,
    detail_limit: int | None = None,
) -> dict[str, IngestStats]:
    cfg = load_crawl_config()
    keys = list(site_keys or cfg.enabled_sites())
    out: dict[str, IngestStats] = {}
    for key in keys:
        log.info("=== crawling %s ===", key)
        try:
            out[key] = crawl_site(
                key, keywords, max_pages=max_pages,
                fetch_details=fetch_details, detail_limit=detail_limit, config=cfg,
            )
        except Exception as e:
            log.error("site %s failed entirely: %s", key, e)
            out[key] = IngestStats(errors=1)
    return out


def refresh_prices(
    site_keys: Sequence[str] | None = None,
    *,
    older_than_hours: int = 12,
    limit: int = 500,
) -> IngestStats:
    """Re-fetch known offers to update prices without re-running discovery.

    Much cheaper than a full crawl and safe to run every few hours.
    """
    cfg = load_crawl_config()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=older_than_hours)
    stats = IngestStats()

    with session_scope() as session:
        query = (
            select(Offer, Site.key)
            .join(Site, Site.id == Offer.site_id)
            .where(Offer.is_active.is_(True), Offer.last_seen < cutoff)
            .order_by(Offer.last_seen)
            .limit(limit)
        )
        if site_keys:
            query = query.where(Site.key.in_(list(site_keys)))
        targets = [(o.id, o.url, o.site_product_id, key) for o, key in session.execute(query).all()]

    by_site: dict[str, list[tuple]] = {}
    for offer_id, url, pid, key in targets:
        by_site.setdefault(key, []).append((offer_id, url, pid))

    for site_key, rows in by_site.items():
        adapter = get_adapter(site_key, cfg)
        try:
            with session_scope() as session:
                ctx = IngestContext(session, cfg)
                try:
                    for offer_id, url, pid in rows:
                        stats.seen += 1
                        try:
                            raw = RawOffer(
                                site_key=site_key, site_product_id=pid, url=url, title=""
                            )
                            raw = adapter.fetch_detail(raw)
                            if not raw.title:
                                existing = session.get(Offer, offer_id)
                                raw.title = existing.title_raw if existing else ""
                            ingest_offer(ctx, raw, fetch_images=False)
                            stats.updated += 1
                        except Exception as e:
                            stats.errors += 1
                            log.debug("[%s] refresh failed for %s: %s", site_key, pid, e)
                        if stats.seen % 25 == 0:
                            session.commit()
                finally:
                    ctx.close()
        finally:
            adapter.close()

    return stats


def deactivate_stale(days: int = 30) -> int:
    """Mark offers not seen in ``days`` as inactive and refresh their products."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    with session_scope() as session:
        stale = session.scalars(
            select(Offer).where(Offer.is_active.is_(True), Offer.last_seen < cutoff)
        ).all()
        affected = {o.canonical_id for o in stale if o.canonical_id}
        session.execute(
            update(Offer)
            .where(Offer.is_active.is_(True), Offer.last_seen < cutoff)
            .values(is_active=False)
        )
        session.flush()
        for cid in affected:
            product = session.get(CanonicalProduct, cid)
            if product is not None:
                rebuild_product(session, product)
                index_product(session, product)
        recount_categories(session)
        return len(stale)


# ------------------------------------------------------------------- internals


def _needs_detail(session: Session, ctx: IngestContext, raw: RawOffer) -> bool:
    """Skip the expensive detail fetch for offers we already enriched recently."""
    site = ctx.site(raw.site_key)
    row = session.execute(
        select(Offer.detail_fetched_at).where(
            Offer.site_id == site.id, Offer.site_product_id == str(raw.site_product_id)
        )
    ).first()
    if row is None or row[0] is None:
        return True
    max_age = float(ctx.config.site(raw.site_key).get("detail_refresh_days", 7))
    age = dt.datetime.now(dt.timezone.utc) - _aware(row[0])
    return age > dt.timedelta(days=max_age)


def _offer_exists(session: Session, ctx: IngestContext, raw: RawOffer) -> bool:
    site = ctx.site(raw.site_key)
    return bool(
        session.scalar(
            select(Offer.id).where(
                Offer.site_id == site.id, Offer.site_product_id == str(raw.site_product_id)
            )
        )
    )


def _aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def _start_run(site_key: str, mode: str, query: str | None) -> int:
    with session_scope() as session:
        run = CrawlRun(site_key=site_key, mode=mode, query=query)
        session.add(run)
        session.flush()
        return run.id


def _finish_run(
    run_id: int, ok: bool, stats: IngestStats, error: str | None = None
) -> None:
    with session_scope() as session:
        run = session.get(CrawlRun, run_id)
        if run is None:
            return
        run.finished_at = dt.datetime.now(dt.timezone.utc)
        run.ok = ok
        run.offers_seen = stats.seen
        run.offers_new = stats.new
        run.offers_updated = stats.updated
        run.error = (error or None) and error[:4000]
