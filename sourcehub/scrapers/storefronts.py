"""Tomtop and Geekbuying -- conventional China-based retail storefronts.

Both are ordinary server-rendered shops with JSON-LD on their product pages, so
they need almost no bespoke code: the shared base class already handles card
selection, price parsing and structured data. They are worth having because they
price in USD, ship to the US directly, and overlap heavily with Banggood's catalog,
which gives the matcher more chances to find the same product twice.

Deliberately *not* included here:

* **Temu** -- aggressive bot detection with device fingerprinting and a signed
  request scheme. Adding a stub that always returns zero would be worse than
  omitting it, because it would sit in the health report looking like a regression.
* **JD.com / Pinduoduo** -- gated much like Taobao. Route them through the browser
  or provider drivers in ``taobao_family`` rather than a new HTTP adapter.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator, Optional
from urllib.parse import quote_plus

from selectolax.parser import HTMLParser

from ..util.money import parse_price
from ..util.text import clean
from .base import RawOffer, SiteAdapter

log = logging.getLogger(__name__)


class _StorefrontAdapter(SiteAdapter):
    """Shared logic for a plain retail storefront."""

    card_selectors: list = []
    link_selector = "a[href]"
    title_selector = "[class*='title'], [class*='name'], h3, h2"
    price_selector = "[class*='price']"
    id_patterns: tuple = (r"/(\d{5,})\.html", r"[?&](?:id|product_id|goods_id)=(\d+)")

    def extra_headers(self) -> dict:
        return {"Referer": self.base_url + "/", "Accept-Language": "en-US,en;q=0.9"}

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        for page in range(1, (max_pages or self.max_pages) + 1):
            url = self.search_page_url(keyword, page)
            offers = self._cards_at(url, page, context=repr(keyword))
            if offers is None:
                return
            yield from offers

    def category_page_url(self, seed_url: str, page: int) -> str:
        """Listing URL for page N of one category_seeds() entry.

        Overridden per site: a category's own pagination URL is rarely the same
        shape as its search box's (see GeekbuyingAdapter).
        """
        return seed_url

    def crawl_category(self, seed_url: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        for page in range(1, (max_pages or 50) + 1):
            url = seed_url if page == 1 else self.category_page_url(seed_url, page)
            offers = self._cards_at(url, page, context=seed_url)
            if offers is None:
                return
            yield from offers

    def _cards_at(self, url: str, page: int, *, context: str) -> Optional[list[RawOffer]]:
        """Every offer on one listing page, or None once there is no such page.

        None (not just an empty list) is the "stop paging" signal, so a page
        that loaded fine but whose cards all failed to parse doesn't get
        mistaken for having run off the end of the listing.
        """
        try:
            body = self.fetch_html(url)
        except Exception as e:
            log.warning("[%s] page %s failed: %s", self.key, page, e)
            return None

        tree = HTMLParser(body)
        cards = self.select_cards(tree, self.card_selectors)
        if not cards:
            log.info("[%s] no cards on page %s for %s", self.key, page, context)
            return None

        offers = []
        for card in cards:
            try:
                offer = self._parse_card(card)
                if offer:
                    offers.append(offer)
            except Exception as e:
                log.debug("[%s] bad card: %s", self.key, e)
        return offers

    def _product_id(self, url: str, card) -> Optional[str]:
        for pattern in self.id_patterns:
            m = re.search(pattern, url)
            if m:
                return m.group(1)
        for attr in ("data-id", "data-product-id", "data-goods-id", "data-spu"):
            if card.attributes.get(attr):
                return str(card.attributes[attr])
        # Fall back to the slug: stable enough to dedupe on, which is all it needs
        # to do, and better than dropping an otherwise usable listing.
        tail = url.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
        return tail.replace(".html", "")[:120] or None

    def _parse_card(self, card) -> Optional[RawOffer]:
        link = card.css_first(self.link_selector)
        url = self.abs_url(link.attributes.get("href") if link else None)
        if not url or url.rstrip("/") == self.base_url.rstrip("/"):
            return None

        title = clean(
            (link.attributes.get("title") if link else "")
            or self.text_of(card, self.title_selector)
            or (link.text(strip=True) if link else "")
        )
        if not title or len(title) < 6:
            return None

        pid = self._product_id(url, card)
        if not pid:
            return None

        pmin, pmax, ccy = parse_price(
            self.text_of(card, self.price_selector), self.home_currency
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
        u = self.first_attr(card.css_first("img"))
        if u:
            offer.image_urls.append(u if u.startswith("http") else "https:" + u)
        return offer

    def fetch_detail(self, offer: RawOffer) -> RawOffer:
        try:
            body = self.fetch_html(offer.url, phase="detail")
        except Exception as e:
            log.debug("[%s] detail failed for %s: %s", self.key, offer.site_product_id, e)
            return offer
        tree = HTMLParser(body)
        self.apply_json_ld(offer, tree)

        for row in self.select_cards(tree, [
            "[class*='spec'] tr", "[class*='parameter'] tr", "[class*='attribute'] tr",
            "[class*='spec'] li", ".product-params li",
        ]):
            k = self.text_of(row, "th, td:first-child, .name, .key, dt")
            v = self.text_of(row, "td:last-child, .value, .val, dd")
            if k and v and k != v and len(k) <= 60:
                offer.add_spec(k, v)

        crumbs = [clean(a.text()) for a in tree.css("[class*='breadcrumb'] a, .crumbs a")]
        crumbs = [c for c in crumbs if c and c.lower() not in ("home", self.name.lower())]
        if crumbs:
            offer.category_path = " > ".join(crumbs[:4])

        offer.detail_fetched = True
        return offer


class TomtopAdapter(_StorefrontAdapter):
    key = "tomtop"
    name = "TOMTOP"
    base_url = "https://www.tomtop.com"
    home_currency = "USD"
    search_template = "https://www.tomtop.com/search.html?keywords={kw}&page={page}"
    card_selectors = [".product-item", ".goods-item", "li[class*='product']", ".item-box"]
    result_selector = ".product-item, .goods-item"


class GeekbuyingAdapter(_StorefrontAdapter):
    key = "geekbuying"
    name = "Geekbuying"
    base_url = "https://www.geekbuying.com"
    home_currency = "USD"
    search_template = "https://www.geekbuying.com/search/{kw}?page={page}"
    # Verified live: the search grid uses .searchResultItem.
    card_selectors = [".searchResultItem", ".goodsItem", ".product-item",
                      "li[class*='goods']"]
    result_selector = ".searchResultItem"

    # Confirmed live 2026-09-08: /sitemap/product.xml 404s despite being listed
    # in sitemap.xml, so category pages are the only path to a full catalog.
    # Page 1 is the bare /category/<slug>-<id> URL; page N>1 is
    # /category/<slug>-<id>/<N>-40-3-0-0-0-grid-0-all-0.html (40 items/page,
    # confirmed distinct products across pages 1 and 2 of a live category).
    CATEGORY_LINK_RE = re.compile(r'href="(/category/([A-Za-z0-9][A-Za-z0-9_-]*-\d+))"')

    def category_seeds(self) -> list[tuple[str, str]]:
        try:
            html = self.fetch_html(self.base_url + "/")
        except Exception as e:
            log.warning("[geekbuying] category discovery failed: %s", e)
            return []
        seen: dict[str, str] = {}
        for m in self.CATEGORY_LINK_RE.finditer(html):
            path = m.group(1)
            if path in seen:
                continue
            label = clean(re.sub(r"-\d+$", "", path.rsplit("/", 1)[-1]).replace("-", " "))
            seen[path] = label
        if not seen:
            log.warning("[geekbuying] no /category/ links found on the homepage -- "
                        "its nav markup has likely changed.")
        return [(label, self.base_url + path) for path, label in seen.items()]

    def category_page_url(self, seed_url: str, page: int) -> str:
        return f"{seed_url}/{page}-40-3-0-0-0-grid-0-all-0.html"
