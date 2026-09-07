"""Greetbuy: field mapping against the real, live response shape captured
2026-09-07 -- a fully anonymous keyword search (no login, no session), found
by watching greetbuy.com's own search page call
POST gateway/alibabaSDK/global/goods_list.php, then confirmed independently
callable via plain curl from a cold start.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_greetbuy_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sourcehub.scrapers.greetbuy import GreetbuyAdapter  # noqa: E402
from sourcehub.util.http import Response  # noqa: E402

FAILS: list[str] = []


def check(label, got, want=True) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


class FakeFetcher:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    def post(self, url, *, data=None, headers=None, referer=None):
        self.calls.append({"url": url, "data": data or {}, "headers": headers or {}})
        return Response(url=url, status=200, text=json.dumps(self.payload))


# Trimmed from a real response to two items with distinct shapes -- one with
# a real price and rating, one with a null repurchaseRate/tradeScore style
# field to confirm that doesn't crash the mapping.
REAL_RESPONSE = {
    "status": 200, "msg": "success",
    "data": {
        "totalRecords": 2000, "totalPage": 41, "pageSize": 50, "currentPage": 1,
        "data": [
            {
                "imageUrl": "https://cbu01.alicdn.com/img/ibank/O1CN01IR7NxZ27wOYdGYFd5_!!2219782677861-0-cib.jpg",
                "subject": "usb3.0扩展器集多插口扩展坞分线器笔记本电脑外接鼠标键盘U优盘",
                "subjectTrans": "Usb3.0 Extender Set Multi-Port Docking Station Splitter Laptop "
                                "External Mouse Keyboard USB Flash Drive",
                "offerId": 966545615266,
                "priceInfo": {"price": "2.00", "consignPrice": "4"},
                "repurchaseRate": "13%", "monthSold": 17105, "tradeScore": "4.7",
                "minOrderQuantity": 1,
                "promotionURL": "https://detail.1688.com/offer/966545615266.html"
                                "?fromkv=refer:HVEVURCJJZFFSSCFGJDDMT2KK5DVSMSU&kjSource=pc",
            },
            {
                "imageUrl": "https://cbu01.alicdn.com/img/ibank/9272046796_775092900.jpg",
                "subject": "高速usb 3.0 4口hub 一拖四USB3.0集线器厂家 4/7口开关",
                "subjectTrans": "High-Speed USB 3.0 4-Port Hub One to Four Usb3.0 Hub "
                                "Manufacturer 4/7 Port Switch",
                "offerId": 576254559101,
                "priceInfo": {"price": "21.10", "consignPrice": "21.1"},
                "monthSold": 1879, "minOrderQuantity": 2,
                "promotionURL": "https://detail.1688.com/offer/576254559101.html"
                                "?fromkv=refer:HVEVURCJJZFFSSCFGJDDMTSS&kjSource=pc",
            },
        ],
    },
}


def main() -> int:
    print("a real search response maps every field the adapter promises")
    adapter = GreetbuyAdapter()
    fake = FakeFetcher(REAL_RESPONSE)
    adapter._fetcher = fake
    offers = list(adapter.search("usb hub", max_pages=1))

    check("both items mapped", len(offers), 2)
    call = fake.calls[0]
    check("hits the confirmed live endpoint", call["url"],
          "https://www.greetbuy.com/gateway/alibabaSDK/global/goods_list.php")
    check("keyword sent as form data", call["data"]["keyword"], "usb hub")
    check("beginPage starts at 1", call["data"]["beginPage"], "1")

    o = offers[0]
    check("site_product_id is the 1688 offerId", o.site_product_id, "966545615266")
    check("url stripped of Greetbuy's own affiliate tracking query string",
          o.url, "https://detail.1688.com/offer/966545615266.html")
    check("title prefers the English translation", o.title,
          "Usb3.0 Extender Set Multi-Port Docking Station Splitter Laptop "
          "External Mouse Keyboard USB Flash Drive")
    check("price mapped from priceInfo.price (raw CNY), not consignPrice",
          o.price_min, 2.00)
    check("currency is CNY (this is a 1688 proxy)", o.currency, "CNY")
    check("moq mapped", o.moq, 1)
    check("rating mapped from tradeScore", o.rating, 4.7)
    check("orders_count mapped from monthSold", o.orders_count, 17105)
    check("image mapped", o.image_urls, [
        "https://cbu01.alicdn.com/img/ibank/O1CN01IR7NxZ27wOYdGYFd5_!!2219782677861-0-cib.jpg"])

    o2 = offers[1]
    check("second item's rating absent (no tradeScore) doesn't crash, stays None",
          o2.rating, None)
    check("second item still mapped fine otherwise", o2.price_min, 21.10)

    print("\na moved/broken endpoint (HTTP 200, application-level error) is "
          "reported plainly, not treated as an empty catalog")
    error_fetcher = FakeFetcher({"status": 500, "msg": "internal error"})
    adapter2 = GreetbuyAdapter()
    adapter2._fetcher = error_fetcher
    check("no offers yielded from an error response",
          list(adapter2.search("usb hub", max_pages=1)), [])

    print("\nan item missing both offerId and a usable title is skipped, not crashed on")
    empty_fetcher = FakeFetcher({"status": 200, "msg": "success",
                                 "data": {"data": [{"imageUrl": "x"}]}})
    adapter3 = GreetbuyAdapter()
    adapter3._fetcher = empty_fetcher
    check("empty item yields nothing",
          list(adapter3.search("usb hub", max_pages=1)), [])

    print("\n" + "=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("greetbuy adapter OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
