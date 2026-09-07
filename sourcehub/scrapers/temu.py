"""Temu -- ships to the US directly, no forwarding agent needed.

Needs a real, logged-in session: confirmed live that an anonymous session gets
redirected to a mandatory "Sign in / Register" wall within seconds, even for
the plain homepage. Run ``python -m sourcehub.cli browser-login --site temu``
once (email + verification code is the reliable path -- "Continue with
Google" hits Google's own anti-automation defense, which detects Chrome being
driven over the DevTools protocol and freezes the page in a debugger trap;
that isn't something to work around here, it's Google actively blocking it).

The search results page (``/search_result.html``) is *not* a client-fetched
API the way USFans or LCSC are -- confirmed live via a captured HAR file: the
first page of results (40 items) is embedded directly in the page's own HTML
as ``window.rawData = {...}``, a plain (non-minified, non-obfuscated) JSON
object assigned once, no bracket-substitution tricks the way LCSC's Nuxt
state used. The real product fields live at ``store.goodsList[].data``:
``goodsId`` (real, numeric, safe to link to directly -- unlike USFans, Temu
has no reason to hide its own item ids), ``title``, ``priceInfo.price`` (an
integer in *cents*), ``priceInfo.currency``, ``image.url``, ``seoLinkUrl``
(a real relative product-page URL), ``salesNum``, ``comment.goodsScore``.

The product-detail page uses the SAME ``window.rawData`` mechanism, confirmed
live via a second captured HAR (this one for a product page specifically) --
but its real fields sit under a much deeper, differently-shaped
``store.goods``/``store.sku``/``store.mall``/``store.reviewStore``/
``store.productDetail`` tree, not ``store.goodsList`` (that shape is
search-only). One wrinkle: this HAR exported its response bodies base64
inline, so parsing it required decoding the HAR entry's own text field before
looking for ``window.rawData=`` -- the search HAR from the earlier capture
happened not to be base64-encoded, so this is a property of the exporting
tool/session, not of the page itself.

NOT yet implemented: pagination past the first ~40 search results per keyword.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterator, Optional
from urllib.parse import quote_plus

from ..util.text import clean
from .base import RawOffer, SiteAdapter

log = logging.getLogger(__name__)


def _extract_raw_data(html: str) -> Optional[dict]:
    """Parse the ``window.rawData = {...}`` object embedded in the page.

    Bracket-matched by hand (not a regex) because the object holds arbitrarily
    nested structures and strings that themselves contain ``{``/``}`` (the ad
    tracking blobs under ``tagsInfo`` are JSON-encoded *strings* full of them).
    """
    key = "window.rawData="
    i = html.find(key)
    if i == -1:
        return None
    start = html.find("{", i)
    if start == -1:
        return None
    depth, in_str, esc, end = 0, False, False, None
    for j in range(start, len(html)):
        c = html[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    if end is None:
        return None
    try:
        return json.loads(html[start:end])
    except Exception as e:
        log.debug("[temu] rawData did not parse as JSON: %s", e)
        return None


class TemuAdapter(SiteAdapter):
    key = "temu"
    name = "Temu"
    base_url = "https://www.temu.com"
    home_currency = "USD"
    needs_agent = False

    SEARCH = "https://www.temu.com/search_result.html?search_key={kw}&search_method=user"

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        # Pagination beyond the first embedded page isn't implemented yet
        # (see module docstring) -- one page is still a real, working result
        # set, not nothing.
        url = self.SEARCH.format(kw=quote_plus(keyword))
        try:
            html = self.fetch_html(url, phase="search")
        except Exception as e:
            log.warning("[temu] search failed: %s", e)
            return

        raw = _extract_raw_data(html)
        if raw is None:
            log.warning(
                "[temu] no window.rawData on the search page -- likely not "
                "logged in (run `browser-login --site temu`) or the site "
                "changed its embedded-state format."
            )
            return

        items = (raw.get("store") or {}).get("goodsList") or []
        if not items:
            log.info("[temu] no items for %r", keyword)
            return

        for entry in items:
            try:
                offer = self._parse_item((entry or {}).get("data") or {})
                if offer:
                    yield offer
            except Exception as e:
                log.debug("[temu] bad item: %s", e)

    def _parse_item(self, d: dict[str, Any]) -> Optional[RawOffer]:
        goods_id = d.get("goodsId")
        title = clean(str(d.get("title") or ""))
        if not goods_id or not title:
            return None

        # seoLinkUrl is the real, human-readable product page path; linkUrl is
        # the same item via a bare "goods.html?...&goods_id=..." redirect.
        # Both carry tracking query params worth stripping for a stable URL.
        path = str(d.get("seoLinkUrl") or d.get("linkUrl") or "")
        if not path:
            return None
        url = (self.base_url + path if path.startswith("/") else f"{self.base_url}/{path}")
        url = url.split("?")[0]

        price_info = d.get("priceInfo") or {}
        price_cents = price_info.get("price")
        price = (price_cents / 100) if isinstance(price_cents, (int, float)) else None
        currency = str(price_info.get("currency") or "USD")

        comment = d.get("comment") or {}
        review_count = None
        raw_review_count = comment.get("commentNumTips")
        if isinstance(raw_review_count, str) and raw_review_count.isdigit():
            review_count = int(raw_review_count)

        orders_count = None
        raw_sales = d.get("salesNum")
        if isinstance(raw_sales, str) and raw_sales.isdigit():
            orders_count = int(raw_sales)

        offer = RawOffer(
            site_key=self.key,
            site_product_id=str(goods_id),
            url=url,
            title=title,
            currency=currency,
            price_min=price,
            moq=1,
            moq_unit="piece",
            seller_name="Temu",
            rating=comment.get("goodsScore"),
            review_count=review_count,
            orders_count=orders_count,
            raw={"source": "search", "goods_id": goods_id},
        )
        image_url = (d.get("image") or {}).get("url")
        if image_url:
            offer.image_urls.append(image_url)
        return offer

    def fetch_detail(self, offer: RawOffer) -> RawOffer:
        try:
            html = self.fetch_html(offer.url, phase="detail")
        except Exception as e:
            log.warning("[temu] detail fetch failed for %s: %s", offer.url, e)
            return offer

        raw = _extract_raw_data(html)
        if raw is None:
            log.warning("[temu] no window.rawData on the detail page for %s", offer.url)
            return offer

        store = raw.get("store") or {}
        g = store.get("goods") or {}
        if not g:
            return offer

        title = clean(str(g.get("goodsName") or ""))
        if title:
            offer.title = title

        min_price = g.get("minOnSalePrice")
        max_price = g.get("maxOnSalePrice")
        if isinstance(min_price, (int, float)):
            offer.price_min = min_price / 100
        if isinstance(max_price, (int, float)):
            offer.price_max = max_price / 100

        for img in g.get("gallery") or []:
            url = img.get("url") if isinstance(img, dict) else None
            if url and url not in offer.image_urls:
                offer.image_urls.append(url)

        mall_name = clean(str(((store.get("mall") or {}).get("mallData") or {}).get("mallName") or ""))
        if mall_name:
            offer.seller_name = mall_name

        review = store.get("reviewStore") or {}
        if review.get("showScore") is not None:
            offer.rating = review.get("showScore")
        if review.get("reviewNum") is not None:
            offer.review_count = review.get("reviewNum")

        for prop in g.get("goodsProperty") or []:
            if not isinstance(prop, dict):
                continue
            key = clean(str(prop.get("key") or ""))
            value = clean(", ".join(str(v) for v in (prop.get("values") or []) if v))
            if key and value:
                offer.add_spec(key, value)

        # Rich description: floorList mixes text blocks (type 3) and image
        # blocks (type 1) in display order; join the text ones into a plain
        # description rather than trying to preserve the original layout.
        floor_list = (store.get("productDetail") or {}).get("floorList") or []
        desc_parts = [
            clean(str(item["text"]))
            for floor in floor_list
            for item in (floor.get("items") or [])
            if item.get("text")
        ]
        if desc_parts:
            offer.description = "\n".join(desc_parts)[:5000]

        for sku in store.get("sku") or []:
            if not isinstance(sku, dict):
                continue
            sku_id = sku.get("skuId")
            if not sku_id:
                continue
            attrs = {}
            for spec in sku.get("specs") or []:
                k = clean(str(spec.get("specKey") or ""))
                v = clean(str(spec.get("specValue") or ""))
                if k and v:
                    attrs[k] = v
            price_cents = sku.get("salePrice")
            price = (price_cents / 100) if isinstance(price_cents, (int, float)) else None
            stock = sku.get("stockQuantity")
            stock = int(stock) if isinstance(stock, (int, float)) else None
            offer.add_variant(
                sku=str(sku_id),
                name=", ".join(f"{k}: {v}" for k, v in attrs.items()) or str(sku_id),
                price=price,
                currency=offer.currency,
                attrs=attrs,
                stock=stock,
                in_stock=(stock > 0) if stock is not None else True,
                image_url=sku.get("thumbUrl") or None,
            )

        offer.detail_fetched = True
        return offer
