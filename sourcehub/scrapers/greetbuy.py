"""Greetbuy -- 1688 forwarding agent with a fully anonymous keyword search.

Confirmed live (2026-09-07): ``POST /gateway/alibabaSDK/global/goods_list.php``
returns real 1688 listings with no login, no session, no cookies at all -- a
plain HTTP POST with form-encoded fields works via curl from a cold start.
The field names (``offerId``, ``priceInfo``, ``repurchaseRate``,
``tradeScore``, ``sellerDataInfo``) match 1688's own open-platform API
vocabulary closely enough that this is most likely a thin server-side proxy
of it, not a bespoke schema -- found by watching greetbuy.com's own search
page make this exact call, the same way every other real endpoint in this
project was found this session.

Confirmed 1688-only: greetbuy.com's own search UI shows a "Taobao" tab
alongside "1688", but no channel/platform form field was found live for it
-- the one request captured for clicking it returned the same 1688-shaped
data, most likely because it caught a pagination call rather than a genuine
channel switch. Building this as 1688-only rather than guessing a parameter
name; someone with a real capture of the Taobao tab's request could extend
this later.

Items already carry a real, clean 1688 URL (``promotionURL``, tracking
params stripped) -- no USFans-style opaque-token problem here at all.

NOT yet implemented: fetch_detail() (search alone already returns enough
for a real offer: price, MOQ, image, rating, sales) and pagination past
whatever a single page returns.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, Optional

from ..util.text import clean
from .base import RawOffer, SiteAdapter

log = logging.getLogger(__name__)


class GreetbuyAdapter(SiteAdapter):
    key = "greetbuy"
    name = "Greetbuy"
    base_url = "https://www.greetbuy.com"
    home_currency = "CNY"
    needs_agent = True

    SEARCH_API = "https://www.greetbuy.com/gateway/alibabaSDK/global/goods_list.php"

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        for page in range(1, (max_pages or self.max_pages) + 1):
            try:
                payload = self.fetcher.post(
                    self.SEARCH_API,
                    data={
                        "LangMark": "en", "Currency": "USD", "imageId": "",
                        "keyword": keyword, "filter": "", "sort": "{}",
                        "priceStart": "", "priceEnd": "",
                        "beginPage": str(page), "pageSize": "50",
                    },
                    headers={"Accept": "application/json", "Referer": self.base_url + "/"},
                ).json()
            except Exception as e:
                log.warning("[greetbuy] search page %s failed: %s", page, e)
                return

            if str(payload.get("status")) != "200":
                log.error("[greetbuy] API rejected the request (status=%s): %s. The endpoint "
                          "has most likely moved; check SEARCH_API in scrapers/greetbuy.py.",
                          payload.get("status"), str(payload.get("msg"))[:120])
                return

            items = ((payload.get("data") or {}).get("data")) or []
            if not items:
                log.info("[greetbuy] no items on page %s for %r", page, keyword)
                return
            for item in items:
                offer = self._offer(item)
                if offer:
                    yield offer

    def _offer(self, item: dict) -> Optional[RawOffer]:
        offer_id = item.get("offerId")
        title = clean(str(item.get("subjectTrans") or item.get("subject") or ""))
        if not offer_id or not title:
            return None

        # promotionURL carries Greetbuy's own affiliate tracking
        # ("?fromkv=...&kjSource=pc") -- stripped so the stored url is the
        # plain, canonical 1688 product page other agents can wrap cleanly.
        url = str(item.get("promotionURL") or "").split("?")[0]
        if not url:
            return None

        price_info = item.get("priceInfo") or {}
        try:
            price = float(price_info.get("price"))
        except (TypeError, ValueError):
            price = None
        if not price:
            price = None

        rating = None
        try:
            rating = float(item.get("tradeScore"))
        except (TypeError, ValueError):
            pass

        offer = RawOffer(
            site_key=self.key,
            site_product_id=str(offer_id),
            url=url,
            title=title,
            currency="CNY",
            price_min=price,
            moq=max(1, int(item.get("minOrderQuantity") or 1)),
            moq_unit="piece",
            # No shop/seller name field in this response at all (checked --
            # not even inside sellerDataInfo) -- Greetbuy is the channel, not
            # the seller, so left unset rather than misattributed.
            rating=rating,
            orders_count=item.get("monthSold"),
            raw={"source": "search", "offer_id": offer_id},
        )
        image_url = item.get("imageUrl")
        if image_url:
            offer.image_urls.append(image_url)
        return offer
