"""Best Buy -- a second US retail baseline, from a real documented API.

Why a second one at all: eBay alone is a shaky reference. It's dominated by
auctions and used/refurbished lots, so its "cheapest US price" can be a beat-up
open-box unit rather than what a buyer actually gets choosing to buy domestically.
Best Buy sells only new, first-party inventory at a fixed price, which is a much
fairer thing to put next to a factory-fresh import.

Why this one specifically, and not Amazon or Walmart: Amazon's PA-API needs
affiliate approval (sales history, an active site), and Walmart's product/search
pages are behind aggressive bot detection -- confirmed live, including against
this project's own headless Chromium with its stealth patches ("Robot or human?"
on the very first request). Best Buy's Products API is self-serve: sign up at
https://developer.bestbuy.com, no approval wait, no scraping, no anti-bot problem --
exactly the eBay-API argument, a second time.

No HTML fallback (unlike eBay's): without BESTBUY_API_KEY this adapter simply
yields nothing, the same as Octopart without Nexar credentials. Best Buy's search
page itself isn't behind a bot wall, but it's a modern Next.js App Router page with
no embedded JSON blob to parse (server components stream as an internal RSC format,
not a stable one) -- reverse-engineering that only to duplicate what the free,
self-serve API already gives directly was not worth it.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, Optional

from ..config import get_settings
from ..util.text import clean, normalize_gtin
from .base import RawOffer, SiteAdapter

log = logging.getLogger(__name__)

# Kept short deliberately: `show=all` on a search response returns the same
# per-item payload as the docs' detail example, several times over for every
# result on the page, for fields (warranty text, full HTML descriptions,
# accessory SKUs...) this catalog has no use for.
SEARCH_FIELDS = (
    "sku,name,url,manufacturer,modelNumber,upc,color,condition,"
    "salePrice,regularPrice,onSale,"
    "onlineAvailability,orderable,"
    "shortDescription,class,subclass,"
    "image,thumbnailImage,largeImage,"
    "customerReviewAverage,customerReviewCount,"
    "shippingLevelsOfService.unitShippingPrice,"
    "weight,depth,width,height"
)
DETAIL_FIELDS = SEARCH_FIELDS + ",longDescription,features.feature"


class BestBuyAdapter(SiteAdapter):
    key = "bestbuy"
    name = "Best Buy"
    base_url = "https://www.bestbuy.com"
    home_currency = "USD"
    is_baseline = True

    SEARCH_URL = "https://api.bestbuy.com/v1/products"

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        api_key = get_settings().bestbuy_api_key
        if not api_key:
            log.info("[bestbuy] BESTBUY_API_KEY not configured, skipping. "
                     "Free key: https://developer.bestbuy.com")
            return

        pages = max_pages or self.max_pages
        page_size = 25
        # Best Buy's own Keyword Search: `search=word` per term, ANDed together --
        # a space is NOT accepted inside one search= value (it means OR there).
        terms = "&".join(f"search={_escape(t)}" for t in keyword.split() if t)
        if not terms:
            return

        for page in range(1, pages + 1):
            try:
                # Best Buy's filter expression is not a query parameter -- it's
                # appended directly to the path, e.g. .../products(search=a&search=b).
                payload = self.fetcher.get(
                    f"{self.SEARCH_URL}({terms})",
                    params={
                        "format": "json",
                        "show": SEARCH_FIELDS,
                        "page": str(page),
                        "pageSize": str(page_size),
                        "apiKey": api_key,
                    },
                    expect_json=True,
                ).json()
            except Exception as e:
                log.warning("[bestbuy] search page %s failed: %s", page, e)
                return

            products = payload.get("products") or []
            if not products:
                if page == 1:
                    log.info("[bestbuy] no results for %r", keyword)
                return
            for product in products:
                offer = self._offer(product)
                if offer:
                    yield offer
            if page >= int(payload.get("totalPages") or page):
                return

    def _offer(self, p: dict) -> Optional[RawOffer]:
        sku = p.get("sku")
        title = clean(p.get("name") or "")
        if not sku or not title:
            return None

        price = _as_float(p.get("salePrice")) or _as_float(p.get("regularPrice"))
        offer = RawOffer(
            site_key=self.key,
            site_product_id=str(sku),
            url=p.get("url") or f"{self.base_url}/site/-/{sku}.p",
            title=title,
            currency="USD",
            price_min=price,
            moq=1,
            brand=self.plausible_brand(clean(str(p.get("manufacturer") or ""))),
            model=self.plausible_mpn(clean(str(p.get("modelNumber") or "")), str(sku)),
            gtin=normalize_gtin(p.get("upc")),
            seller_name="Best Buy",
            in_stock=bool(p.get("onlineAvailability")) and bool(p.get("orderable", True)),
            rating=_as_float(p.get("customerReviewAverage")),
            review_count=_as_int(p.get("customerReviewCount")),
            category_path=" > ".join(
                c for c in (p.get("class"), p.get("subclass")) if c
            ) or None,
            raw={"source": "products_api"},
        )

        # Condition matters for a price baseline the same way it does on eBay: a
        # refurbished unit is not a fair comparison against a factory-fresh import.
        condition = clean(str(p.get("condition") or "")) or "New"
        offer.add_spec("Condition", condition)
        if p.get("color"):
            offer.add_spec("Color", clean(str(p["color"])))

        weight = clean(str(p.get("weight") or ""))
        if weight:
            offer.add_spec("Weight", weight if any(c.isalpha() for c in weight) else f"{weight} lb")
        depth, width, height = p.get("depth"), p.get("width"), p.get("height")
        if depth and width and height:
            offer.add_spec("Package Dimensions", f"{depth} x {width} x {height} in")

        for url in (p.get("image"), p.get("largeImage"), p.get("thumbnailImage")):
            if url and url not in offer.image_urls:
                offer.image_urls.append(url)

        desc = p.get("shortDescription")
        if desc:
            offer.description = clean(str(desc))[:8000]

        for svc in p.get("shippingLevelsOfService") or []:
            cost = _as_float(svc.get("unitShippingPrice"))
            if cost is not None:
                offer.shipping_cost, offer.shipping_currency = cost, "USD"
                offer.shipping_free = cost == 0.0
                break

        return offer

    def fetch_detail(self, offer: RawOffer) -> RawOffer:
        api_key = get_settings().bestbuy_api_key
        if not api_key:
            return offer
        try:
            payload = self.fetcher.get(
                f"{self.SEARCH_URL}/{offer.site_product_id}.json",
                params={"show": DETAIL_FIELDS, "apiKey": api_key},
                expect_json=True,
            ).json()
        except Exception as e:
            log.debug("[bestbuy] detail failed for %s: %s", offer.site_product_id, e)
            offer.detail_fetched = True
            return offer

        if isinstance(payload, dict) and payload.get("longDescription"):
            offer.description = clean(str(payload["longDescription"]))[:8000]
        for feature in (payload.get("features") or [])[:20] if isinstance(payload, dict) else []:
            text = clean(str(feature.get("feature") or "")) if isinstance(feature, dict) else ""
            if text:
                offer.add_spec("Feature", text)

        offer.detail_fetched = True
        return offer


def _escape(term: str) -> str:
    """Best Buy's query-string search syntax has no quoting mechanism of its own --
    strip the characters that would otherwise be read as query grammar (parens,
    the field-search '=', boolean '&'/'|') rather than literal text."""
    return "".join(c for c in term if c not in "()=&|")


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    f = _as_float(value)
    return int(f) if f is not None else None
