"""BuckyDrop: field mapping against the real, live response shape captured
2026-09-07 -- a browser-session-backed keyword search (no login, no captcha,
just a cookie set by visiting the site once) against a 1688 forwarding agent.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_buckydrop_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sourcehub.scrapers.buckydrop import SEARCH_API, SOURCING_URL, BuckydropAdapter  # noqa: E402

FAILS: list[str] = []


def check(label, got, want=True) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


class FakeBrowser:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    def fetch_json(self, url, *, referer=None, headers=None, method="GET", body=None):
        self.calls.append({"url": url, "referer": referer, "headers": headers,
                            "method": method, "body": body})
        return self.payload


# Trimmed from a real response -- costPrice is native CNY (matches 1688 pricing
# scale), and thirdGoodUrl is already a real, resolved detail.1688.com URL --
# no separate resolve step, unlike CNFans' opaque productID.
REAL_RESPONSE = {
    "success": True,
    "data": {
        "total": 2000, "size": 20, "pages": 100, "current": 1,
        "records": [
            {
                "spuCode": "966545615266",
                "spuName": "Usb3.0 Extender Set Multi-Port Docking Station Splitter",
                "ownerType": 2,
                "mainPic": "https://cbu01.alicdn.com/img/ibank/O1CN01IR7NxZ27wOYdGYFd5.jpg",
                "costPrice": 2.0, "originalPrice": 2.0, "salesVolume": 17105,
                "thirdParty": "ALIBABA", "thirdGoodCode": "966545615266",
                "thirdGoodUrl": "https://detail.1688.com/offer/966545615266.html",
                "thirdCatCode": "1038633", "minOrderQuantity": 0,
            },
            {
                "spuCode": "926126483019",
                "spuName": "Cross-Border Wholesale USB Docking Station",
                "ownerType": 2,
                "mainPic": "https://cbu01.alicdn.com/img/ibank/O1CN019lFxrM.jpg",
                "costPrice": 6.56, "originalPrice": 6.56, "salesVolume": 1201,
                "thirdParty": "ALIBABA", "thirdGoodCode": "926126483019",
                "thirdGoodUrl": "https://detail.1688.com/offer/926126483019.html",
                "thirdCatCode": "1038633", "minOrderQuantity": 0,
            },
        ],
    },
    "errKey": "", "code": 0, "info": "Success", "currentTime": 1788799448250,
}


def main() -> int:
    print("a real search response maps every field the adapter promises")
    adapter = BuckydropAdapter()
    fake = FakeBrowser(REAL_RESPONSE)
    adapter._browser = fake
    offers = list(adapter.search("usb hub", max_pages=1))

    check("both items mapped", len(offers), 2)
    call = fake.calls[0]
    check("hits the confirmed live endpoint", call["url"], SEARCH_API)
    check("visits the sourcing page first as referer, for the session cookie",
          call["referer"], SOURCING_URL)
    check("keyword sent inside item", call["body"]["item"]["keyword"], "usb hub")
    check("platform is ALIBABA (1688) -- the only confirmed value",
          call["body"]["item"]["platform"], "ALIBABA")
    check("current page sent", call["body"]["current"], 1)

    o = offers[0]
    check("site_product_id is the spuCode", o.site_product_id, "966545615266")
    check("url is the real, already-resolved 1688 detail page", o.url,
          "https://detail.1688.com/offer/966545615266.html")
    check("title mapped", o.title,
          "Usb3.0 Extender Set Multi-Port Docking Station Splitter")
    check("price is native CNY, not a converted value", o.price_min, 2.0)
    check("currency is CNY", o.currency, "CNY")
    check("orders_count mapped from salesVolume", o.orders_count, 17105)
    check("image mapped", o.image_urls,
          ["https://cbu01.alicdn.com/img/ibank/O1CN01IR7NxZ27wOYdGYFd5.jpg"])
    check("a minOrderQuantity of 0 (no MOQ restriction) floors to 1, not 0", o.moq, 1)

    print("\na moved/broken endpoint or a session BuckyDrop no longer accepts "
          "(HTTP 200, application-level error) is reported plainly")
    error_fetcher = FakeBrowser({"success": False, "code": "X", "info": "没有登录"})
    adapter2 = BuckydropAdapter()
    adapter2._browser = error_fetcher
    check("no offers yielded from a 'not logged in' response",
          list(adapter2.search("usb hub", max_pages=1)), [])

    print("\nan item missing spuCode, title or url is skipped, not crashed on")
    empty_fetcher = FakeBrowser({"success": True, "data": {"records": [{"mainPic": "x"}]}})
    adapter3 = BuckydropAdapter()
    adapter3._browser = empty_fetcher
    check("empty item yields nothing",
          list(adapter3.search("usb hub", max_pages=1)), [])

    print("\n" + "=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("buckydrop adapter OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
