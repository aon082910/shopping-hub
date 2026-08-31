"""Banggood and GearBest.

Both are conventional retail storefronts on the same lineage of template, so one
implementation with a subclass covers them. Banggood exposes clean JSON-LD
(``schema.org/Product``) on product pages -- when a site gives you structured data,
use it; it survives redesigns that break every CSS selector.

GearBest note: you pointed at ``gearbest.ma``, the Morocco storefront, which prices
in MAD. FX conversion handles that, but be aware its catalog is far smaller than
the other ten and many searches legitimately return nothing.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterator, Optional
from urllib.parse import quote_plus

from selectolax.parser import HTMLParser

from ..util.money import parse_price
from ..util.text import clean, find_gtin
from .base import RawOffer, SiteAdapter

log = logging.getLogger(__name__)


class _RetailStoreAdapter(SiteAdapter):
    """Shared logic: search grid -> cards, product page -> JSON-LD + spec table."""

    search_template = ""
    card_selectors = (
        ".product-item, .goodlist_item, li[class*='product'], .p-wrap, .goods-item"
    )
    # Cards are server-rendered but the price nodes arrive empty and are filled by
    # XHR, so waiting on a populated price is what makes browser rendering worth it.
    result_selector = ".p-wrap .price, .product-item .price, [class*='price']"

    def extra_headers(self) -> dict[str, str]:
        return {"Referer": self.base_url + "/", "Accept-Language": "en-US,en;q=0.9"}

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        for page in range(1, (max_pages or self.max_pages) + 1):
            url = self.search_template.format(kw=quote_plus(keyword), page=page)
            try:
                body = self.fetch_html(url)
            except Exception as e:
                log.warning("[%s] page %s failed: %s", self.key, page, e)
                return

            tree = HTMLParser(body)
            cards = self.select_cards(tree, self.card_selectors)
            if not cards:
                log.info("[%s] no cards on page %s for %r", self.key, page, keyword)
                return
            for card in cards:
                try:
                    offer = self._parse_card(card)
                    if offer:
                        yield offer
                except Exception as e:
                    log.debug("[%s] bad card: %s", self.key, e)

    def _parse_card(self, card) -> Optional[RawOffer]:
        link = card.css_first("a[href*='-p-'], a.title, a[class*='title'], h3 a, a")
        url = self.abs_url(link.attributes.get("href") if link else None)
        if not url or url.rstrip("/") == self.base_url.rstrip("/"):
            return None

        title = clean(
            (link.attributes.get("title") if link else "")
            or self.text_of(card, "[class*='title'], .title, h3")
            or (link.text(strip=True) if link else "")
        )
        if not title or len(title) < 6:
            return None

        pid = self._product_id(url, card)
        if not pid:
            return None

        pmin, pmax, ccy = parse_price(
            self.text_of(card, "[class*='price'], .price, .goods-price"), self.home_currency
        )
        offer = RawOffer(
            site_key=self.key,
            site_product_id=pid,
            url=url.split("?")[0],
            title=title,
            currency=ccy,
            price_min=pmin,
            price_max=pmax,
            moq=1,
            seller_name=self.name,
            rating=self.parse_float(self.text_of(card, "[class*='rating'], [class*='star']")),
            review_count=self.parse_int(self.text_of(card, "[class*='review'], [class*='comment']")),
            raw={"source": "search"},
        )
        ship = self.text_of(card, "[class*='shipping'], [class*='freight']")
        if ship:
            offer.shipping_note = clean(ship)[:512]
            offer.shipping_free = "free" in ship.lower()

        u = self.first_attr(card.css_first("img"))
        if u:
            offer.image_urls.append(u if u.startswith("http") else "https:" + u)
        return offer

    def _product_id(self, url: str, card) -> Optional[str]:
        for pattern in (r"-p-(\d+)", r"/(\d{6,})\.html", r"[?&](?:id|goods_id)=(\d+)"):
            m = re.search(pattern, url)
            if m:
                return m.group(1)
        for attr in ("data-product-id", "data-id", "data-spu"):
            v = card.attributes.get(attr)
            if v:
                return str(v)
        return None

    # ------------------------------------------------------------------ detail

    def fetch_detail(self, offer: RawOffer) -> RawOffer:
        body = self.fetch_html(offer.url, phase="detail")
        tree = HTMLParser(body)

        self.apply_json_ld(offer, tree)

        # Verified against a captured page: the attribute table is a plain <tr> grid
        # under a class containing "spec". The narrower selectors below are kept as
        # fallbacks for other storefront versions.
        for row in self.select_cards(tree, [
            "[class*='spec'] tr", ".product-specification tr", "[class*='parameter'] tr",
            "#specification tr", ".spec-list li", ".goods-attr li",
        ]):
            k = self.text_of(row, "th, td:first-child, .name, .key")
            v = self.text_of(row, "td:last-child, .value, .val")
            if k and v and k != v and len(k) <= 60:
                offer.add_spec(k, v)
                if k.strip().lower() == "brand":
                    offer.brand = offer.brand or self.plausible_brand(v)

        ship = tree.css_first("[class*='shipping'], #shipping, .delivery-info")
        if ship:
            txt = clean(ship.text(separator=" "))[:512]
            offer.shipping_note = txt
            if "free shipping" in txt.lower():
                offer.shipping_free = True
            else:
                cost, _, ccy = parse_price(txt, self.home_currency)
                if cost is not None:
                    offer.shipping_cost, offer.shipping_currency = cost, ccy

        crumbs = [clean(a.text()) for a in tree.css("[class*='breadcrumb'] a, .crumbs a")]
        crumbs = [c for c in crumbs if c and c.lower() not in ("home", self.name.lower())]
        if crumbs:
            offer.category_path = " > ".join(crumbs[:4])

        if len(offer.image_urls) < 2:
            for img in tree.css("[class*='gallery'] img, .product-image img, #j-detail-gallery img")[:12]:
                u = self.first_attr(img)
                if u:
                    offer.image_urls.append(u if u.startswith("http") else "https:" + u)

        offer.detail_fetched = True
        return offer


class BanggoodAdapter(_RetailStoreAdapter):
    key = "banggood"
    name = "Banggood"
    base_url = "https://www.banggood.com"
    home_currency = "USD"
    search_template = "https://www.banggood.com/search/{kw}/{page}.html?from=nav"


class GearBestAdapter(_RetailStoreAdapter):
    key = "gearbest"
    name = "GearBest"
    base_url = "https://www.gearbest.ma"
    home_currency = "MAD"
    search_template = "https://www.gearbest.ma/index.php?route=product/search&search={kw}&page={page}"
    card_selectors = ".product-layout, .product-thumb, .product-grid .product-layout"

    def _product_id(self, url: str, card) -> Optional[str]:
        m = re.search(r"product_id=(\d+)", url)
        if m:
            return m.group(1)
        return super()._product_id(url, card) or _slug_id(url)


# ------------------------------------------------------------------- helpers


def _json_ld_product(tree: HTMLParser) -> Optional[dict]:
    """Find the schema.org Product node, including inside @graph wrappers."""
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except Exception:
            continue
        for candidate in _iter_ld(data):
            if str(candidate.get("@type", "")).lower() in ("product", "individualproduct"):
                return candidate
    return None


def _iter_ld(data: Any) -> Iterator[dict]:
    if isinstance(data, dict):
        if "@graph" in data:
            for entry in data["@graph"]:
                yield from _iter_ld(entry)
        yield data
    elif isinstance(data, list):
        for entry in data:
            yield from _iter_ld(entry)


def _ld_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        return clean(str(value.get("name") or value.get("@id") or "")) or None
    return clean(str(value)) or None


def _as_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> Optional[int]:
    f = _as_float(v)
    return int(f) if f is not None else None


def _slug_id(url: str) -> Optional[str]:
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return tail.replace(".html", "")[:120] or None
