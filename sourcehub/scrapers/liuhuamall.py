"""LiuHuaMall -- Guangzhou Liuhua clothing wholesale market's own B2B platform.

Not a forwarding agent at all, unlike every other site added this session --
confirmed live, this is its own independent marketplace with its own catalog
(2000+ onsite suppliers from the real Guangzhou Liuhua wholesale district),
already-USD pricing, and (per its own homepage copy) its own international
shipping, the same shape as Alibaba/DHgate/Chinavasion rather than a
USFans/Greetbuy-style proxy of Taobao/1688.

Confirmed live (2026-09-07): ``GET /api/search/buyer/goods`` is fully
anonymous (no login, no session) -- found by watching its own search page,
confirmed independently callable via plain curl from a cold start. Product
pages are a simple ``/goods/{goodsId}`` path, confirmed by visiting one
directly with no other context.

NOT yet implemented: fetch_detail() (search alone already returns a real
price, MOQ, image and shop name) and pagination past whatever a single
page returns.
"""

from __future__ import annotations

import json
import logging
from typing import Iterator, Optional
from urllib.parse import quote

from ..util.text import clean
from .base import RawOffer, SiteAdapter

log = logging.getLogger(__name__)


class LiuhuamallAdapter(SiteAdapter):
    key = "liuhuamall"
    name = "LIUHUAMALL"
    base_url = "https://www.liuhuamall.com"
    home_currency = "USD"
    needs_agent = False

    SEARCH_API = "https://www.liuhuamall.com/api/search/buyer/goods"

    def category_seeds(self) -> list[tuple[str, str]]:
        # Confirmed live: an empty keyword against the same search endpoint
        # returns the whole catalog (categoryId is already sent empty too), so
        # one seed with no real category walking is enough -- crawl_category()
        # just pages until the API itself runs out of results.
        return [("all", "")]

    def crawl_category(self, seed_url: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        yield from self.search("", max_pages=max_pages or 10_000)

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        for page in range(1, (max_pages or self.max_pages) + 1):
            filt = {
                "keyword": keyword, "sort": {"name": "def", "order": "desc"},
                "categoryId": "", "price": {"ge": 0, "le": 200},
                "goodsCustomTypeList": [0, 0], "moq": {"lt": 0},
            }
            try:
                payload = self.fetcher.get(
                    self.SEARCH_API,
                    params={
                        "pageNum": page, "pageSize": 30, "sort": "def_desc",
                        "keyword": keyword, "filter": json.dumps(filt),
                    },
                    headers={"Accept": "application/json"},
                    expect_json=True,
                ).json()
            except Exception as e:
                log.warning("[liuhuamall] search page %s failed: %s", page, e)
                return

            if str(payload.get("status")).lower() != "true":
                log.error("[liuhuamall] API rejected the request (code=%s): %s. The endpoint "
                          "has most likely moved; check SEARCH_API in scrapers/liuhuamall.py.",
                          payload.get("code"), str(payload.get("msg"))[:120])
                return

            items = ((payload.get("info") or {}).get("list")) or []
            if not items:
                log.info("[liuhuamall] no items on page %s for %r", page, keyword)
                return
            for item in items:
                offer = self._offer(item)
                if offer:
                    yield offer

    def _offer(self, item: dict) -> Optional[RawOffer]:
        goods_id = item.get("goodsId")
        title = clean(str(item.get("goodsName") or ""))
        if not goods_id or not title:
            return None

        try:
            price = float(item.get("price"))
        except (TypeError, ValueError):
            price = None
        if not price:
            price = None

        offer = RawOffer(
            site_key=self.key,
            site_product_id=str(goods_id),
            url=f"{self.base_url}/goods/{quote(str(goods_id))}",
            title=title,
            currency="USD",
            price_min=price,
            moq=max(1, int(item.get("moq") or 1)),
            moq_unit="piece",
            seller_name=clean(str(item.get("shopName") or "")) or None,
            raw={"source": "search", "goods_id": goods_id},
        )
        image_url = item.get("original")
        if image_url:
            offer.image_urls.append(image_url)
        return offer
