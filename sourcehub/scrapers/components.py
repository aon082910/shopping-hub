"""Electronic component distributors: LCSC and Octopart.

Qualitatively different from every other adapter here, and much better. Marketplace
listings share no identifier, so matching falls back to fuzzy title and image
comparison. Component distributors publish the **manufacturer part number** on every
line -- an ESP32-WROOM-32E is that exact string at LCSC, Mouser and Digi-Key alike.
That drives the matcher's brand+MPN path (weight 0.92) instead of the fuzzy one, so
cross-site matching here is near-exact rather than a guess.

LCSC is the China-side distributor, which makes it the natural companion to the
marketplaces: the honest price for a real, traceable part, against which a $0.30
"ESP32" listing on AliExpress can be judged.

Octopart aggregates many distributors behind one API. It needs a free key; without
one it says so and returns nothing rather than scraping a site whose terms forbid it.
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional
from urllib.parse import quote_plus

from ..config import get_settings
from ..util.text import clean
from .base import RawOffer, RawTier, SiteAdapter

log = logging.getLogger(__name__)


class LcscAdapter(SiteAdapter):
    """LCSC. Public JSON search endpoint, tiered pricing, real MPNs."""

    key = "lcsc"
    name = "LCSC"
    base_url = "https://www.lcsc.com"
    home_currency = "USD"

    SEARCH_API = "https://wmsc.lcsc.com/wmsc/search/global"
    SEARCH = "https://www.lcsc.com/search?q={kw}&page={page}"

    def extra_headers(self) -> dict:
        return {
            "Accept": "application/json, text/plain, */*",
            "Referer": self.base_url + "/",
            "Content-Type": "application/json",
        }

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        for page in range(1, (max_pages or self.max_pages) + 1):
            try:
                payload = self.fetcher.post(
                    self.SEARCH_API,
                    json_body={"keyword": keyword, "currentPage": page, "pageSize": 25},
                    headers=self.extra_headers(),
                ).json()
            except Exception as e:
                log.warning("[lcsc] search page %s failed: %s", page, e)
                return

            # LCSC answers HTTP 200 with an application-level error body. Treating
            # that as "no results" is how a moved endpoint masquerades as an empty
            # catalog for weeks -- say plainly which of the two it is.
            if payload.get("ok") is False or payload.get("code") not in (200, None, 0):
                log.error(
                    "[lcsc] API rejected the request (code=%s): %s. The endpoint has "
                    "most likely moved; check SEARCH_API in scrapers/components.py.",
                    payload.get("code"), str(payload.get("msg"))[:120],
                )
                return

            result = payload.get("result") or {}
            items = (
                (result.get("productSearchResultVO") or {}).get("productList")
                or result.get("productList")
                or []
            )
            if not items:
                log.info("[lcsc] no items on page %s for %r", page, keyword)
                return
            for item in items:
                offer = self._offer(item)
                if offer:
                    yield offer

    def _offer(self, item: dict) -> Optional[RawOffer]:
        code = str(item.get("productCode") or "")
        mpn = clean(str(item.get("productModel") or ""))
        title = clean(str(item.get("productIntroEn") or item.get("productModel") or ""))
        if not code or not (title or mpn):
            return None

        offer = RawOffer(
            site_key=self.key,
            site_product_id=code,
            url=f"{self.base_url}/product-detail/{code}.html",
            title=(f"{mpn} {title}".strip() if mpn else title)[:512],
            currency="USD",
            # The part number is the point: the same string at every distributor,
            # so the matcher can use its exact-identifier path instead of guessing.
            mpn=mpn or None,
            model=mpn or None,
            brand=clean(str(item.get("brandNameEn") or "")) or None,
            moq=max(1, int(item.get("minBuyNumber") or 1)),
            moq_unit="piece",
            seller_name="LCSC",
            category_path=clean(str(item.get("catalogName") or "")) or None,
            raw={"source": "lcsc_api", "stock": item.get("stockNumber")},
        )

        for tier in item.get("productPriceList") or []:
            try:
                qty = int(tier.get("ladder"))
                price = float(tier.get("usdPrice") or tier.get("productPrice"))
            except (TypeError, ValueError):
                continue
            offer.tiers.append(RawTier(qty, None, price, "USD"))
        if offer.tiers:
            offer.tiers.sort(key=lambda t: t.min_qty)
            # Close the open-ended upper bounds so the ladder reads correctly.
            for a, b in zip(offer.tiers, offer.tiers[1:]):
                a.max_qty = b.min_qty - 1
            offer.moq = max(offer.moq, offer.tiers[0].min_qty)
            offer.price_min = min(t.price for t in offer.tiers)
            offer.price_max = max(t.price for t in offer.tiers)

        stock = item.get("stockNumber")
        if stock is not None:
            offer.in_stock = int(stock or 0) > 0
            offer.add_spec("Stock", str(stock))
        for key in ("encapStandard", "packageName"):
            if item.get(key):
                offer.add_spec("Package", str(item[key]))
                break

        img = item.get("productImageUrl") or item.get("productImageUrlBig")
        if img:
            offer.image_urls.append(img if img.startswith("http") else "https:" + img)
        return offer


class OctopartAdapter(SiteAdapter):
    """Octopart (Nexar) -- many distributors behind one GraphQL API."""

    key = "octopart"
    name = "Octopart"
    base_url = "https://octopart.com"
    home_currency = "USD"

    TOKEN_URL = "https://identity.nexar.com/connect/token"
    GRAPHQL_URL = "https://api.nexar.com/graphql"

    QUERY = (
        "query Search($q: String!, $limit: Int!) {"
        "  supSearchMpn(q: $q, limit: $limit) { results { part {"
        "    mpn manufacturer { name } shortDescription bestImage { url }"
        "    sellers { company { name } offers { clickUrl inventoryLevel"
        "      prices { quantity price currency } } } } } } }"
    )

    def __init__(self, config=None):
        super().__init__(config)
        self._token: Optional[str] = None

    def _access_token(self) -> Optional[str]:
        if self._token:
            return self._token
        s = get_settings()
        if not (s.octopart_client_id and s.octopart_client_secret):
            return None
        try:
            payload = self.fetcher.post(
                self.TOKEN_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": s.octopart_client_id,
                    "client_secret": s.octopart_client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ).json()
        except Exception as e:
            log.error("[octopart] token request failed: %s", e)
            return None
        self._token = payload.get("access_token")
        return self._token

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        token = self._access_token()
        if not token:
            log.info("[octopart] OCTOPART_CLIENT_ID/SECRET not configured, skipping. "
                     "Free keys: https://nexar.com")
            return

        limit = min(50, (max_pages or 1) * 20)
        try:
            payload = self.fetcher.post(
                self.GRAPHQL_URL,
                json_body={"query": self.QUERY,
                           "variables": {"q": keyword, "limit": limit}},
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
            ).json()
        except Exception as e:
            log.warning("[octopart] search failed: %s", e)
            return

        results = ((payload.get("data") or {}).get("supSearchMpn") or {}).get("results") or []
        for entry in results:
            offer = self._offer(entry.get("part") or {})
            if offer:
                yield offer

    def _offer(self, part: dict) -> Optional[RawOffer]:
        mpn = clean(str(part.get("mpn") or ""))
        if not mpn:
            return None
        brand = clean(str((part.get("manufacturer") or {}).get("name") or ""))

        # Cheapest seller across all distributors -- that is the value of an
        # aggregator, and quoting an arbitrary one would throw it away.
        best_price = best_seller = best_url = None
        moq = 1
        tiers: list = []
        for seller in part.get("sellers") or []:
            name = clean(str((seller.get("company") or {}).get("name") or ""))
            for sell_offer in seller.get("offers") or []:
                for price in sell_offer.get("prices") or []:
                    if (price.get("currency") or "USD") != "USD":
                        continue
                    try:
                        qty = int(price.get("quantity") or 1)
                        value = float(price.get("price"))
                    except (TypeError, ValueError):
                        continue
                    tiers.append(RawTier(qty, None, value, "USD"))
                    if best_price is None or value < best_price:
                        best_price, best_seller = value, name
                        best_url = sell_offer.get("clickUrl")
                        moq = max(1, qty)

        offer = RawOffer(
            site_key=self.key,
            site_product_id=(f"{brand}:{mpn}" if brand else mpn)[:120],
            url=best_url or f"{self.base_url}/search?q={quote_plus(mpn)}",
            title=f"{mpn} {clean(str(part.get('shortDescription') or ''))}".strip()[:512],
            currency="USD",
            price_min=best_price,
            mpn=mpn,
            model=mpn,
            brand=brand or None,
            moq=moq,
            seller_name=best_seller,
            raw={"source": "nexar_graphql"},
        )
        if tiers:
            tiers.sort(key=lambda t: t.min_qty)
            offer.tiers = tiers[:8]
            offer.price_max = max(t.price for t in tiers)
        img = (part.get("bestImage") or {}).get("url")
        if img:
            offer.image_urls.append(img)
        return offer
