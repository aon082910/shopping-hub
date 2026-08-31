"""eBay -- the US retail baseline.

Every other site here answers "which Chinese marketplace is cheapest". None answers
the question that actually decides a purchase: **is sourcing from China cheaper than
buying domestically, today, with fast delivery and a returns policy?** A $4 hub from
AliExpress that lands in three weeks is not obviously better than a $9 one from a US
seller, and until the catalog holds a domestic price there is nothing to compare to.

eBay is the right first baseline: a real, documented, free-tier API (Browse), US
inventory, and no anti-bot problem. Amazon would be better data but its API needs
affiliate approval and its pages actively resist scraping.

Two drivers:
  * **api**  -- Browse API when EBAY_CLIENT_ID/EBAY_CLIENT_SECRET are set. OAuth
    client-credentials, token cached for the run. Preferred.
  * **html** -- public search page, so the adapter is useful without keys.

Marked ``is_baseline`` so the UI presents it as a reference price rather than as
another sourcing option -- nobody buys 500 units from an eBay listing.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from typing import Any, Iterator, Optional
from urllib.parse import quote_plus

from selectolax.parser import HTMLParser

from ..config import get_settings
from ..util.money import parse_price
from ..util.text import clean
from .base import RawOffer, SiteAdapter

log = logging.getLogger(__name__)


class EbayAdapter(SiteAdapter):
    key = "ebay"
    name = "eBay"
    base_url = "https://www.ebay.com"
    home_currency = "USD"
    is_baseline = True

    SEARCH = "https://www.ebay.com/sch/i.html?_nkw={kw}&_pgn={page}&LH_BIN=1"
    OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
    BROWSE_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

    def __init__(self, config=None):
        super().__init__(config)
        self._token: Optional[str] = None
        self._token_expires = 0.0

    def extra_headers(self) -> dict[str, str]:
        return {"Accept-Language": "en-US,en;q=0.9", "Referer": self.base_url + "/"}

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        s = get_settings()
        pages = max_pages or self.max_pages
        source = (
            self._search_api(keyword, pages)
            if (s.ebay_client_id and s.ebay_client_secret)
            else self._search_html(keyword, pages)
        )
        # eBay repeats listings across sponsored and organic slots.
        yield from self.dedupe(source)

    def _access_token(self) -> Optional[str]:
        """Client-credentials token, cached until shortly before it expires."""
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        s = get_settings()
        creds = base64.b64encode(
            f"{s.ebay_client_id}:{s.ebay_client_secret}".encode()
        ).decode()
        try:
            payload = self.fetcher.post(
                self.OAUTH_URL,
                data={
                    "grant_type": "client_credentials",
                    "scope": "https://api.ebay.com/oauth/api_scope",
                },
                headers={
                    "Authorization": f"Basic {creds}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            ).json()
        except Exception as e:
            log.error("[ebay] OAuth failed: %s", e)
            return None
        self._token = payload.get("access_token")
        self._token_expires = time.time() + float(payload.get("expires_in", 0) or 0)
        if not self._token:
            log.error("[ebay] OAuth returned no token: %s", str(payload)[:200])
        return self._token

    def _search_api(self, keyword: str, pages: int) -> Iterator[RawOffer]:
        token = self._access_token()
        if not token:
            log.warning("[ebay] no token; falling back to the public search page")
            yield from self._search_html(keyword, pages)
            return

        for page in range(pages):
            try:
                payload = self.fetcher.get(
                    self.BROWSE_URL,
                    params={
                        "q": keyword,
                        "limit": "50",
                        "offset": str(page * 50),
                        "filter": "buyingOptions:{FIXED_PRICE},itemLocationCountry:US",
                    },
                    headers={
                        "Authorization": f"Bearer {token}",
                        # Pins results to the US site and US shipping estimates.
                        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                    },
                    expect_json=True,
                ).json()
            except Exception as e:
                log.warning("[ebay] Browse API page %s failed: %s", page, e)
                return

            items = payload.get("itemSummaries") or []
            if not items:
                return
            for item in items:
                offer = self._offer_from_api(item)
                if offer:
                    yield offer

    def _offer_from_api(self, item: dict) -> Optional[RawOffer]:
        item_id = str(item.get("itemId") or "")
        title = clean(item.get("title") or "")
        if not item_id or not title:
            return None

        price = item.get("price") or {}
        offer = RawOffer(
            site_key=self.key,
            site_product_id=item_id.split("|")[-2] if "|" in item_id else item_id,
            url=item.get("itemWebUrl") or f"{self.base_url}/itm/{item_id}",
            title=title,
            currency=price.get("currency") or "USD",
            price_min=_as_float(price.get("value")),
            moq=1,
            seller_name=clean(str((item.get("seller") or {}).get("username") or "")) or None,
            rating=_as_float((item.get("seller") or {}).get("feedbackPercentage")),
            review_count=_as_int((item.get("seller") or {}).get("feedbackScore")),
            category_path=" > ".join(
                c.get("categoryName", "") for c in (item.get("categories") or [])
            ) or None,
            shipping_from=(item.get("itemLocation") or {}).get("country"),
            raw={"source": "browse_api", "condition": item.get("condition")},
        )

        for opt in item.get("shippingOptions") or []:
            amount = _as_float((opt.get("shippingCost") or {}).get("value"))
            if amount is None:
                continue
            offer.shipping_cost = amount
            offer.shipping_currency = (opt.get("shippingCost") or {}).get("currency") or "USD"
            offer.shipping_free = amount == 0.0
            break

        img = (item.get("image") or {}).get("imageUrl")
        if img:
            offer.image_urls.append(img)
        for extra in (item.get("additionalImages") or [])[:5]:
            if extra.get("imageUrl"):
                offer.image_urls.append(extra["imageUrl"])

        # Condition matters enormously for a baseline: a used unit is not a
        # comparable reference for a new one straight from a factory.
        if item.get("condition"):
            offer.add_spec("Condition", str(item["condition"]))
        return offer

    def _search_html(self, keyword: str, pages: int) -> Iterator[RawOffer]:
        for page in range(1, pages + 1):
            url = self.SEARCH.format(kw=quote_plus(keyword), page=page)
            try:
                body = self.fetch_html(url)
            except Exception as e:
                log.warning("[ebay] page %s failed: %s", page, e)
                return

            tree = HTMLParser(body)
            # Verified live: eBay's current grid is .s-card / .su-card-container.
            # The older li.s-item markup is kept as a fallback.
            cards = self.select_cards(tree, [
                ".s-card", ".su-card-container", "li.s-item",
                "[class*='s-item__wrapper']",
            ])
            if not cards:
                log.info("[ebay] no cards on page %s for %r", page, keyword)
                return
            for card in cards:
                try:
                    offer = self._parse_card(card)
                    if offer:
                        yield offer
                except Exception as e:
                    log.debug("[ebay] bad card: %s", e)

    def _parse_card(self, card) -> Optional[RawOffer]:
        link = card.css_first("a.s-item__link, a[href*='/itm/']")
        url = (link.attributes.get("href") if link else None) or ""
        m = re.search(r"/itm/(\d{6,})", url)
        if not m:
            return None

        title = clean(
            self.text_of(card, ".s-card__title")
            or self.text_of(card, ".s-item__title, [class*='title']")
            or (link.text(strip=True) if link else "")
        )
        # eBay pads its grid with a literal "Shop on eBay" placeholder card.
        if not title or title.lower().startswith("shop on ebay"):
            return None

        pmin, pmax, ccy = parse_price(
            self.text_of(card, ".s-card__attribute-row")
            or self.text_of(card, ".s-item__price, [class*='price']"),
            "USD",
        )
        offer = RawOffer(
            site_key=self.key,
            site_product_id=m.group(1),
            url=url.split("?")[0],
            title=title,
            currency=ccy,
            price_min=pmin,
            price_max=pmax,
            moq=1,
            seller_name=clean(self.text_of(card, ".s-item__seller-info-text")) or None,
            raw={"source": "search_html"},
        )
        ship = self.text_of(card, ".s-item__shipping, .s-item__logisticsCost")
        if ship:
            offer.shipping_note = clean(ship)[:512]
            if "free" in ship.lower():
                offer.shipping_free = True
            else:
                cost, _, sccy = parse_price(ship, "USD")
                if cost is not None:
                    offer.shipping_cost, offer.shipping_currency = cost, sccy

        # Condition is essential for a price baseline -- a used unit is not a
        # comparable reference for a new one from a factory.
        condition = clean(self.text_of(card, ".s-card__subtitle, .SECONDARY_INFO"))
        if condition:
            offer.add_spec("Condition", condition)

        for img in card.css("img"):
            u = self.first_attr(img)
            # Skip eBay's own sprite/chrome assets.
            if u and "ebaystatic.com" not in u:
                offer.image_urls.append(u if u.startswith("http") else "https:" + u)
                break
        return offer

    def fetch_detail(self, offer: RawOffer) -> RawOffer:
        try:
            body = self.fetch_html(offer.url, phase="detail")
        except Exception as e:
            log.debug("[ebay] detail failed for %s: %s", offer.site_product_id, e)
            return offer
        tree = HTMLParser(body)
        self.apply_json_ld(offer, tree)

        for row in self.select_cards(tree, [
            ".ux-layout-section__row .ux-labels-values",
            "[class*='item-specifics'] .ux-labels-values",
            "[class*='specifications'] tr",
        ]):
            k = self.text_of(row, ".ux-labels-values__labels, th, td:first-child")
            v = self.text_of(row, ".ux-labels-values__values, td:last-child")
            if k and v and k != v and len(k) <= 60:
                offer.add_spec(k, v)

        offer.detail_fetched = True
        return offer


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    f = _as_float(value)
    return int(f) if f is not None else None
