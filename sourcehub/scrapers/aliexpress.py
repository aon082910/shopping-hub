"""AliExpress.

Two drivers:
  * **api**  -- if ALIEXPRESS_APP_KEY/SECRET are set, uses the official
    affiliate/dropship API. Stable, legal, no anti-bot. Strongly preferred.
  * **html** -- parses the server-rendered ``window._dida_config_._init_data_``
    blob on the search page, falling back to DOM scraping.

AliExpress renders its search results into a JSON global rather than clean HTML,
so reading that blob is both easier and far more stable than CSS selectors.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from typing import Any, Iterator, Optional
from urllib.parse import quote

from ..config import get_settings
from ..util.money import parse_price
from ..util.text import clean
from .base import RawOffer, RawSpec, SiteAdapter

log = logging.getLogger(__name__)


class AliExpressAdapter(SiteAdapter):
    key = "aliexpress"
    name = "AliExpress"
    base_url = "https://www.aliexpress.com"
    home_currency = "USD"

    SEARCH = "https://www.aliexpress.com/w/wholesale-{kw}.html?page={page}&g=y&SearchText={kw}"
    API_ENDPOINT = "https://api-sg.aliexpress.com/sync"

    def extra_headers(self) -> dict[str, str]:
        return {"Accept-Language": "en-US,en;q=0.9", "Referer": self.base_url + "/"}

    # ------------------------------------------------------------------ search

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        s = get_settings()
        if s.aliexpress_app_key and s.aliexpress_app_secret:
            yield from self._search_api(keyword, max_pages or self.max_pages)
            return
        yield from self._search_html(keyword, max_pages or self.max_pages)

    def _search_html(self, keyword: str, max_pages: int) -> Iterator[RawOffer]:
        kw = quote(keyword.replace(" ", "-"))
        for page in range(1, max_pages + 1):
            url = self.SEARCH.format(kw=kw, page=page)
            try:
                body = self.fetcher.get(url, referer=self.base_url + "/").text
            except Exception as e:
                log.warning("[aliexpress] search page %s failed: %s", page, e)
                return

            items = self._extract_items(body)
            if not items:
                log.info("[aliexpress] no items on page %s for %r; stopping", page, keyword)
                return
            for item in items:
                try:
                    offer = self._offer_from_item(item)
                    if offer:
                        yield offer
                except Exception as e:
                    log.debug("[aliexpress] bad card: %s", e)

    def _extract_items(self, body: str) -> list[dict]:
        """Dig the item list out of whichever global AliExpress used today."""
        for blob in self.json_blobs(body, "_init_data_", "window.runParams", "runParams"):
            items = _deep_find_items(blob)
            if items:
                return items
        # DOM fallback -- much more fragile, but better than nothing.
        from selectolax.parser import HTMLParser

        tree = HTMLParser(body)
        out = []
        for card in tree.css("a[href*='/item/']"):
            href = card.attributes.get("href", "")
            m = re.search(r"/item/(\d+)\.html", href)
            if not m:
                continue
            title = card.attributes.get("title") or card.text(strip=True)
            if not title or len(title) < 8:
                continue
            out.append({
                "productId": m.group(1),
                "title": {"displayTitle": title},
                "__href": href,
                "__price_text": card.parent.text(strip=True) if card.parent else "",
            })
        # de-dupe by product id, preserving order
        seen, uniq = set(), []
        for it in out:
            pid = it["productId"]
            if pid not in seen:
                seen.add(pid)
                uniq.append(it)
        return uniq

    def _offer_from_item(self, item: dict) -> Optional[RawOffer]:
        pid = str(item.get("productId") or item.get("product_id") or item.get("itemId") or "")
        if not pid.isdigit():
            return None

        title = (
            _dig(item, "title", "displayTitle")
            or _dig(item, "title", "seoTitle")
            or item.get("productTitle")
            or item.get("subject")
            or ""
        )
        title = clean(title)
        if not title:
            return None

        # Prefer the structured numbers over the formatted string: "US $4.79" has to
        # be re-parsed and its currency guessed, while minPrice/currencyCode are
        # already typed and correct. Fall back to the string only if they are absent.
        sale = _dig(item, "prices", "salePrice") or {}
        original = _dig(item, "prices", "originalPrice") or {}
        pmin = _as_float(sale.get("minPrice"))
        ccy = sale.get("currencyCode") or original.get("currencyCode") or "USD"
        # The pre-discount price is the honest ceiling for this listing.
        pmax = _as_float(original.get("minPrice"))
        if pmax is not None and pmin is not None and pmax <= pmin:
            pmax = None

        if pmin is None:
            price_text = (
                sale.get("formattedPrice")
                or original.get("formattedPrice")
                or item.get("__price_text")
                or ""
            )
            pmin, pmax, ccy = parse_price(str(price_text), "USD")

        offer = RawOffer(
            site_key=self.key,
            site_product_id=pid,
            url=f"{self.base_url}/item/{pid}.html",
            title=title,
            currency=ccy,
            price_min=pmin,
            price_max=pmax,
            rating=_as_float(_dig(item, "evaluation", "starRating")),
            orders_count=self.parse_int(str(_dig(item, "trade", "tradeDesc") or "")),
            seller_name=clean(str(_dig(item, "store", "storeName") or "")) or None,
            seller_url=self.abs_url(_dig(item, "store", "storeUrl")),
            raw={"source": "search"},
        )

        ship = _dig(item, "sellingPoints") or []
        notes = [p.get("tagContent", {}).get("tagText", "") for p in ship if isinstance(p, dict)]
        note = " | ".join(n for n in notes if n)
        if note:
            offer.shipping_note = clean(note)[:512]
            if "free shipping" in note.lower():
                offer.shipping_free = True

        img = (
            _dig(item, "image", "imgUrl")
            or item.get("imageUrl")
            or item.get("productImage")
        )
        if img:
            offer.image_urls.append(_https(img))
        for extra in (_dig(item, "images") or [])[:5]:
            if isinstance(extra, str):
                url = _https(extra)
                if url and url not in offer.image_urls:
                    offer.image_urls.append(url)
        return offer

    # ------------------------------------------------------------------ detail

    def fetch_detail(self, offer: RawOffer) -> RawOffer:
        body = self.fetch_html(offer.url, phase="detail")

        data = None
        for blob in self.json_blobs(body, "window.runParams", "_init_data_", "runParams"):
            if isinstance(blob, dict):
                data = blob.get("data") if "data" in blob else blob
                if isinstance(data, dict) and (
                    "priceComponent" in data or "skuComponent" in data or "productInfoComponent" in data
                ):
                    break
        if isinstance(data, dict):
            self._detail_from_json(offer, data)

        if not offer.specs or not offer.image_urls:
            self._detail_from_dom(offer, body)

        offer.detail_fetched = True
        return offer

    def _detail_from_json(self, offer: RawOffer, data: dict) -> None:
        info = data.get("productInfoComponent") or {}
        offer.model = offer.model or clean(str(info.get("subject") or "")) or None
        cat = info.get("categoryPaths") or info.get("categoryId")
        if isinstance(cat, str):
            offer.category_path = cat

        price = data.get("priceComponent") or {}
        txt = (
            _dig(price, "discountPrice", "minActivityAmount", "formatedAmount")
            or _dig(price, "origPrice", "minAmount", "formatedAmount")
        )
        if txt:
            pmin, pmax, ccy = parse_price(str(txt), offer.currency)
            if pmin is not None:
                offer.price_min, offer.price_max, offer.currency = pmin, pmax, ccy

        ship = data.get("webGeneralFreightCalculateComponent") or {}
        first = _dig(ship, "originalLayoutResultList", 0, "bizData") or {}
        if first:
            amount = first.get("displayAmount")
            if amount is not None:
                offer.shipping_cost = float(amount)
                offer.shipping_currency = first.get("currency") or "USD"
                offer.shipping_free = float(amount) == 0.0
            offer.shipping_from = first.get("shipFrom") or offer.shipping_from
            note = first.get("deliveryDateFormat") or first.get("company")
            if note:
                offer.shipping_note = clean(str(note))[:512]

        for group in (data.get("productPropComponent") or {}).get("props", []) or []:
            offer.add_spec(str(group.get("attrName", "")), str(group.get("attrValue", "")))

        imgs = _dig(data, "imageComponent", "imagePathList") or []
        for u in imgs[:12]:
            if isinstance(u, str):
                offer.image_urls.append(_https(u))

        store = data.get("sellerComponent") or {}
        offer.seller_name = offer.seller_name or clean(str(store.get("storeName") or "")) or None
        offer.seller_url = offer.seller_url or self.abs_url(store.get("storeURL"))

    def _detail_from_dom(self, offer: RawOffer, body: str) -> None:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(body)
        for row in tree.css("[class*='specification--prop'], .product-prop, li.property-item"):
            title = row.css_first("[class*='title'], .property-title")
            value = row.css_first("[class*='desc'], .property-desc")
            if title and value:
                offer.add_spec(title.text(strip=True), value.text(strip=True))
        if not offer.image_urls:
            for img in tree.css("[class*='slider--img'] img, .images-view-item img")[:12]:
                u = self.first_attr(img)
                if u:
                    offer.image_urls.append(_https(u))
        if not offer.description:
            desc = tree.css_first("#product-description, .detail-desc-decorate-richtext")
            if desc:
                offer.description = clean(desc.text())[:8000]

    # --------------------------------------------------------------- official API

    def _search_api(self, keyword: str, max_pages: int) -> Iterator[RawOffer]:
        s = get_settings()
        for page in range(1, max_pages + 1):
            params = {
                "app_key": s.aliexpress_app_key,
                "method": "aliexpress.affiliate.product.query",
                "sign_method": "hmac-sha256",
                "timestamp": str(int(time.time() * 1000)),
                "format": "json",
                "v": "2.0",
                "keywords": keyword,
                "page_no": str(page),
                "page_size": "50",
                "target_currency": "USD",
                "target_language": "EN",
                "ship_to_country": "US",
                "tracking_id": s.aliexpress_tracking_id or "",
            }
            params["sign"] = _sign_top(params, s.aliexpress_app_secret)
            try:
                resp = self.fetcher.get(self.API_ENDPOINT, params=params, expect_json=True)
                payload = resp.json()
            except Exception as e:
                log.warning("[aliexpress] API page %s failed: %s", page, e)
                return

            products = (
                _dig(payload, "aliexpress_affiliate_product_query_response", "resp_result",
                     "result", "products", "product")
                or []
            )
            if not products:
                return
            for p in products:
                pid = str(p.get("product_id") or "")
                if not pid:
                    continue
                pmin, pmax, ccy = parse_price(
                    str(p.get("target_sale_price") or p.get("sale_price") or ""), "USD"
                )
                offer = RawOffer(
                    site_key=self.key,
                    site_product_id=pid,
                    url=p.get("product_detail_url") or f"{self.base_url}/item/{pid}.html",
                    title=clean(p.get("product_title") or ""),
                    currency=p.get("target_sale_price_currency") or ccy,
                    price_min=pmin,
                    price_max=pmax,
                    rating=self.parse_float(str(p.get("evaluate_rate") or "")),
                    orders_count=self.parse_int(str(p.get("lastest_volume") or "")),
                    seller_name=p.get("shop_name"),
                    seller_url=p.get("shop_url"),
                    category_path=str(p.get("second_level_category_name") or ""),
                    image_urls=[_https(u) for u in [p.get("product_main_image_url")] if u],
                    raw={"source": "api"},
                )
                for u in (p.get("product_small_image_urls", {}) or {}).get("string", [])[:8]:
                    offer.image_urls.append(_https(u))
                yield offer


# ------------------------------------------------------------------- helpers


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _https(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    return url


def _dig(obj: Any, *path: Any) -> Any:
    """Safe nested lookup across dicts and lists."""
    cur = obj
    for p in path:
        if cur is None:
            return None
        if isinstance(p, int):
            if not isinstance(cur, (list, tuple)) or len(cur) <= p:
                return None
            cur = cur[p]
        else:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(p)
    return cur


def _deep_find_items(blob: Any, depth: int = 0) -> list[dict]:
    """AliExpress moves the item array around between releases; find it by shape."""
    if depth > 8 or blob is None:
        return []
    if isinstance(blob, list):
        if blob and isinstance(blob[0], dict) and (
            "productId" in blob[0] or "product_id" in blob[0]
        ):
            return [b for b in blob if isinstance(b, dict)]
        for entry in blob[:20]:
            found = _deep_find_items(entry, depth + 1)
            if found:
                return found
        return []
    if isinstance(blob, dict):
        for name in ("itemList", "items", "content", "products", "mods"):
            if name in blob:
                found = _deep_find_items(blob[name], depth + 1)
                if found:
                    return found
        for v in list(blob.values())[:40]:
            found = _deep_find_items(v, depth + 1)
            if found:
                return found
    return []


def _sign_top(params: dict[str, str], secret: str) -> str:
    """Alibaba/AliExpress TOP gateway HMAC-SHA256 signature."""
    payload = "".join(f"{k}{params[k]}" for k in sorted(params) if params[k] != "")
    return hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest().upper()
