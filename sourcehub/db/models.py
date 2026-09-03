"""Schema.

Shape of the thing:

    Site 1---* Offer *---1 CanonicalProduct 1---* Image
                |                  |
                +-* PriceTier      +-1 Category (self-referencing tree)
                +-* OfferSpec

An *Offer* is one listing on one site. A *CanonicalProduct* is the merged item page
the user actually reads -- it owns the English title, the unified spec sheet, and it
gathers every site's offer underneath it. Matching (pipeline/matching.py) is what
decides which offers collapse into one canonical product.

Everything raw is preserved alongside its English translation (``*_raw`` + ``*_en``)
so a bad translation is always recoverable and never destroys source data.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy import DateTime as SADateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .session import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class DateTime(TypeDecorator):
    """Timezone-aware UTC datetimes that survive SQLite.

    SQLite has no datetime type -- it stores an ISO string and hands it back
    *naive*, so a value just written (aware) cannot be compared against one loaded
    from disk (naive) and any ``max()`` over a mixed set raises TypeError. This
    coerces on the way in and re-attaches UTC on the way out, so application code
    can assume every datetime is aware.
    """

    impl = SADateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------- sites


class Site(Base, TimestampMixin):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    base_url: Mapped[str] = mapped_column(String(255))
    home_currency: Mapped[str] = mapped_column(String(8), default="USD")
    default_language: Mapped[str] = mapped_column(String(8), default="en")

    # True for 1688/Taobao/Tmall: domestic-China only, you physically cannot order
    # without a forwarding agent. Drives the "agent required" badge on the item page.
    needs_agent: Mapped[bool] = mapped_column(Boolean, default=False)
    is_wholesale: Mapped[bool] = mapped_column(Boolean, default=False)
    # A reference price rather than a sourcing option: domestic retail, shown
    # to answer "is importing actually cheaper", not "should I buy 500 here".
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    offers: Mapped[list["Offer"]] = relationship(back_populates="site")


# ------------------------------------------------------------------- category tree


class Category(Base, TimestampMixin):
    __tablename__ = "categories"
    __table_args__ = (Index("ix_cat_parent_slug", "parent_id", "slug", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    name: Mapped[str] = mapped_column(String(160))
    parent_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id"), index=True)

    # Materialized path ("electronics/audio/earbuds") so a whole subtree is one
    # LIKE 'path/%' query instead of a recursive walk.
    path: Mapped[str] = mapped_column(String(512), index=True, default="")
    level: Mapped[int] = mapped_column(Integer, default=0)
    icon: Mapped[Optional[str]] = mapped_column(String(32))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    product_count: Mapped[int] = mapped_column(Integer, default=0)

    parent: Mapped[Optional["Category"]] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list["Category"]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )
    products: Mapped[list["CanonicalProduct"]] = relationship(back_populates="category")

    def ancestors_path(self) -> list[str]:
        return [seg for seg in self.path.split("/") if seg]


class SiteCategoryMap(Base):
    """Raw per-site category strings mapped onto our taxonomy.

    Every site names categories differently ("Consumer Electronics > Earphones"
    vs "耳机"), so we record the raw breadcrumb once and learn the mapping.
    Unmapped rows surface in the admin queue.
    """

    __tablename__ = "site_category_map"
    __table_args__ = (UniqueConstraint("site_id", "raw_path", name="uq_sitecat"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    raw_path: Mapped[str] = mapped_column(String(512))
    raw_path_en: Mapped[Optional[str]] = mapped_column(String(512))
    category_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id"), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    hits: Mapped[int] = mapped_column(Integer, default=0)


# ------------------------------------------------------------- canonical products


class CanonicalProduct(Base, TimestampMixin):
    __tablename__ = "canonical_products"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(220), unique=True, index=True)

    title_en: Mapped[str] = mapped_column(String(512))
    brand: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    model: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    mpn: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    gtin: Mapped[Optional[str]] = mapped_column(String(20), index=True)  # UPC/EAN normalized to 14
    description_en: Mapped[Optional[str]] = mapped_column(Text)

    category_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id"), index=True)
    primary_image_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("images.id", use_alter=True, name="fk_prod_primary_img")
    )

    # Union of every offer's specs, key-normalized. {"Battery": "500mAh", ...}
    specs: Mapped[dict] = mapped_column(JSON, default=dict)

    # Denormalized rollups so listing pages never fan out into per-offer queries.
    offer_count: Mapped[int] = mapped_column(Integer, default=0, index=True)
    site_count: Mapped[int] = mapped_column(Integer, default=0)
    best_price_usd: Mapped[Optional[float]] = mapped_column(Float, index=True)
    best_price_site_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sites.id"))
    best_landed_usd: Mapped[Optional[float]] = mapped_column(Float, index=True)  # incl. shipping
    max_price_usd: Mapped[Optional[float]] = mapped_column(Float)
    min_moq: Mapped[Optional[int]] = mapped_column(Integer)

    first_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    category: Mapped[Optional["Category"]] = relationship(back_populates="products")
    offers: Mapped[list["Offer"]] = relationship(
        back_populates="canonical", cascade="all, delete-orphan"
    )
    images: Mapped[list["Image"]] = relationship(
        back_populates="canonical",
        foreign_keys="Image.canonical_id",
        cascade="all, delete-orphan",
    )
    primary_image: Mapped[Optional["Image"]] = relationship(
        foreign_keys=[primary_image_id], post_update=True
    )


# ------------------------------------------------------------------------- offers


class Offer(Base, TimestampMixin):
    """One listing on one site."""

    __tablename__ = "offers"
    __table_args__ = (
        UniqueConstraint("site_id", "site_product_id", name="uq_offer_site_product"),
        Index("ix_offer_canon_price", "canonical_id", "price_usd"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    site_product_id: Mapped[str] = mapped_column(String(160))
    canonical_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("canonical_products.id"), index=True
    )

    url: Mapped[str] = mapped_column(String(1024))
    title_raw: Mapped[str] = mapped_column(String(512))
    title_en: Mapped[Optional[str]] = mapped_column(String(512))
    source_lang: Mapped[str] = mapped_column(String(8), default="auto")
    description_raw: Mapped[Optional[str]] = mapped_column(Text)
    description_en: Mapped[Optional[str]] = mapped_column(Text)

    brand: Mapped[Optional[str]] = mapped_column(String(120))
    model: Mapped[Optional[str]] = mapped_column(String(160))
    mpn: Mapped[Optional[str]] = mapped_column(String(160), index=True)
    gtin: Mapped[Optional[str]] = mapped_column(String(20), index=True)

    # --- price, as listed ---
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    price_min: Mapped[Optional[float]] = mapped_column(Float)
    price_max: Mapped[Optional[float]] = mapped_column(Float)
    price_usd: Mapped[Optional[float]] = mapped_column(Float, index=True)  # unit price @ MOQ
    fx_rate: Mapped[Optional[float]] = mapped_column(Float)

    # --- order terms ---
    moq: Mapped[int] = mapped_column(Integer, default=1)
    moq_unit: Mapped[str] = mapped_column(String(32), default="piece")
    lead_time_days: Mapped[Optional[int]] = mapped_column(Integer)

    # --- shipping & fees (whatever the site actually discloses; None = not listed) ---
    shipping_cost_usd: Mapped[Optional[float]] = mapped_column(Float)
    shipping_free: Mapped[bool] = mapped_column(Boolean, default=False)
    shipping_from: Mapped[Optional[str]] = mapped_column(String(80))
    shipping_to: Mapped[Optional[str]] = mapped_column(String(8), default="US")
    shipping_note_raw: Mapped[Optional[str]] = mapped_column(String(512))
    shipping_note_en: Mapped[Optional[str]] = mapped_column(String(512))
    fees_note_raw: Mapped[Optional[str]] = mapped_column(String(512))
    fees_note_en: Mapped[Optional[str]] = mapped_column(String(512))
    duty_rate: Mapped[Optional[float]] = mapped_column(Float)   # 0.075 = 7.5%
    duty_usd: Mapped[Optional[float]] = mapped_column(Float)
    # price_usd*moq + shipping (+ duty when configured); the number that
    # actually matters when comparing
    landed_cost_usd: Mapped[Optional[float]] = mapped_column(Float, index=True)

    # --- seller ---
    seller_name: Mapped[Optional[str]] = mapped_column(String(200))
    seller_url: Mapped[Optional[str]] = mapped_column(String(1024))
    seller_years: Mapped[Optional[int]] = mapped_column(Integer)
    is_verified_supplier: Mapped[bool] = mapped_column(Boolean, default=False)
    rating: Mapped[Optional[float]] = mapped_column(Float)
    review_count: Mapped[Optional[int]] = mapped_column(Integer)
    orders_count: Mapped[Optional[int]] = mapped_column(Integer)

    raw_category_path: Mapped[Optional[str]] = mapped_column(String(512))
    raw_payload: Mapped[Optional[dict]] = mapped_column(JSON)

    in_stock: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    supplier_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("suppliers.id"), index=True
    )
    variant_count: Mapped[int] = mapped_column(Integer, default=0)
    match_score: Mapped[Optional[float]] = mapped_column(Float)
    match_method: Mapped[Optional[str]] = mapped_column(String(32))

    first_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    detail_fetched_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))

    site: Mapped["Site"] = relationship(back_populates="offers")
    supplier: Mapped[Optional["Supplier"]] = relationship(back_populates="offers")
    canonical: Mapped[Optional["CanonicalProduct"]] = relationship(back_populates="offers")
    tiers: Mapped[list["PriceTier"]] = relationship(
        back_populates="offer", cascade="all, delete-orphan", order_by="PriceTier.min_qty"
    )
    variants: Mapped[list["OfferVariant"]] = relationship(
        back_populates="offer", cascade="all, delete-orphan",
        order_by="OfferVariant.position",
    )
    specs: Mapped[list["OfferSpec"]] = relationship(
        back_populates="offer", cascade="all, delete-orphan"
    )
    images: Mapped[list["Image"]] = relationship(
        back_populates="offer", foreign_keys="Image.offer_id", cascade="all, delete-orphan"
    )
    history: Mapped[list["PriceHistory"]] = relationship(
        back_populates="offer", cascade="all, delete-orphan"
    )


class Supplier(Base, TimestampMixin):
    """A seller, as an entity rather than a string repeated on every listing.

    Without this you cannot ask the two questions that matter when sourcing: *what
    else does this supplier make*, and *is this the one I already vetted*. Identity
    is (site, normalized name) -- there is no cross-site seller id to join on, and
    pretending otherwise would silently merge unrelated companies sharing a name.
    """

    __tablename__ = "suppliers"
    __table_args__ = (
        UniqueConstraint("site_id", "name_norm", name="uq_supplier_site_name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    name_norm: Mapped[str] = mapped_column(String(200), index=True)
    name_en: Mapped[Optional[str]] = mapped_column(String(200))
    url: Mapped[Optional[str]] = mapped_column(String(1024))

    years_active: Mapped[Optional[int]] = mapped_column(Integer)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    rating: Mapped[Optional[float]] = mapped_column(Float)
    country: Mapped[Optional[str]] = mapped_column(String(80))

    offer_count: Mapped[int] = mapped_column(Integer, default=0, index=True)
    first_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    offers: Mapped[list["Offer"]] = relationship(back_populates="supplier")


class OfferVariant(Base):
    """One purchasable SKU within a listing (colour, capacity, plug type...).

    Real listings are not one price. A "USB Hub" listing sells a 4-port at $9 and an
    8-port at $24 under one title and one photo. Collapsing that to a single number
    makes the catalog quietly wrong in both directions: the headline looks too cheap,
    and the variant you actually want is invisible.

    Variants are attached to the *offer*, not the canonical product, because the SKU
    split is a property of how one seller chose to list -- another site may sell the
    same three SKUs as three separate listings.
    """

    __tablename__ = "offer_variants"
    __table_args__ = (
        UniqueConstraint("offer_id", "sku", name="uq_variant_sku"),
        Index("ix_variant_offer_price", "offer_id", "price_usd"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"), index=True)

    sku: Mapped[str] = mapped_column(String(160))
    name_raw: Mapped[str] = mapped_column(String(300))
    name_en: Mapped[Optional[str]] = mapped_column(String(300))
    # {"Color": "Black", "Capacity": "128GB"} -- normalized keys where known.
    attrs: Mapped[dict] = mapped_column(JSON, default=dict)

    price: Mapped[Optional[float]] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    price_usd: Mapped[Optional[float]] = mapped_column(Float, index=True)
    stock: Mapped[Optional[int]] = mapped_column(Integer)
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True)
    image_url: Mapped[Optional[str]] = mapped_column(String(1024))
    position: Mapped[int] = mapped_column(Integer, default=0)

    offer: Mapped["Offer"] = relationship(back_populates="variants")


class PriceTier(Base):
    """Volume break: '100-499 pcs -> $2.10'. Ubiquitous on 1688/Alibaba/Made-in-China."""

    __tablename__ = "price_tiers"

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"), index=True)
    min_qty: Mapped[int] = mapped_column(Integer)
    max_qty: Mapped[Optional[int]] = mapped_column(Integer)  # None = "and up"
    price: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    price_usd: Mapped[Optional[float]] = mapped_column(Float)

    offer: Mapped["Offer"] = relationship(back_populates="tiers")


class OfferSpec(Base):
    __tablename__ = "offer_specs"
    __table_args__ = (Index("ix_spec_offer_key", "offer_id", "key_norm"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"), index=True)
    key_raw: Mapped[str] = mapped_column(String(200))
    key_en: Mapped[Optional[str]] = mapped_column(String(200))
    key_norm: Mapped[Optional[str]] = mapped_column(String(200))  # canonical key for merging
    value_raw: Mapped[str] = mapped_column(String(1000))
    value_en: Mapped[Optional[str]] = mapped_column(String(1000))
    position: Mapped[int] = mapped_column(Integer, default=0)

    offer: Mapped["Offer"] = relationship(back_populates="specs")


class PriceHistory(Base):
    __tablename__ = "price_history"
    __table_args__ = (Index("ix_hist_offer_ts", "offer_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"), index=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    price_usd: Mapped[Optional[float]] = mapped_column(Float)
    landed_cost_usd: Mapped[Optional[float]] = mapped_column(Float)
    moq: Mapped[Optional[int]] = mapped_column(Integer)
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True)

    offer: Mapped["Offer"] = relationship(back_populates="history")


# ------------------------------------------------------------------------- images


class Image(Base):
    __tablename__ = "images"
    __table_args__ = (Index("ix_img_phash", "phash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("canonical_products.id"), index=True
    )
    offer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("offers.id"), index=True)

    src_url: Mapped[str] = mapped_column(String(1024))
    local_path: Mapped[Optional[str]] = mapped_column(String(512))
    thumb_path: Mapped[Optional[str]] = mapped_column(String(512))
    sha256: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    phash: Mapped[Optional[str]] = mapped_column(String(32))  # hex perceptual hash
    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    canonical: Mapped[Optional["CanonicalProduct"]] = relationship(
        back_populates="images", foreign_keys=[canonical_id]
    )
    offer: Mapped[Optional["Offer"]] = relationship(
        back_populates="images", foreign_keys=[offer_id]
    )


# ------------------------------------------------------------- translation cache


class Translation(Base):
    """Content-addressed cache. Re-crawling a listing costs zero translation calls."""

    __tablename__ = "translations"

    id: Mapped[int] = mapped_column(primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    src_lang: Mapped[str] = mapped_column(String(8))
    src_text: Mapped[str] = mapped_column(Text)
    dst_text: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FxRate(Base):
    __tablename__ = "fx_rates"

    id: Mapped[int] = mapped_column(primary_key=True)
    currency: Mapped[str] = mapped_column(String(8), unique=True, index=True)
    per_usd: Mapped[float] = mapped_column(Float)  # 1 USD = per_usd <currency>
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ----------------------------------------------------------- forwarding agents


class ShippingAgent(Base, TimestampMixin):
    """Taobao/1688 forwarding agents ("daigou") that accept US customers."""

    __tablename__ = "shipping_agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    home_url: Mapped[str] = mapped_column(String(512))
    # Deep link with {url} / {ref} placeholders -> agent's "buy this link" page.
    url_template: Mapped[str] = mapped_column(String(1024))
    supported_site_keys: Mapped[list] = mapped_column(JSON, default=list)
    service_fee_note: Mapped[Optional[str]] = mapped_column(String(255))
    ships_to_us: Mapped[bool] = mapped_column(Boolean, default=True)
    consolidation: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


# ------------------------------------------------------------------ crawl ledger


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    site_key: Mapped[str] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(32))  # search | detail | refresh
    query: Mapped[Optional[str]] = mapped_column(String(255))
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    ok: Mapped[Optional[bool]] = mapped_column(Boolean)
    offers_seen: Mapped[int] = mapped_column(Integer, default=0)
    offers_new: Mapped[int] = mapped_column(Integer, default=0)
    offers_updated: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text)


class SearchDemand(Base):
    """A keyword someone searched for, and what we did about it.

    Persisted rather than kept in memory because the cooldown is the only thing
    standing between "search triggers a crawl" and "every page refresh hammers
    eleven marketplaces". A restart must not reset it.
    """

    __tablename__ = "search_demand"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Normalised (casefolded, whitespace collapsed) so "USB Hub" and "usb  hub"
    # share one cooldown.
    keyword: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display: Mapped[str] = mapped_column(String(255))

    first_requested: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    last_requested: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    request_count: Mapped[int] = mapped_column(Integer, default=1)

    last_crawled: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    crawl_count: Mapped[int] = mapped_column(Integer, default=0)
    # queued | running | done | failed
    last_status: Mapped[Optional[str]] = mapped_column(String(16))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    offers_found: Mapped[int] = mapped_column(Integer, default=0)


class Watch(Base, TimestampMixin):
    """A price the user is waiting for.

    Deliberately keyed on the canonical product, not an offer: you care that *the
    thing* got cheaper somewhere, not that one particular listing did. Offers churn
    constantly -- sellers delist and relist -- so a watch pinned to an offer id
    would quietly stop firing.
    """

    __tablename__ = "watches"
    __table_args__ = (
        UniqueConstraint("canonical_id", "label", name="uq_watch_product_label"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_id: Mapped[int] = mapped_column(
        ForeignKey("canonical_products.id"), index=True
    )
    label: Mapped[str] = mapped_column(String(120), default="")
    target_usd: Mapped[Optional[float]] = mapped_column(Float)
    # Compare against landed cost rather than unit price when set: a unit-price
    # alert on a MOQ-500 listing fires on a number you cannot actually pay.
    use_landed: Mapped[bool] = mapped_column(Boolean, default=False)
    direct_only: Mapped[bool] = mapped_column(Boolean, default=False)
    # Fire when the product becomes buyable again rather than on a price. Sourcing
    # failures are as often "the only good supplier went out of stock" as they are
    # about price, and that transition is invisible without watching for it.
    on_restock: Mapped[bool] = mapped_column(Boolean, default=False)
    last_in_stock: Mapped[Optional[bool]] = mapped_column(Boolean)
    notify_url: Mapped[Optional[str]] = mapped_column(String(1024))

    # Price when the watch was created, so "cheapest ever seen" is meaningful.
    baseline_usd: Mapped[Optional[float]] = mapped_column(Float)
    last_price_usd: Mapped[Optional[float]] = mapped_column(Float)
    last_triggered_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True))
    trigger_count: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    product: Mapped["CanonicalProduct"] = relationship()


class MatchReview(Base):
    """Borderline merges parked for a human. Approving is one click in the admin UI."""

    __tablename__ = "match_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"), index=True)
    canonical_id: Mapped[int] = mapped_column(ForeignKey("canonical_products.id"), index=True)
    score: Mapped[float] = mapped_column(Float)
    signals: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MatchRejection(Base):
    """A human's ruling that two listings are NOT the same product.

    Anchored to a **pair of offers**, not to (offer, product). Canonical products
    are derived state -- they get merged, split, emptied and deleted as the catalog
    churns -- so a rejection stored against a product id decays into meaninglessness.
    Offers are stable: one row per (site, site_product_id), for as long as the
    listing exists. "These two listings are different things" therefore stays true
    however the products around them are reshaped.

    The pair is stored normalized (low id, high id) so a single row covers both
    directions and the unique constraint actually prevents duplicates.
    """

    __tablename__ = "match_rejections"
    __table_args__ = (
        UniqueConstraint("low_offer_id", "high_offer_id", name="uq_rejection_pair"),
        Index("ix_rejection_high", "high_offer_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    low_offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"), index=True)
    high_offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"), index=True)
    reason: Mapped[str] = mapped_column(String(32), default="human")
    note: Mapped[Optional[str]] = mapped_column(String(512))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @staticmethod
    def normalize(a: int, b: int) -> tuple[int, int]:
        return (a, b) if a <= b else (b, a)
