"""BuckyDrop -- a 1688/Taobao forwarding agent with its own product search.

Confirmed live (2026-09-07): ``POST /api/buckydrop/portal/product/page-search-new``
looked "not logged in" (``{"success":false,"info":"没有登录"}``) when replayed cold
via plain curl with the exact captured JSON body -- but the identical call succeeds
with no login and no captcha from inside a real browser tab that has simply
*visited* the site once (``document.cookie`` shows only analytics cookies; whatever
BuckyDrop checks is an httpOnly cookie set on that first visit, not an account
session). ``BrowserSession.fetch_json(..., referer=SOURCING_URL)`` was built for
exactly this shape of problem: load the referer once inside the same page to pick
up that cookie, then fire the API call from the same context.

This is notably *better* than CNFans, which looks superficially similar (also a
forwarding agent, also has its own keyword search): BuckyDrop's search results
already carry a real, resolved ``thirdGoodUrl`` straight to ``detail.1688.com`` --
no separate, Cloudflare-gated resolve step required. Confirmed live only for
``platform: "ALIBABA"`` (1688); a ``"TAOBAO"`` value was not captured/tested.

NOT yet implemented: fetch_detail() (search alone already returns price, sales
count and an image) and pagination past whatever ``current``/``size`` return.
"""

from __future__ import annotations

import logging
from typing import Iterator, Optional

from ..util.text import clean
from .base import RawOffer, SiteAdapter

log = logging.getLogger(__name__)

SOURCING_URL = "https://www.buckydrop.com/en/sourcing"
SEARCH_API = "https://www.buckydrop.com/api/buckydrop/portal/product/page-search-new"


class BuckydropAdapter(SiteAdapter):
    key = "buckydrop"
    name = "BuckyDrop"
    base_url = "https://www.buckydrop.com"
    home_currency = "CNY"
    needs_agent = True

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        for page in range(1, (max_pages or self.max_pages) + 1):
            body = {
                "current": page,
                "size": 20,
                "item": {
                    "platform": "ALIBABA",
                    "keyword": keyword,
                    "activityTags": [],
                    "sort": {},
                    "filter": [],
                },
            }
            try:
                payload = self.browser.fetch_json(
                    SEARCH_API,
                    referer=SOURCING_URL,
                    headers={"Content-Type": "application/json;charset=utf-8", "Lang": "en"},
                    method="POST",
                    body=body,
                )
            except Exception as e:
                log.warning("[buckydrop] search page %s failed: %s", page, e)
                return

            if not isinstance(payload, dict) or not payload.get("success"):
                log.error("[buckydrop] API rejected the request (code=%s): %s. The endpoint "
                          "or its session requirement has most likely changed; check "
                          "SEARCH_API in scrapers/buckydrop.py.",
                          (payload or {}).get("code"), str((payload or {}).get("info"))[:120])
                return

            items = ((payload.get("data") or {}).get("records")) or []
            if not items:
                log.info("[buckydrop] no items on page %s for %r", page, keyword)
                return
            for item in items:
                offer = self._offer(item)
                if offer:
                    yield offer

    def _offer(self, item: dict) -> Optional[RawOffer]:
        spu_code = item.get("spuCode")
        title = clean(str(item.get("spuName") or ""))
        url = item.get("thirdGoodUrl")
        if not spu_code or not title or not url:
            return None

        try:
            price = float(item.get("costPrice"))
        except (TypeError, ValueError):
            price = None
        if not price:
            price = None

        offer = RawOffer(
            site_key=self.key,
            site_product_id=str(spu_code),
            url=url,
            title=title,
            currency="CNY",
            price_min=price,
            moq=max(1, int(item.get("minOrderQuantity") or 1)),
            moq_unit="piece",
            orders_count=item.get("salesVolume"),
            raw={
                "source": "search",
                "spu_code": spu_code,
                "third_party": item.get("thirdParty"),
                "third_good_code": item.get("thirdGoodCode"),
            },
        )
        image_url = item.get("mainPic")
        if image_url:
            offer.image_urls.append(image_url)
        return offer
