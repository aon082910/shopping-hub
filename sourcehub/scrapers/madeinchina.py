"""Made-in-China.com -- manufacturer directory with FOB tiered pricing.

Made-in-China is the most consistently *structured* of the B2B sites: nearly every
listing has a real attribute table and an explicit MOQ, which makes it a good
anchor for matching wholesale offers against retail ones.
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


class MadeInChinaAdapter(SiteAdapter):
    key = "madeinchina"
    name = "Made-in-China"
    base_url = "https://www.made-in-china.com"
    home_currency = "USD"

    SEARCH = "https://www.made-in-china.com/productdirectory.do?word={kw}&file=&subaction=hunt&style=b&mode=and&code=0&comProvince=nolimit&order=0&isOpenCorrection=1&page={page}"

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        for page in range(1, (max_pages or self.max_pages) + 1):
            url = self.SEARCH.format(kw=quote_plus(keyword), page=page)
            try:
                body = self.fetcher.get(url, referer=self.base_url + "/").text
            except Exception as e:
                log.warning("[madeinchina] page %s failed: %s", page, e)
                return

            tree = HTMLParser(body)
            cards = self.select_cards(tree, [
                ".prod-list .prod-item", ".list-node", ".search-list li",
                "div[class*='product-item']",
            ])
            if not cards:
                return
            for card in cards:
                try:
                    offer = self._parse_card(card)
                    if offer:
                        yield offer
                except Exception as e:
                    log.debug("[madeinchina] bad card: %s", e)

    def _parse_card(self, card) -> Optional[RawOffer]:
        link = card.css_first("h2 a, .product-name a, a[href*='/product/']")
        url = self.abs_url(link.attributes.get("href") if link else None)
        if not url:
            return None

        title = clean(
            (link.attributes.get("title") if link else "")
            or (link.text(strip=True) if link else "")
        )
        if not title:
            return None

        m = re.search(r"/([A-Za-z0-9_-]{6,})\.html", url)
        pid = m.group(1) if m else url.rstrip("/").rsplit("/", 1)[-1][:120]

        price_text = self.text_of(card, ".price, [class*='price']")
        pmin, pmax, ccy = parse_price(price_text, "USD")
        moq, unit = parse_moq(self.text_of(card, ".moq, [class*='min-order'], [class*='order']"))

        offer = RawOffer(
            site_key=self.key,
            site_product_id=pid,
            url=url.split("?")[0],
            title=title,
            currency=ccy,
            price_min=pmin,
            price_max=pmax,
            moq=moq,
            moq_unit=unit,
            seller_name=clean(self.text_of(card, ".compnay-name, .company-name, [class*='company']")) or None,
            is_verified_supplier=bool(card.css_first("[class*='audited'], [class*='diamond'], .icon-as")),
            seller_years=self.parse_int(self.text_of(card, "[class*='year']")),
            shipping_from=clean(self.text_of(card, "[class*='province'], [class*='location']")) or None,
            raw={"source": "search"},
        )
        u = self.first_attr(card.css_first("img"))
        if u:
            offer.image_urls.append(u if u.startswith("http") else "https:" + u)
        return offer

    def fetch_detail(self, offer: RawOffer) -> RawOffer:
        body = self.fetcher.get(offer.url, referer=self.base_url + "/").text
        tree = HTMLParser(body)
        self.apply_json_ld(offer, tree)

        for row in tree.css(
            ".basic-info-list .bac-item, .prop-item, table.info-table tr, dl.basic-info-list > div"
        ):
            k = self.text_of(row, ".bac-item-label, .prop-name, th, dt")
            v = self.text_of(row, ".bac-item-value, .prop-value, td, dd")
            if k and v and k != v:
                offer.add_spec(k, v)

        ladder = tree.css_first(".price-ladder, [class*='ladder'], .fob-price, [class*='price-list']")
        if ladder:
            for tier in parse_tiers(ladder.text(separator=" "), offer.currency):
                offer.tiers.append(RawTier(**tier))
        if offer.tiers:
            offer.moq = min(t.min_qty for t in offer.tiers)
            offer.price_min = min(t.price for t in offer.tiers)
            offer.price_max = max(t.price for t in offer.tiers)

        port = tree.css_first("[class*='port'], [class*='payment-terms']")
        note = "FOB pricing - freight, insurance, duty and customs are additional."
        if port:
            note = clean(port.text(separator=" "))[:400] + " | " + note
        offer.fees_note = note[:512]

        lead = tree.css_first("[class*='lead-time'], [class*='delivery-time']")
        if lead:
            d = self.parse_int(lead.text())
            if d and d < 400:
                offer.lead_time_days = d

        crumbs = [clean(a.text()) for a in tree.css(".breadcrumb a, [class*='crumb'] a")]
        crumbs = [c for c in crumbs if c and c.lower() not in ("home", "made-in-china.com")]
        if crumbs:
            offer.category_path = " > ".join(crumbs[:4])

        for img in tree.css(".sr-proMainInfo-slide img, [class*='gallery'] img, .J-product-img img")[:12]:
            u = self.first_attr(img)
            if u:
                offer.image_urls.append(u if u.startswith("http") else "https:" + u)

        desc = tree.css_first(".sr-proDetail-content, [class*='product-detail'], #j-product-desc")
        if desc:
            offer.description = clean(desc.text(separator=" "))[:8000]

        offer.detail_fetched = True
        return offer
