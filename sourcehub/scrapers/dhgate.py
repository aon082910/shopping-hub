"""DHgate -- small-lot wholesale, English, USD, ships to the US directly.

Notable: DHgate quotes a *per-unit* price alongside a lot size ("$3.20 / piece,
Min. Order: 5 pieces"), and it discloses shipping cost on the listing page more
often than most, so ``landed_cost_usd`` is usually computable here.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator, Optional
from urllib.parse import quote_plus

from selectolax.parser import HTMLParser

from ..util.money import parse_moq, parse_price
from ..util.text import clean
from .base import RawOffer, SiteAdapter

log = logging.getLogger(__name__)


class DHgateAdapter(SiteAdapter):
    key = "dhgate"
    name = "DHgate"
    base_url = "https://www.dhgate.com"
    home_currency = "USD"

    SEARCH = "https://www.dhgate.com/wholesale/search.do?act=search&searchkey={kw}&pageNum={page}"

    def extra_headers(self) -> dict[str, str]:
        return {"Referer": self.base_url + "/", "Accept-Language": "en-US,en;q=0.9"}

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        for page in range(1, (max_pages or self.max_pages) + 1):
            url = self.SEARCH.format(kw=quote_plus(keyword), page=page)
            try:
                body = self.fetch_html(url)
            except Exception as e:
                log.warning("[dhgate] page %s failed: %s", page, e)
                return

            tree = HTMLParser(body)
            cards = self.select_cards(tree, [
                ".gallery-main", "div[class*='gallery-main']", ".listitem",
                ".item-wrap", "li[class*='product']", "div[class*='productItem']",
            ])
            if not cards:
                log.info("[dhgate] no cards on page %s for %r", page, keyword)
                return
            for card in cards:
                try:
                    offer = self._parse_card(card)
                    if offer:
                        yield offer
                except Exception as e:
                    log.debug("[dhgate] bad card: %s", e)

    def _parse_card(self, card) -> Optional[RawOffer]:
        # Verified against live markup: cards are .gallery-main, with the title in
        # .gallery-pro-name, the price in .current-price ("US $2.26 - 3.14/Piece")
        # and the seller in .store-name.
        link = card.css_first(
            ".gallery-pro-name a, .gallery-img-link, a[href*='/product/'], "
            "a.pro-title, h3 a"
        )
        href = (link.attributes.get("href") if link else "") or ""
        url = self.abs_url(href)
        if not url or "/product/" not in url:
            return None
        # The card carries the id directly; the URL regex is the fallback.
        itemcode = card.attributes.get("itemcode") or self.attr_of(
            card, "[itemcode]", "itemcode"
        )
        m = re.search(r"/(\d{6,})\.html", url)
        pid = itemcode or (m.group(1) if m
                           else url.rstrip("/").rsplit("/", 1)[-1].split(".")[0])

        title = clean(
            self.text_of(card, ".gallery-pro-name")
            or (link.attributes.get("title") if link else "")
            or self.text_of(card, "h3, .pro-title, [class*='title']")
            or (link.text(strip=True) if link else "")
        )
        if not title:
            return None

        price_text = (
            self.text_of(card, ".current-price")
            or self.text_of(card, "[class*='price'], .price, .cur-price")
        )
        pmin, pmax, ccy = parse_price(price_text, "USD")

        lot_text = self.text_of(card, "[class*='minOrder'], .min-order, [class*='sold']")
        moq, unit = parse_moq(lot_text)

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
                self.text_of(card, ".store-name")
                or self.text_of(card, "[class*='store'], .seller-name")
            ) or None,
            rating=self.parse_float(self.text_of(card, "[class*='rating'], .star-rating")),
            review_count=self.parse_int(self.text_of(card, "[class*='review'], .reviews")),
            orders_count=self.parse_int(self.text_of(card, "[class*='sold'], .sold-num")),
            raw={"source": "search"},
        )

        ship_text = self.text_of(card, "[class*='shipping'], .freight")
        if ship_text:
            offer.shipping_note = clean(ship_text)[:512]
            if "free shipping" in ship_text.lower():
                offer.shipping_free = True
            else:
                cost, _, sccy = parse_price(ship_text, "USD")
                if cost is not None:
                    offer.shipping_cost, offer.shipping_currency = cost, sccy

        u = self.first_attr(card.css_first("img"))
        if u:
            offer.image_urls.append(u if u.startswith("http") else "https:" + u)
        return offer

    def fetch_detail(self, offer: RawOffer) -> RawOffer:
        body = self.fetch_html(offer.url, phase="detail")
        tree = HTMLParser(body)

        # DHgate publishes a full schema.org Product (brand, sku, mpn). Reading it
        # first is what gives the matcher its identifier signals.
        self.apply_json_ld(offer, tree)

        # Verified against a captured page: this template carries no stable
        # specification class, but the attributes are plain "Key: Value" list items,
        # so shape is a more durable signal here than any class name.
        for row in tree.css(
            ".prodSpecifications_showUl__2Jaqc li, .product-props li, "
            "[class*='specification'] li, .attribute-list li, li"
        ):
            if row.child is not None and row.css("li"):
                continue  # a wrapper, not a leaf attribute
            txt = clean(row.text(separator=" "))
            if not (6 < len(txt) < 200) or ":" not in txt:
                continue
            key, _, value = txt.partition(":")
            key, value = key.strip(), value.strip()
            # Guard against prose and nav that merely contains a colon.
            if 1 < len(key) <= 40 and value and len(key.split()) <= 5:
                offer.add_spec(key, value)
            if len(offer.specs) >= 40:
                break

        ship = tree.css_first("[class*='shipping'], #shipping-cost, .freight-info")
        if ship:
            txt = clean(ship.text(separator=" "))[:512]
            offer.shipping_note = txt
            if "free shipping" in txt.lower():
                offer.shipping_free = True
            else:
                cost, _, ccy = parse_price(txt, "USD")
                if cost is not None:
                    offer.shipping_cost, offer.shipping_currency = cost, ccy

        fees = tree.css_first("[class*='tax'], [class*='import'], [class*='duty']")
        if fees:
            offer.fees_note = clean(fees.text(separator=" "))[:512] or None

        crumbs = [clean(a.text()) for a in tree.css(".crumb a, [class*='breadcrumb'] a")]
        crumbs = [c for c in crumbs if c and c.lower() not in ("home", "dhgate")]
        if crumbs:
            offer.category_path = " > ".join(crumbs[:4])

        for img in tree.css("[class*='masterMap'] img, .pro-imgs img, #j-product-img img")[:12]:
            u = self.first_attr(img)
            if u:
                offer.image_urls.append(u if u.startswith("http") else "https:" + u)

        if not offer.image_urls:
            # Gallery images are injected by script, so they exist in the page source
            # but not in the DOM as <img>. Product photos live under /albu/ on the
            # image CDN; css.dhresource.com is chrome (logos, licence badges).
            for url in dict.fromkeys(
                re.findall(r"https://img\d*\.dhresource\.com/[^\s\"']{10,120}", body)
            ):
                if "/albu/" in url and not url.endswith((".css", ".js")):
                    offer.image_urls.append(url)
                if len(offer.image_urls) >= 8:
                    break

        desc = tree.css_first("#productDesc, .product-description, [class*='prodDetail']")
        if desc:
            offer.description = clean(desc.text(separator=" "))[:8000]

        offer.detail_fetched = True
        return offer
