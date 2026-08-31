"""Alibaba.com (B2B, English, USD).

The important extra dimension here vs retail sites: **MOQ and tiered pricing**.
A listing that says "$1.20 - $3.40 / piece, Min. Order 500 pieces" is not comparable
to a $3.99 AliExpress retail listing until you record both the ladder and the MOQ,
which is exactly what this adapter pulls.
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


class AlibabaAdapter(SiteAdapter):
    key = "alibaba"
    name = "Alibaba.com"
    base_url = "https://www.alibaba.com"
    home_currency = "USD"

    SEARCH = "https://www.alibaba.com/trade/search?SearchText={kw}&page={page}&IndexArea=product_en"

    def extra_headers(self) -> dict[str, str]:
        return {"Accept-Language": "en-US,en;q=0.9", "Referer": self.base_url + "/"}

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        pages = max_pages or self.max_pages
        for page in range(1, pages + 1):
            url = self.SEARCH.format(kw=quote_plus(keyword), page=page)
            try:
                body = self.fetcher.get(url, referer=self.base_url + "/").text
            except Exception as e:
                log.warning("[alibaba] page %s failed: %s", page, e)
                return

            cards = self._cards(body)
            if not cards:
                log.info("[alibaba] no cards on page %s for %r", page, keyword)
                return
            for card in cards:
                try:
                    offer = self._parse_card(card)
                    if offer:
                        yield offer
                except Exception as e:
                    log.debug("[alibaba] bad card: %s", e)

    def _cards(self, body: str) -> list:
        """Locate result cards across Alibaba's several grid generations.

        Their markup is versioned and changes often -- the current build uses
        ``searchx-*`` classes. Rather than chase the container class, the last
        resort walks up from the product links themselves, which is the one thing
        that has to exist on a results page whatever it is called this quarter.
        """
        tree = HTMLParser(body)
        cards = self.select_cards(tree, [
            ".searchx-offer-item",
            ".searchx-product-link-wrapper",
            "div.fy23-search-card",
            "div[data-content='productItem']",
            ".organic-list .list-no-v2-outter",
            ".J-offer-wrapper",
            ".gallery-offer-item",
        ])
        if cards:
            return cards

        seen, out = set(), []
        for link in tree.css("a[href*='/product-detail/']"):
            node = link
            # Climb to a container big enough to hold the price and MOQ text.
            for _ in range(4):
                if node.parent is None:
                    break
                node = node.parent
                if len(node.text(strip=True) or "") > 40:
                    break
            key = (node.attributes.get("class") or "") + (link.attributes.get("href") or "")
            if key not in seen:
                seen.add(key)
                out.append(node)
        return out

    def _parse_card(self, card) -> Optional[RawOffer]:
        link = card.css_first("a[href*='/product-detail/'], h2 a, .elements-title-normal__content")
        href = (link.attributes.get("href") if link else None) or ""
        url = self.abs_url(href)
        if not url or "/product-detail/" not in url:
            return None

        m = re.search(r"_(\d{6,})\.html", url) or re.search(r"/(\d{8,})\.html", url)
        pid = m.group(1) if m else url.rsplit("/", 1)[-1].split(".")[0]

        title = clean(
            (link.attributes.get("title") if link else "")
            or self.text_of(card, "h2")
            or self.text_of(card, ".elements-title-normal__content")
            or (link.text(strip=True) if link else "")
        )
        if not title:
            return None

        price_text = (
            self.text_of(card, ".elements-offer-price-normal")
            or self.text_of(card, "[class*='price']")
        )
        pmin, pmax, ccy = parse_price(price_text, "USD")

        moq_text = (
            self.text_of(card, ".element-offer-minorder-normal")
            or self.text_of(card, "[class*='minorder'], [class*='min-order']")
        )
        moq, unit = parse_moq(moq_text)

        offer = RawOffer(
            site_key=self.key,
            site_product_id=str(pid),
            url=url.split("?")[0],
            title=title,
            currency=ccy,
            price_min=pmin,
            price_max=pmax,
            moq=moq,
            moq_unit=unit,
            seller_name=clean(
                self.text_of(card, ".search-card-e-company")
                or self.text_of(card, "[class*='company-name']")
            ) or None,
            rating=self.parse_float(self.text_of(card, "[class*='review'], [class*='rating']")),
            is_verified_supplier=bool(
                card.css_first("[class*='verified'], [class*='gold-supplier'], .icbu-supplier-icon")
            ),
            raw={"source": "search"},
        )

        years = self.text_of(card, "[class*='year'], .search-card-e-supplier__year")
        offer.seller_years = self.parse_int(years)

        img = card.css_first("img")
        u = self.first_attr(img)
        if u:
            offer.image_urls.append(u if u.startswith("http") else "https:" + u)
        return offer

    # ------------------------------------------------------------------ detail

    def fetch_detail(self, offer: RawOffer) -> RawOffer:
        body = self.fetcher.get(offer.url, referer=self.base_url + "/").text
        tree = HTMLParser(body)
        self.apply_json_ld(offer, tree)

        # Price ladder table: "1 - 99 Pieces | $2.30"
        ladder = tree.css_first(
            ".ma-ladder-price, .ladder-price, [class*='price-list'], .product-price-tiers"
        )
        if ladder:
            for tier in parse_tiers(ladder.text(separator=" "), offer.currency):
                offer.tiers.append(RawTier(**tier))
        if not offer.tiers:
            for row in tree.css("[class*='price-item'], .ladder-price-item"):
                qty = self.text_of(row, "[class*='quantity'], .ladder-price-item-quantity")
                pr = self.text_of(row, "[class*='price'], .ladder-price-item-price")
                lo_hi = re.findall(r"\d[\d,]*", qty.replace(",", ""))
                p, _, ccy = parse_price(pr, offer.currency)
                if lo_hi and p is not None:
                    offer.tiers.append(
                        RawTier(
                            min_qty=int(lo_hi[0]),
                            max_qty=int(lo_hi[1]) if len(lo_hi) > 1 else None,
                            price=p,
                            currency=ccy,
                        )
                    )
        if offer.tiers:
            offer.moq = min(t.min_qty for t in offer.tiers)
            offer.price_min = min(t.price for t in offer.tiers)
            offer.price_max = max(t.price for t in offer.tiers)

        # Attribute table
        for row in tree.css(
            ".do-entry-item, .attribute-item, [class*='product-attribute'] tr, .ma-attribute-item"
        ):
            k = self.text_of(row, ".attr-name, .do-entry-label, th, td:first-child")
            v = self.text_of(row, ".attr-value, .do-entry-list-value, td:last-child")
            if k and v and k != v:
                offer.add_spec(k, v)

        # Shipping / lead time, when the listing discloses it
        ship = tree.css_first("[class*='shipping'], .logistics-info, .ma-shipping")
        if ship:
            txt = clean(ship.text(separator=" "))[:512]
            offer.shipping_note = txt or offer.shipping_note
            cost, _, ccy = parse_price(txt, "USD")
            if cost is not None and "free" not in txt.lower():
                offer.shipping_cost, offer.shipping_currency = cost, ccy
            if "free shipping" in txt.lower():
                offer.shipping_free = True

        lead = tree.css_first("[class*='lead-time'], .lead-time")
        if lead:
            days = self.parse_int(lead.text())
            if days and days < 400:
                offer.lead_time_days = days

        fees = tree.css_first("[class*='trade-assurance'], [class*='payment']")
        if fees:
            offer.fees_note = clean(fees.text(separator=" "))[:512] or None

        crumbs = [clean(a.text()) for a in tree.css(".breadcrumb a, [class*='breadcrumb'] a")]
        crumbs = [c for c in crumbs if c and c.lower() not in ("home", "alibaba.com")]
        if crumbs:
            offer.category_path = " > ".join(crumbs[:4])

        for img in tree.css("[class*='main-image'] img, .detail-gallery img, .images-view img")[:12]:
            u = self.first_attr(img)
            if u:
                offer.image_urls.append(u if u.startswith("http") else "https:" + u)

        desc = tree.css_first("#description, .product-description, [class*='detailmodule']")
        if desc:
            offer.description = clean(desc.text(separator=" "))[:8000]

        offer.detail_fetched = True
        return offer
