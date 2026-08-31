"""FastAPI app: browse UI + JSON API.

Pages
    /                       home - category grid, newest products, catalog stats
    /search?q=...           full-text search across the whole catalog, with filters
    /category/{path}        browse a category or subcategory (materialized path)
    /product/{slug}         the item page: unified specs + every site's price
    /admin                  crawl history and the match review queue

JSON (same data, for scripting)
    /api/search  /api/product/{slug}  /api/categories  /api/stats  /api/suggest
"""

from __future__ import annotations

import datetime as dt
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..agents import agent_notice, build_agent_links, estimate_agent_total
from ..config import get_settings
from ..duty import load_duty_table
from ..pipeline.breakeven import analyse
from ..pipeline.freight import from_specs as spec_freight
from ..pipeline.trust import assess_offers
from ..db.models import (
    CanonicalProduct,
    Category,
    CrawlRun,
    Image,
    MatchRejection,
    MatchReview,
    Offer,
    OfferSpec,
    OfferVariant,
    PriceHistory,
    PriceTier,
    Site,
    Supplier,
)
from ..db.search import drop_product, index_product, search_product_ids, suggest
from .security import require_admin, require_same_origin
from ..db.session import get_session, init_db
from ..pipeline.matching import (
    clear_rejections,
    detach_offer,
    rebuild_product,
    record_rejection,
)

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"

PAGE_SIZE = 36


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="SourceHub",
    description="Cross-marketplace product comparison",
    lifespan=lifespan,
)
templates = Jinja2Templates(directory=str(WEB / "templates"))

# Mounted at import time: media_path creates the directory as a side effect, so
# this is safe on a first run with no data.
(WEB / "static").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(WEB / "static")), name="static")
app.mount("/media", StaticFiles(directory=str(get_settings().media_path)), name="media")


def db() -> Session:
    session = get_session()
    try:
        yield session
    finally:
        session.close()


# ------------------------------------------------------------------ view models


def product_card(session: Session, p: CanonicalProduct) -> dict:
    img = session.get(Image, p.primary_image_id) if p.primary_image_id else None
    best_site = session.get(Site, p.best_price_site_id) if p.best_price_site_id else None
    return {
        "id": p.id,
        "slug": p.slug,
        "title": p.title_en,
        "brand": p.brand,
        "image": f"/media/{img.thumb_path}" if img and img.thumb_path else None,
        "best_price_usd": p.best_price_usd,
        "max_price_usd": p.max_price_usd,
        "best_landed_usd": p.best_landed_usd,
        "best_site": best_site.name if best_site else None,
        "offer_count": p.offer_count,
        "site_count": p.site_count,
        "min_moq": p.min_moq,
        "savings_pct": _savings_pct(p),
        "category": p.category.name if p.category else None,
        "category_path": p.category.path if p.category else None,
        "last_seen": p.last_seen,
    }


def _savings_pct(p: CanonicalProduct) -> Optional[int]:
    if not p.best_price_usd or not p.max_price_usd or p.max_price_usd <= p.best_price_usd:
        return None
    return int(round(100 * (1 - p.best_price_usd / p.max_price_usd)))


def offer_view(session: Session, o: Offer) -> dict:
    site = session.get(Site, o.site_id)
    tiers = session.scalars(
        select(PriceTier).where(PriceTier.offer_id == o.id).order_by(PriceTier.min_qty)
    ).all()
    variants = session.scalars(
        select(OfferVariant)
        .where(OfferVariant.offer_id == o.id)
        .order_by(OfferVariant.price_usd.is_(None), OfferVariant.price_usd)
    ).all()

    # Freight is priced on weight and volume, so pull whatever the specs disclose.
    spec_map = {
        (sp.key_en or sp.key_raw): (sp.value_en or sp.value_raw)
        for sp in session.scalars(
            select(OfferSpec).where(OfferSpec.offer_id == o.id)
        ).all()
    }
    weight_kg, dims_cm = spec_freight(spec_map)
    return {
        "id": o.id,
        "site_key": site.key if site else "",
        "site_name": site.name if site else "",
        "site_needs_agent": bool(site and site.needs_agent),
        "site_is_wholesale": bool(site and site.is_wholesale),
        "site_is_baseline": bool(site and site.is_baseline),
        "url": o.url,
        "title": o.title_en or o.title_raw,
        "title_raw": o.title_raw,
        "was_translated": bool(o.title_en and o.title_en != o.title_raw),
        "price_usd": o.price_usd,
        "price_native": o.price_min,
        "price_native_max": o.price_max,
        "currency": o.currency,
        "moq": o.moq,
        "moq_unit": o.moq_unit,
        "shipping_cost_usd": o.shipping_cost_usd,
        "shipping_free": o.shipping_free,
        "shipping_from": o.shipping_from,
        "shipping_note": o.shipping_note_en or o.shipping_note_raw,
        "fees_note": o.fees_note_en or o.fees_note_raw,
        "landed_cost_usd": o.landed_cost_usd,
        "duty_rate": o.duty_rate,
        "duty_usd": o.duty_usd,
        "supplier_id": o.supplier_id,
        "lead_time_days": o.lead_time_days,
        "seller_name": o.seller_name,
        "seller_url": o.seller_url,
        "seller_years": o.seller_years,
        "verified": o.is_verified_supplier,
        "rating": o.rating,
        "review_count": o.review_count,
        "orders_count": o.orders_count,
        "in_stock": o.in_stock,
        "last_seen": o.last_seen,
        "match_score": o.match_score,
        "tiers": [
            {
                "min_qty": t.min_qty,
                "max_qty": t.max_qty,
                "price": t.price,
                "currency": t.currency,
                "price_usd": t.price_usd,
            }
            for t in tiers
        ],
        "variants": [
            {
                "sku": v.sku,
                "name": v.name_en or v.name_raw,
                "price_usd": v.price_usd,
                "price": v.price,
                "currency": v.currency,
                "in_stock": v.in_stock,
                "stock": v.stock,
            }
            for v in variants
        ],
        "variant_count": len(variants),
        "agent_links": [vars(a) for a in build_agent_links(session, o)],
        "agent_notice": agent_notice(session, o),
        "agent_estimate": (
            estimate_agent_total(
                o.price_usd, o.moq,
                weight_kg=weight_kg, dims_cm=dims_cm,
                category_path=o.raw_category_path,
            )
            if site and site.needs_agent
            else None
        ),
    }


def category_tree(session: Session) -> list[dict]:
    cats = session.scalars(
        select(Category).order_by(Category.level, Category.sort_order, Category.name)
    ).all()
    tops = [c for c in cats if c.parent_id is None]
    by_parent: dict[int, list[Category]] = {}
    for c in cats:
        if c.parent_id:
            by_parent.setdefault(c.parent_id, []).append(c)
    return [
        {
            "name": t.name,
            "slug": t.slug,
            "path": t.path,
            "icon": t.icon,
            "count": t.product_count,
            "children": [
                {"name": c.name, "path": c.path, "count": c.product_count}
                for c in by_parent.get(t.id, [])
            ],
        }
        for t in tops
    ]


# ------------------------------------------------------------------- query core


def query_products(
    session: Session,
    *,
    q: str = "",
    category_path: str = "",
    site_key: str = "",
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    max_moq: Optional[int] = None,
    direct_only: bool = False,
    brand: str = "",
    sort: str = "relevance",
    page: int = 1,
) -> tuple[list[CanonicalProduct], int]:
    """Search + filter + sort. Returns (page of products, total matches)."""
    stmt = select(CanonicalProduct).where(CanonicalProduct.is_active.is_(True))
    count_stmt = select(func.count(CanonicalProduct.id)).where(
        CanonicalProduct.is_active.is_(True)
    )

    ranked_ids: list[int] = []
    if q:
        # Pull a generous candidate set from FTS, then apply structured filters in
        # SQL -- filtering inside the FTS query would break BM25 ranking.
        hits = search_product_ids(session, q, limit=2000)
        ranked_ids = [pid for pid, _ in hits]
        if not ranked_ids:
            return [], 0
        stmt = stmt.where(CanonicalProduct.id.in_(ranked_ids))
        count_stmt = count_stmt.where(CanonicalProduct.id.in_(ranked_ids))

    if category_path:
        cat_filter = or_(
            Category.path == category_path,
            Category.path.like(f"{category_path}/%"),
        )
        sub = select(Category.id).where(cat_filter)
        stmt = stmt.where(CanonicalProduct.category_id.in_(sub))
        count_stmt = count_stmt.where(CanonicalProduct.category_id.in_(sub))

    if site_key or direct_only or max_moq is not None:
        offer_q = select(Offer.canonical_id).join(Site, Site.id == Offer.site_id).where(
            Offer.is_active.is_(True)
        )
        if site_key:
            offer_q = offer_q.where(Site.key == site_key)
        if direct_only:
            offer_q = offer_q.where(Site.needs_agent.is_(False))
        if max_moq is not None:
            offer_q = offer_q.where(Offer.moq <= max_moq)
        stmt = stmt.where(CanonicalProduct.id.in_(offer_q))
        count_stmt = count_stmt.where(CanonicalProduct.id.in_(offer_q))

    if brand:
        stmt = stmt.where(func.lower(CanonicalProduct.brand) == brand.lower())
        count_stmt = count_stmt.where(func.lower(CanonicalProduct.brand) == brand.lower())

    if min_price is not None:
        stmt = stmt.where(CanonicalProduct.best_price_usd >= min_price)
        count_stmt = count_stmt.where(CanonicalProduct.best_price_usd >= min_price)
    if max_price is not None:
        stmt = stmt.where(CanonicalProduct.best_price_usd <= max_price)
        count_stmt = count_stmt.where(CanonicalProduct.best_price_usd <= max_price)

    total = int(session.scalar(count_stmt) or 0)

    if sort == "price_asc":
        stmt = stmt.order_by(CanonicalProduct.best_price_usd.is_(None), CanonicalProduct.best_price_usd.asc())
    elif sort == "price_desc":
        stmt = stmt.order_by(CanonicalProduct.best_price_usd.desc().nullslast())
    elif sort == "newest":
        stmt = stmt.order_by(CanonicalProduct.first_seen.desc())
    elif sort == "sites":
        stmt = stmt.order_by(CanonicalProduct.site_count.desc(), CanonicalProduct.offer_count.desc())
    elif sort == "savings":
        stmt = stmt.order_by(
            (CanonicalProduct.max_price_usd - CanonicalProduct.best_price_usd).desc().nullslast()
        )
    elif q and sort == "relevance":
        pass  # ordered in Python below to preserve BM25 rank
    else:
        stmt = stmt.order_by(CanonicalProduct.offer_count.desc(), CanonicalProduct.id.desc())

    offset = (max(1, page) - 1) * PAGE_SIZE

    if q and sort == "relevance":
        rows = list(session.scalars(stmt.options(selectinload(CanonicalProduct.category))).all())
        order = {pid: i for i, pid in enumerate(ranked_ids)}
        rows.sort(key=lambda p: order.get(p.id, 10**9))
        return rows[offset : offset + PAGE_SIZE], total

    rows = list(
        session.scalars(
            stmt.options(selectinload(CanonicalProduct.category))
            .limit(PAGE_SIZE)
            .offset(offset)
        ).all()
    )
    return rows, total


PRICE_BUCKETS = [
    ("under $5", None, 5.0), ("$5-$20", 5.0, 20.0), ("$20-$50", 20.0, 50.0),
    ("$50-$200", 50.0, 200.0), ("over $200", 200.0, None),
]


def compute_facets(session: Session, product_ids: list[int]) -> dict:
    """Counts for the current result set, so filters show what they would yield.

    Computed over the matched ids rather than the whole catalog -- a facet count
    that ignores the active query tells you nothing about the choice in front of you.
    Capped, because faceting a hundred-thousand-row result set on every keystroke is
    not worth the latency.
    """
    if not product_ids:
        return {"brands": [], "sites": [], "prices": []}
    ids = product_ids[:5000]

    brands = session.execute(
        select(CanonicalProduct.brand, func.count(CanonicalProduct.id))
        .where(CanonicalProduct.id.in_(ids), CanonicalProduct.brand.is_not(None))
        .group_by(CanonicalProduct.brand)
        .order_by(func.count(CanonicalProduct.id).desc())
        .limit(12)
    ).all()

    sites = session.execute(
        select(Site.key, Site.name, func.count(func.distinct(Offer.canonical_id)))
        .join(Offer, Offer.site_id == Site.id)
        .where(Offer.canonical_id.in_(ids), Offer.is_active.is_(True))
        .group_by(Site.id)
        .order_by(func.count(func.distinct(Offer.canonical_id)).desc())
    ).all()

    prices = []
    for label, lo, hi in PRICE_BUCKETS:
        stmt = select(func.count(CanonicalProduct.id)).where(
            CanonicalProduct.id.in_(ids), CanonicalProduct.best_price_usd.is_not(None)
        )
        if lo is not None:
            stmt = stmt.where(CanonicalProduct.best_price_usd >= lo)
        if hi is not None:
            stmt = stmt.where(CanonicalProduct.best_price_usd < hi)
        count = int(session.scalar(stmt) or 0)
        if count:
            prices.append({"label": label, "min": lo, "max": hi, "count": count})

    return {
        "brands": [{"name": b, "count": int(c)} for b, c in brands if b],
        "sites": [{"key": k, "name": n, "count": int(c)} for k, n, c in sites],
        "prices": prices,
    }


def _filters(request: Request) -> dict:
    qp = request.query_params
    return {
        "q": qp.get("q", "").strip(),
        "site": qp.get("site", ""),
        "sort": qp.get("sort", "relevance"),
        "min_price": qp.get("min_price", ""),
        "max_price": qp.get("max_price", ""),
        "max_moq": qp.get("max_moq", ""),
        "direct_only": qp.get("direct_only", "") in ("1", "true", "on"),
        "brand": qp.get("brand", ""),
    }


def _as_float(v: str) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v: str) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------- pages


@app.get("/", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(db)):
    newest = session.scalars(
        select(CanonicalProduct)
        .where(CanonicalProduct.is_active.is_(True))
        .order_by(CanonicalProduct.first_seen.desc())
        .limit(12)
    ).all()
    most_compared = session.scalars(
        select(CanonicalProduct)
        .where(CanonicalProduct.is_active.is_(True), CanonicalProduct.site_count > 1)
        .order_by(CanonicalProduct.site_count.desc(), CanonicalProduct.offer_count.desc())
        .limit(12)
    ).all()
    # Note: request-first is the current Starlette signature. The legacy
    # TemplateResponse(name, context) form was removed in Starlette 1.6.
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "categories": category_tree(session),
            "newest": [product_card(session, p) for p in newest],
            "most_compared": [product_card(session, p) for p in most_compared],
            "stats": _stats(session),
            "sites": _site_rows(session),
            "filters": _filters(request),
        },
    )


@app.get("/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    q: str = "",
    page: int = 1,
    session: Session = Depends(db),
):
    f = _filters(request)
    products, total = query_products(
        session,
        q=q,
        site_key=f["site"],
        min_price=_as_float(f["min_price"]),
        max_price=_as_float(f["max_price"]),
        max_moq=_as_int(f["max_moq"]),
        direct_only=f["direct_only"],
        brand=f["brand"],
        sort=f["sort"],
        page=page,
    )
    facets = compute_facets(session, [p.id for p in products])
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "q": q,
            "products": [product_card(session, p) for p in products],
            "total": total,
            "page": page,
            "pages": max(1, -(-total // PAGE_SIZE)),
            "categories": category_tree(session),
            "sites": _site_rows(session),
            "filters": f,
            "facets": facets,
            "heading": f'Search results for "{q}"' if q else "All products",
            "category": None,
        },
    )


@app.get("/category/{path:path}", response_class=HTMLResponse)
def category_page(
    request: Request, path: str, page: int = 1, session: Session = Depends(db)
):
    category = session.scalar(select(Category).where(Category.path == path))
    if category is None:
        raise HTTPException(404, f"No such category: {path}")

    f = _filters(request)
    products, total = query_products(
        session,
        q=f["q"],
        category_path=path,
        site_key=f["site"],
        min_price=_as_float(f["min_price"]),
        max_price=_as_float(f["max_price"]),
        max_moq=_as_int(f["max_moq"]),
        direct_only=f["direct_only"],
        brand=f["brand"],
        sort=f["sort"] if f["sort"] != "relevance" else "sites",
        page=page,
    )
    facets = compute_facets(session, [p.id for p in products])

    children = session.scalars(
        select(Category).where(Category.parent_id == category.id).order_by(Category.sort_order)
    ).all()
    ancestors = []
    node = category
    while node.parent_id:
        node = session.get(Category, node.parent_id)
        if node is None:
            break
        ancestors.insert(0, {"name": node.name, "path": node.path})

    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "q": f["q"],
            "products": [product_card(session, p) for p in products],
            "total": total,
            "page": page,
            "pages": max(1, -(-total // PAGE_SIZE)),
            "categories": category_tree(session),
            "sites": _site_rows(session),
            "filters": f,
            "facets": facets,
            "heading": category.name,
            "category": {
                "name": category.name,
                "path": category.path,
                "children": [
                    {"name": c.name, "path": c.path, "count": c.product_count} for c in children
                ],
                "ancestors": ancestors,
            },
        },
    )


@app.get("/product/{slug}", response_class=HTMLResponse)
def product_page(request: Request, slug: str, session: Session = Depends(db)):
    product = session.scalar(select(CanonicalProduct).where(CanonicalProduct.slug == slug))
    if product is None:
        raise HTTPException(404, "Product not found")

    offers = session.scalars(
        select(Offer)
        .where(Offer.canonical_id == product.id, Offer.is_active.is_(True))
        .order_by(Offer.price_usd.is_(None), Offer.price_usd.asc())
    ).all()

    images = session.scalars(
        select(Image)
        .join(Offer, Offer.id == Image.offer_id)
        .where(Offer.canonical_id == product.id, Image.thumb_path.is_not(None))
        .order_by((Image.width * Image.height).desc())
        .limit(10)
    ).all()
    # De-dupe the gallery by content hash -- resellers share the same photo.
    gallery, seen = [], set()
    for img in images:
        if img.sha256 in seen:
            continue
        seen.add(img.sha256)
        gallery.append({"full": f"/media/{img.local_path}", "thumb": f"/media/{img.thumb_path}"})

    offer_views = [offer_view(session, o) for o in offers]
    for view in offer_views:
        view["can_split"] = len(offers) > 1
    priced = [o for o in offer_views if o["price_usd"]]

    history = session.execute(
        select(PriceHistory.ts, func.min(PriceHistory.price_usd))
        .join(Offer, Offer.id == PriceHistory.offer_id)
        .where(Offer.canonical_id == product.id, PriceHistory.price_usd.is_not(None))
        .group_by(func.date(PriceHistory.ts))
        .order_by(PriceHistory.ts)
        .limit(180)
    ).all()

    ancestors = []
    if product.category:
        node = product.category
        ancestors = [{"name": node.name, "path": node.path}]
        while node.parent_id:
            node = session.get(Category, node.parent_id)
            if node is None:
                break
            ancestors.insert(0, {"name": node.name, "path": node.path})

    return templates.TemplateResponse(
        request,
        "product.html",
        {
            "product": {
                "id": product.id,
                "slug": product.slug,
                "title": product.title_en,
                "brand": product.brand,
                "model": product.model,
                "mpn": product.mpn,
                "gtin": product.gtin,
                "description": product.description_en,
                "specs": product.specs or {},
                "best_price_usd": product.best_price_usd,
                "max_price_usd": product.max_price_usd,
                "best_landed_usd": product.best_landed_usd,
                "min_moq": product.min_moq,
                "offer_count": product.offer_count,
                "site_count": product.site_count,
                "savings_pct": _savings_pct(product),
                "first_seen": product.first_seen,
                "last_seen": product.last_seen,
            },
            "offers": offer_views,
            "cheapest_unit": min(priced, key=lambda o: o["price_usd"]) if priced else None,
            "cheapest_landed": (
                min(
                    (o for o in offer_views if o["landed_cost_usd"]),
                    key=lambda o: o["landed_cost_usd"],
                    default=None,
                )
            ),
            "gallery": gallery,
            "categories": category_tree(session),
            "breadcrumbs": ancestors,
            "history": [{"date": str(ts)[:10], "price": price} for ts, price in history],
            "duty": load_duty_table(),
            "economics": analyse(offer_views),
            "trust": {k: vars(v) for k, v in assess_offers(offer_views).items()},
            "filters": _filters(request),
        },
    )


MAX_UPLOAD_BYTES = 12 * 1024 * 1024


@app.get("/search/image", response_class=HTMLResponse)
def image_search_form(request: Request, session: Session = Depends(db)):
    return templates.TemplateResponse(
        request, "image_search.html",
        {"categories": category_tree(session), "filters": _filters(request),
         "hits": None, "error": None, "query_image": None},
    )


@app.post("/search/image", response_class=HTMLResponse)
async def image_search(
    request: Request,
    file: UploadFile = File(None),
    image_url: str = Form(""),
    session: Session = Depends(db),
):
    """Find products by photograph.

    Works because these marketplaces reuse one supplier photograph across every
    reseller, so an identical hash usually means an identical product.
    """
    from ..pipeline.imagesearch import UndecodableImage, find_by_bytes, find_by_url

    hits, error = None, None
    try:
        if file is not None and file.filename:
            data = await file.read()
            if len(data) > MAX_UPLOAD_BYTES:
                raise UndecodableImage(
                    f"image is {len(data) // 1024 // 1024}MB; the limit is "
                    f"{MAX_UPLOAD_BYTES // 1024 // 1024}MB"
                )
            hits = find_by_bytes(session, data)
        elif image_url.strip():
            hits = find_by_url(session, image_url.strip())
        else:
            error = "Choose a file or paste an image URL."
    except UndecodableImage as e:
        error = str(e)
    except Exception as e:
        log.warning("image search failed: %s", e)
        error = f"Could not search that image: {e}"

    return templates.TemplateResponse(
        request, "image_search.html",
        {
            "categories": category_tree(session),
            "filters": _filters(request),
            "error": error,
            "query_image": image_url.strip() or None,
            "hits": [
                {
                    "card": product_card(session, h.product),
                    "distance": h.distance,
                    "confidence": h.confidence,
                    "score": round(h.score * 100),
                    "tier": h.tier,
                }
                for h in (hits or [])
            ] if hits is not None else None,
        },
    )


@app.post("/api/search/image")
async def api_image_search(
    file: UploadFile = File(None),
    image_url: str = Form(""),
    session: Session = Depends(db),
):
    from ..pipeline.imagesearch import UndecodableImage, find_by_bytes, find_by_url

    try:
        if file is not None and file.filename:
            hits = find_by_bytes(session, await file.read())
        elif image_url.strip():
            hits = find_by_url(session, image_url.strip())
        else:
            raise HTTPException(400, "provide a file or image_url")
    except UndecodableImage as e:
        raise HTTPException(400, str(e)) from e

    return {
        "count": len(hits),
        "results": [
            {
                "slug": h.product.slug,
                "title": h.product.title_en,
                "best_price_usd": h.product.best_price_usd,
                "site_count": h.product.site_count,
                "distance": h.distance,
                "confidence": h.confidence,
                "tier": h.tier,
            }
            for h in hits
        ],
    }


def _csv_response(body: str, filename: str) -> PlainTextResponse:
    return PlainTextResponse(
        body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/export/products.csv")
def export_products(request: Request, q: str = "", session: Session = Depends(db)):
    """Whatever the current search/filter shows, as a spreadsheet."""
    from ..pipeline.export import export_products_csv

    f = _filters(request)
    products, _ = query_products(
        session, q=q or f["q"], category_path=request.query_params.get("category", ""),
        site_key=f["site"], min_price=_as_float(f["min_price"]),
        max_price=_as_float(f["max_price"]), max_moq=_as_int(f["max_moq"]),
        direct_only=f["direct_only"], sort=f["sort"], page=1,
    )
    return _csv_response(export_products_csv(session, products), "sourcehub-products.csv")


@app.get("/export/product/{slug}.csv")
def export_product(slug: str, session: Session = Depends(db)):
    """Every site's terms for one product."""
    from ..pipeline.export import export_offers_csv

    product = session.scalar(select(CanonicalProduct).where(CanonicalProduct.slug == slug))
    if product is None:
        raise HTTPException(404, "Product not found")
    return _csv_response(export_offers_csv(session, product), f"{slug}.csv")


@app.get("/bom", response_class=HTMLResponse)
def bom_form(request: Request, session: Session = Depends(db)):
    return templates.TemplateResponse(
        request, "bom.html",
        {"categories": category_tree(session), "filters": _filters(request),
         "result": None, "raw_text": "", "direct_only": False},
    )


@app.post("/bom", response_class=HTMLResponse)
def bom_price(
    request: Request,
    lines: str = Form(""),
    direct_only: str = Form(""),
    fmt: str = Form(""),
    session: Session = Depends(db),
):
    """Cost a pasted parts list: best landed source per line, plus a total."""
    from ..pipeline.export import export_bom_csv, parse_bom, price_bom

    entries = parse_bom(lines)
    only_direct = direct_only in ("1", "true", "on")
    result = price_bom(session, entries, direct_only=only_direct) if entries else None

    if fmt == "csv" and result is not None:
        return _csv_response(export_bom_csv(result), "sourcehub-bom.csv")

    return templates.TemplateResponse(
        request, "bom.html",
        {
            "categories": category_tree(session),
            "filters": _filters(request),
            "raw_text": lines,
            "direct_only": only_direct,
            "result": result,
        },
    )


@app.post("/api/bom")
def api_bom(payload: dict, session: Session = Depends(db)):
    """JSON BOM costing. Accepts {"lines": "...text..."} or {"items": [[q, n], ...]}."""
    from ..pipeline.export import parse_bom, price_bom

    if "lines" in payload:
        entries = parse_bom(str(payload["lines"]))
    else:
        entries = [(str(q), int(n)) for q, n in (payload.get("items") or [])]
    result = price_bom(session, entries, direct_only=bool(payload.get("direct_only")))
    return {
        "total_usd": result.total_usd,
        "matched": result.matched_lines,
        "unmatched": result.unmatched,
        "agent_lines": result.agent_lines,
        "undisclosed_shipping": result.undisclosed_shipping,
        "lines": [
            {
                "query": ln.query, "qty": ln.qty, "order_qty": ln.order_qty,
                "overage": ln.overage, "site": ln.site_name,
                "product": ln.product.title_en if ln.product else None,
                "slug": ln.product.slug if ln.product else None,
                "unit_usd": ln.unit_usd, "moq": ln.moq,
                "shipping_usd": ln.shipping_usd, "line_total_usd": ln.line_total_usd,
                "needs_agent": ln.needs_agent, "note": ln.note,
            }
            for ln in result.lines
        ],
    }


@app.get("/supplier/{supplier_id}", response_class=HTMLResponse)
def supplier_page(request: Request, supplier_id: int, session: Session = Depends(db)):
    """Everything one seller lists -- the "what else do they make" view."""
    supplier = session.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(404, "Supplier not found")
    site = session.get(Site, supplier.site_id)

    offers = session.scalars(
        select(Offer)
        .where(Offer.supplier_id == supplier.id, Offer.is_active.is_(True))
        .order_by(Offer.price_usd.is_(None), Offer.price_usd)
        .limit(200)
    ).all()
    products = []
    seen: set[int] = set()
    for o in offers:
        if o.canonical_id and o.canonical_id not in seen:
            seen.add(o.canonical_id)
            p = session.get(CanonicalProduct, o.canonical_id)
            if p is not None and p.is_active:
                products.append(product_card(session, p))

    return templates.TemplateResponse(
        request, "supplier.html",
        {
            "categories": category_tree(session),
            "filters": _filters(request),
            "supplier": {
                "name": supplier.name_en or supplier.name,
                "name_raw": supplier.name,
                "site": site.name if site else "",
                "url": supplier.url,
                "years": supplier.years_active,
                "verified": supplier.is_verified,
                "rating": supplier.rating,
                "country": supplier.country,
                "offer_count": supplier.offer_count or len(offers),
                "first_seen": supplier.first_seen,
            },
            "products": products,
        },
    )


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, session: Session = Depends(db),
          _auth: None = Depends(require_admin)):
    runs = session.scalars(
        select(CrawlRun).order_by(CrawlRun.started_at.desc()).limit(40)
    ).all()
    reviews = session.scalars(
        select(MatchReview)
        .where(MatchReview.status == "pending")
        .order_by(MatchReview.score.desc())
        .limit(50)
    ).all()

    review_rows = []
    for r in reviews:
        offer = session.get(Offer, r.offer_id)
        product = session.get(CanonicalProduct, r.canonical_id)
        if not offer or not product:
            continue
        site = session.get(Site, offer.site_id)
        review_rows.append(
            {
                "id": r.id,
                "score": round(r.score, 3),
                "signals": r.signals,
                "offer_title": offer.title_en or offer.title_raw,
                "offer_site": site.name if site else "",
                "offer_url": offer.url,
                "product_title": product.title_en,
                "product_slug": product.slug,
            }
        )

    rejections = []
    rows = session.scalars(
        select(MatchRejection).order_by(MatchRejection.created_at.desc()).limit(50)
    ).all()
    seen_pairs: set[tuple[int, int]] = set()
    for r in rows:
        a, b = session.get(Offer, r.low_offer_id), session.get(Offer, r.high_offer_id)
        if not a or not b:
            continue
        # One row per (offer, product) pair -- a rejection against a 3-offer product
        # created 3 rows, and listing all of them is noise.
        key = (a.id, b.canonical_id or 0)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        product = session.get(CanonicalProduct, b.canonical_id) if b.canonical_id else None
        rejections.append({
            "offer_id": a.id,
            "canonical_id": b.canonical_id,
            "offer_title": a.title_en or a.title_raw,
            "other_title": b.title_en or b.title_raw,
            "product_slug": product.slug if product else None,
            "product_title": product.title_en if product else "(product removed)",
            "created_at": r.created_at,
        })

    from ..health import health_summary

    health = health_summary(session)

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "runs": runs,
            "rejections": rejections,
            "health": health["sites"],
            "health_attention": health["attention"],
            "reviews": review_rows,
            "stats": _stats(session),
            "sites": _site_rows(session),
            "categories": category_tree(session),
            "filters": _filters(request),
        },
    )


@app.post("/admin/review/{review_id}/{action}")
def resolve_review(review_id: int, action: str, session: Session = Depends(db),
                   _auth: None = Depends(require_admin),
                   _origin: None = Depends(require_same_origin)):
    review = session.get(MatchReview, review_id)
    if review is None:
        raise HTTPException(404, "No such review")
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")

    offer = session.get(Offer, review.offer_id)
    target = session.get(CanonicalProduct, review.canonical_id)

    if action == "approve":
        if offer and target:
            orphan_id = offer.canonical_id
            # An earlier rejection between these two is now overruled by this
            # approval; leaving it would block the merge again on the next rematch.
            clear_rejections(session, offer.id, target.id)
            offer.canonical_id = target.id
            offer.match_method = "human"
            offer.match_score = 1.0
            session.flush()
            rebuild_product(session, target)
            index_product(session, target)
            if orphan_id and orphan_id != target.id:
                orphan = session.get(CanonicalProduct, orphan_id)
                if orphan is not None:
                    rebuild_product(session, orphan)
                    index_product(session, orphan)
        review.status = "approved"
    else:
        if offer and target:
            # Make the ruling stick, and undo the merge if it already happened --
            # recording the rejection alone would leave the bad merge in place.
            if offer.canonical_id == target.id:
                detach_offer(session, offer)
            record_rejection(session, offer.id, target.id, note="admin review")
        review.status = "rejected"

    session.commit()
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/merge")
def merge_products(
    source_slug: str = Form(...),
    target_slug: str = Form(...),
    session: Session = Depends(db),
    _auth: None = Depends(require_admin),
    _origin: None = Depends(require_same_origin),
):
    """Fold every offer from one product into another.

    The matcher is conservative by design, so genuine duplicates survive it. Without
    a manual merge the only way to fix that was editing the database by hand.
    """
    source = session.scalar(select(CanonicalProduct).where(CanonicalProduct.slug == source_slug))
    target = session.scalar(select(CanonicalProduct).where(CanonicalProduct.slug == target_slug))
    if source is None or target is None:
        raise HTTPException(404, "Both products must exist")
    if source.id == target.id:
        raise HTTPException(400, "Cannot merge a product into itself")

    moved = 0
    for o in session.scalars(select(Offer).where(Offer.canonical_id == source.id)).all():
        # A prior rejection between these listings would silently undo the merge on
        # the next rematch, so an explicit merge clears it.
        clear_rejections(session, o.id, target.id)
        o.canonical_id = target.id
        o.match_method = "human_merge"
        o.match_score = 1.0
        moved += 1
    session.flush()

    rebuild_product(session, target)
    rebuild_product(session, source)
    index_product(session, target)
    if source.offer_count == 0:
        drop_product(session, source.id)
        session.delete(source)
    session.commit()
    log.info("merged %s offers from %s into %s", moved, source_slug, target_slug)
    return RedirectResponse(f"/product/{target_slug}", status_code=303)


@app.post("/admin/split/{offer_id}")
def split_offer(
    offer_id: int,
    session: Session = Depends(db),
    _auth: None = Depends(require_admin),
    _origin: None = Depends(require_same_origin),
):
    """Pull one listing off a product onto its own, and remember the decision.

    Splitting without recording a rejection would let the next crawl merge it
    straight back, which is the behaviour that made review effort evaporate.
    """
    offer = session.get(Offer, offer_id)
    if offer is None:
        raise HTTPException(404, "Offer not found")
    old_id = offer.canonical_id
    if old_id is None:
        raise HTTPException(400, "Offer is not attached to a product")

    detach_offer(session, offer)
    record_rejection(session, offer.id, old_id, note="admin split")
    session.commit()
    new_product = session.get(CanonicalProduct, offer.canonical_id)
    return RedirectResponse(
        f"/product/{new_product.slug}" if new_product else "/admin", status_code=303
    )


@app.post("/admin/unblock/{offer_id}/{canonical_id}")
def unblock_pair(offer_id: int, canonical_id: int, session: Session = Depends(db),
                 _auth: None = Depends(require_admin),
                 _origin: None = Depends(require_same_origin)):
    """Undo a rejection, so the matcher may reconsider the pair.

    A ruling the system treats as authoritative has to be reversible, or one
    misclick is permanent and unfixable from the UI.
    """
    removed = clear_rejections(session, offer_id, canonical_id)
    session.commit()
    log.info("cleared %s rejection pair(s) for offer %s / product %s",
             removed, offer_id, canonical_id)
    return RedirectResponse("/admin", status_code=303)


# ------------------------------------------------------------------- JSON API


@app.get("/api/search")
def api_search(
    request: Request,
    q: str = "",
    page: int = 1,
    session: Session = Depends(db),
):
    f = _filters(request)
    products, total = query_products(
        session,
        q=q,
        site_key=f["site"],
        min_price=_as_float(f["min_price"]),
        max_price=_as_float(f["max_price"]),
        max_moq=_as_int(f["max_moq"]),
        direct_only=f["direct_only"],
        sort=f["sort"],
        page=page,
    )
    return {
        "query": q,
        "total": total,
        "page": page,
        "page_size": PAGE_SIZE,
        "results": [product_card(session, p) for p in products],
    }


@app.get("/api/suggest")
def api_suggest(q: str = Query("", min_length=0), session: Session = Depends(db)):
    return {"suggestions": suggest(session, q)}


@app.get("/api/product/{slug}")
def api_product(slug: str, session: Session = Depends(db)):
    product = session.scalar(select(CanonicalProduct).where(CanonicalProduct.slug == slug))
    if product is None:
        raise HTTPException(404, "Product not found")
    offers = session.scalars(
        select(Offer)
        .where(Offer.canonical_id == product.id, Offer.is_active.is_(True))
        .order_by(Offer.price_usd.asc())
    ).all()
    return {
        "slug": product.slug,
        "title": product.title_en,
        "brand": product.brand,
        "model": product.model,
        "mpn": product.mpn,
        "gtin": product.gtin,
        "category": product.category.path if product.category else None,
        "specs": product.specs,
        "best_price_usd": product.best_price_usd,
        "best_landed_usd": product.best_landed_usd,
        "min_moq": product.min_moq,
        "offers": [offer_view(session, o) for o in offers],
    }


@app.get("/api/categories")
def api_categories(session: Session = Depends(db)):
    return {"categories": category_tree(session)}


@app.get("/api/stats")
def api_stats(session: Session = Depends(db)):
    return _stats(session)


@app.get("/healthz")
def healthz():
    return JSONResponse({"ok": True, "ts": dt.datetime.now(dt.timezone.utc).isoformat()})


# ------------------------------------------------------------------- internals


def _stats(session: Session) -> dict:
    return {
        "products": int(session.scalar(
            select(func.count(CanonicalProduct.id)).where(CanonicalProduct.is_active.is_(True))
        ) or 0),
        "offers": int(session.scalar(
            select(func.count(Offer.id)).where(Offer.is_active.is_(True))
        ) or 0),
        "multi_site": int(session.scalar(
            select(func.count(CanonicalProduct.id)).where(CanonicalProduct.site_count > 1)
        ) or 0),
        "images": int(session.scalar(select(func.count(Image.id))) or 0),
        "pending_reviews": int(session.scalar(
            select(func.count(MatchReview.id)).where(MatchReview.status == "pending")
        ) or 0),
        "rejections": int(session.scalar(select(func.count(MatchRejection.id))) or 0),
    }


def _site_rows(session: Session) -> list[dict]:
    counts = dict(
        session.execute(
            select(Offer.site_id, func.count(Offer.id))
            .where(Offer.is_active.is_(True))
            .group_by(Offer.site_id)
        ).all()
    )
    sites = session.scalars(select(Site).order_by(Site.name)).all()
    return [
        {
            "key": s.key,
            "name": s.name,
            "needs_agent": s.needs_agent,
            "currency": s.home_currency,
            "offers": int(counts.get(s.id, 0) or 0),
        }
        for s in sites
    ]
