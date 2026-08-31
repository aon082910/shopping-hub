"""Global Sources -- B2B supplier directory.

Caveat that matters for comparison: a large share of Global Sources listings quote
"Price on request" / "Negotiable" rather than a number. Those are ingested with a
null price so they still appear on the item page as a sourcing option, but they are
excluded from best-price rollups instead of being silently treated as $0.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator, Optional
from urllib.parse import quote_plus

from selectolax.parser import HTMLParser

from ..util.money import parse_moq, parse_price, parse_tiers
from ..util.text import clean
from .base import RawOffer, RawTier, SiteAdapter

log = logging.getLogger(__name__)

NEGOTIABLE = re.compile(r"negotiab|on request|inquire|contact suppl|面议", re.I)


class GlobalSourcesAdapter(SiteAdapter):
    key = "globalsources"
    name = "Global Sources"
    base_url = "https://www.globalsources.com"
    home_currency = "USD"

    SEARCH = "https://www.globalsources.com/searchList/products?keyWord={kw}&pageNum={page}"

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        for page in range(1, (max_pages or self.max_pages) + 1):
            url = self.SEARCH.format(kw=quote_plus(keyword), page=page)
            try:
                body = self.fetch_html(url)
            except Exception as e:
                log.warning("[globalsources] page %s failed: %s", page, e)
                return

            tree = HTMLParser(body)
            # `li[class*='item']` used to be in this list and matched the site's
            # navigation, so the crawler happily ingested "Magazines" as a product.
            # A too-broad fallback that silently produces junk is worse than no
            # fallback: the junk reaches the catalog and has to be cleaned out.
            cards = self.select_cards(tree, [
                "[class*='product-item']", ".prod-item", "div[data-product-id]",
            ])
            if not cards:
                return
            for card in cards:
                try:
                    offer = self._parse_card(card)
                    if offer:
                        yield offer
                except Exception as e:
                    log.debug("[globalsources] bad card: %s", e)

    def _parse_card(self, card) -> Optional[RawOffer]:
        link = card.css_first("a[href*='/product/'], a[href*='pdtl'], h2 a, a[class*='title']")
        url = self.abs_url(link.attributes.get("href") if link else None)
        # The URL has to look like a product page. Without this a card that is
        # really a nav block still yields a plausible-looking offer.
        if not url or not any(m in url for m in ("/product/", "/pdtl", "/manufacturers/")):
            return None

        title = clean(
            (link.attributes.get("title") if link else "")
            or self.text_of(card, "[class*='title'], h2")
            or (link.text(strip=True) if link else "")
        )
        if not title:
            return None

        pid = (
            card.attributes.get("data-product-id")
            or (re.search(r"(\d{7,})", url).group(1) if re.search(r"(\d{7,})", url) else None)
            or url.rstrip("/").rsplit("/", 1)[-1]
        )

        price_text = self.text_of(card, "[class*='price']")
        pmin, pmax, ccy = (None, None, "USD")
        if price_text and not NEGOTIABLE.search(price_text):
            pmin, pmax, ccy = parse_price(price_text, "USD")

        moq, unit = parse_moq(self.text_of(card, "[class*='moq'], [class*='min-order']"))

        offer = RawOffer(
            site_key=self.key,
            site_product_id=str(pid)[:120],
            url=url.split("?")[0],
            title=title,
            currency=ccy,
            price_min=pmin,
            price_max=pmax,
            moq=moq,
            moq_unit=unit,
            seller_name=clean(self.text_of(card, "[class*='supplier'], [class*='company']")) or None,
            is_verified_supplier=bool(card.css_first("[class*='verified'], [class*='audited']")),
            raw={"source": "search", "price_text": price_text},
        )
        if price_text and NEGOTIABLE.search(price_text):
            offer.fees_note = "Price not published - supplier quotes on request."

        u = self.first_attr(card.css_first("img"))
        if u:
            offer.image_urls.append(u if u.startswith("http") else "https:" + u)
        return offer

    def fetch_detail(self, offer: RawOffer) -> RawOffer:
        body = self.fetch_html(offer.url, phase="detail")
        tree = HTMLParser(body)
        self.apply_json_ld(offer, tree)

        for row in tree.css("[class*='spec'] tr, [class*='attribute'] li, .prod-spec-item, dl.spec dd"):
            txt = clean(row.text(separator=" | "))
            if "|" in txt:
                k, _, v = txt.partition("|")
                offer.add_spec(k, v)
            else:
                k = self.text_of(row, "th, dt, .label")
                v = self.text_of(row, "td, dd, .value")
                if k and v and k != v:
                    offer.add_spec(k, v)

        ladder = tree.css_first("[class*='ladder'], [class*='price-table'], [class*='fob']")
        if ladder:
            for tier in parse_tiers(ladder.text(separator=" "), offer.currency):
                offer.tiers.append(RawTier(**tier))
        if offer.tiers:
            offer.moq = min(t.min_qty for t in offer.tiers)
            offer.price_min = min(t.price for t in offer.tiers)
            offer.price_max = max(t.price for t in offer.tiers)

        # Global Sources quotes FOB, so freight is explicitly the buyer's cost.
        offer.fees_note = (offer.fees_note or "") + (
            " Prices are typically quoted FOB China - ocean/air freight, duty and "
            "customs clearance are additional and not included."
        )
        offer.fees_note = offer.fees_note.strip()[:512]

        lead = tree.css_first("[class*='lead-time'], [class*='delivery']")
        if lead:
            d = self.parse_int(lead.text())
            if d and d < 400:
                offer.lead_time_days = d

        crumbs = [clean(a.text()) for a in tree.css("[class*='breadcrumb'] a")]
        crumbs = [c for c in crumbs if c and c.lower() not in ("home", "global sources")]
        if crumbs:
            offer.category_path = " > ".join(crumbs[:4])

        for img in tree.css("[class*='gallery'] img, [class*='main-image'] img")[:12]:
            u = self.first_attr(img)
            if u:
                offer.image_urls.append(u if u.startswith("http") else "https:" + u)

        offer.detail_fetched = True
        return offer
