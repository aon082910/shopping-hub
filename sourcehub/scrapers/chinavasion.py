"""Chinavasion -- a conventional Magento-style storefront.

Easiest of the eleven: no anti-bot to speak of, English, USD, single-unit orders,
and it publishes a clean spec table plus an actual SKU per product (which is a
genuinely useful matching signal -- most of these sites give you nothing).
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


class ChinavasionAdapter(SiteAdapter):
    key = "chinavasion"
    name = "Chinavasion"
    base_url = "https://www.chinavasion.com"
    home_currency = "USD"

    # /catalogsearch/ is Magento's default and 404s here; the live route is
    # /search/?q= (which redirects to /deals/<kw>). Results are client-rendered,
    # hence render: browser in config.yaml.
    SEARCH = "https://www.chinavasion.com/search/?q={kw}&page={page}"
    result_selector = "li.product-item, .item.product, [class*='product-item-info']"

    # Chinavasion's on-site search renders entirely client-side and yields nothing
    # even under a real browser, so discovery goes through the site's own product
    # sitemap instead: ~20k URLs, fetched once and cached for the run, filtered by
    # keyword against the slug. Product pages themselves are server-rendered, so
    # enrichment is ordinary HTTP.
    SITEMAPS = (
        "https://www.chinavasion.com/sitemap/product-1.xml",
        "https://www.chinavasion.com/sitemap/product-2.xml",
    )

    def __init__(self, config=None):
        super().__init__(config)
        self._sitemap: list[str] | None = None

    def _product_urls(self) -> list[str]:
        if self._sitemap is not None:
            return self._sitemap
        urls: list[str] = []
        for sm in self.SITEMAPS:
            try:
                body = self.fetcher.get(sm).text
            except Exception as e:
                log.warning("[chinavasion] sitemap %s failed: %s", sm, e)
                continue
            urls.extend(
                u for u in re.findall(r"<loc>([^<]+)</loc>", body)
                if "/wholesale/" in u
            )
        self._sitemap = urls
        log.info("[chinavasion] sitemap: %s product urls", len(urls))
        return urls

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        # Every token must appear in the slug; the slug is the product title, so this
        # is a reasonable stand-in for the search the site will not give us.
        tokens = [t for t in re.split(r"\W+", keyword.lower()) if len(t) > 1]
        if not tokens:
            return
        limit = (max_pages or self.max_pages) * 20

        matched = 0
        for url in self._product_urls():
            slug = url.rsplit("/", 1)[-1].lower()
            if not all(t in slug for t in tokens):
                continue
            offer = self._offer_from_url(url)
            if offer is None:
                continue
            matched += 1
            yield offer
            if matched >= limit:
                return
        if matched == 0:
            log.info("[chinavasion] no sitemap slugs matched %r", keyword)

    def _offer_from_url(self, url: str) -> Optional[RawOffer]:
        slug = url.rsplit("/", 1)[-1]
        m = re.search(r"-([a-z]{3}-[a-z0-9]{6,})$", slug)
        sku = m.group(1) if m else slug[:120]
        title = clean(re.sub(r"-[a-z]{3}-[a-z0-9]{6,}$", "", slug).replace("-", " ")).title()
        if not title:
            return None
        crumbs = [p for p in url.split("/wholesale/")[-1].split("/")[:-1] if p]
        return RawOffer(
            site_key=self.key,
            site_product_id=sku,
            url=url,
            title=title,
            currency="USD",
            moq=1,
            mpn=sku.upper(),
            seller_name="Chinavasion",
            category_path=" > ".join(c.replace("-", " ").title() for c in crumbs) or None,
            raw={"source": "sitemap"},
        )

    def fetch_detail(self, offer: RawOffer) -> RawOffer:
        body = self.fetch_html(offer.url, phase="detail")
        tree = HTMLParser(body)
        self.apply_json_ld(offer, tree)

        for row in tree.css("#product-attribute-specs-table tr, .additional-attributes tr, "
                            ".data.table tr"):
            k = self.text_of(row, "th, td:first-child")
            v = self.text_of(row, "td:last-child")
            if k and v and k != v:
                offer.add_spec(k, v)

        if offer.price_min is None:
            # Prices live in <span class="ccy">$19.17</span>. On this template the
            # main product's own price is injected by JS, so what remains in the HTML
            # are the related-product cards. Taking one of those would be worse than
            # useless -- a confidently wrong price attributed to the wrong product --
            # so only a price inside the main product region is accepted, and
            # otherwise the field is honestly left empty.
            region = tree.css_first(
                "#product-info, .product-info-main, .product-main, [itemprop='offers']"
            )
            node = region.css_first(".ccy") if region is not None else None
            if node is not None:
                pmin, pmax, ccy = parse_price(node.text(strip=True), "USD")
                if pmin and pmin > 0:
                    offer.price_min, offer.price_max, offer.currency = pmin, pmax, ccy

        sku = self.text_of(tree, "[itemprop='sku'], .product.attribute.sku .value")
        if sku:
            offer.mpn = offer.mpn or clean(sku)

        # Chinavasion publishes real UPC/EAN on many listings -- the single best
        # cross-site matching signal we can get.
        for label in ("upc", "ean", "gtin", "barcode"):
            node = tree.css_first(f"[data-th*='{label}' i], [class*='{label}']")
            if node:
                from ..util.text import find_gtin

                g = find_gtin(node.text())
                if g:
                    offer.gtin = g
                    break

        crumbs = [clean(a.text()) for a in tree.css(".breadcrumbs a")]
        crumbs = [c for c in crumbs if c and c.lower() != "home"]
        if crumbs:
            offer.category_path = " > ".join(crumbs[:4])

        for img in tree.css(".fotorama__img, .gallery-placeholder img, [class*='product-image'] img")[:12]:
            u = self.first_attr(img)
            if u:
                offer.image_urls.append(u)

        desc = tree.css_first("#description, .product.attribute.description, [class*='product-info']")
        if desc:
            offer.description = clean(desc.text(separator=" "))[:8000]

        ship = tree.css_first("[class*='shipping'], .shipping-info")
        if ship:
            offer.shipping_note = clean(ship.text(separator=" "))[:512]

        offer.detail_fetched = True
        return offer
