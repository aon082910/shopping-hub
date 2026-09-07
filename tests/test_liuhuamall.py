"""LiuhuaMall: field mapping against the real, live response shape captured
2026-09-07 -- a fully anonymous keyword search (no login, no session)
against Guangzhou Liuhua clothing market's own independent B2B platform,
not a Taobao/1688 forwarding agent.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_liuhuamall_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sourcehub.scrapers.liuhuamall import LiuhuamallAdapter  # noqa: E402
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

    def get(self, url, *, params=None, headers=None, referer=None, expect_json=False):
        self.calls.append({"url": url, "params": params or {}, "headers": headers or {}})
        return Response(url=url, status=200, text=json.dumps(self.payload))


# Trimmed from a real response -- already-USD pricing, since this platform
# sells directly to international B2B buyers rather than proxying a
# CNY-priced domestic site.
REAL_RESPONSE = {
    "status": "true", "code": "200", "msg": "",
    "info": {
        "pageNum": 1, "pageSize": 2, "total": 150,
        "list": [
            {
                "goodsId": "1429649300525309954",
                "goodsName": "Wholesale Women's Elegant Sleeveless V Neck Mesh Bowknot Dress",
                "original": "https://static.liuhuamall.cn/resourcegoods/1362855735074B7E82C0EDCC8CACEA53.jpeg",
                "shopId": "1318834606186762241", "shopName": "JIN HUI DRESS",
                "price": 10.63, "moq": 10,
            },
            {
                "goodsId": "1429649300525309999",
                "goodsName": "Casual Cotton Blend Loose Fit Summer Dress",
                "original": "https://static.liuhuamall.cn/resourcegoods/other.jpeg",
                "shopId": "1318834606186762299", "shopName": "",
                "price": 5.20, "moq": 5,
            },
        ],
    },
}


def main() -> int:
    print("a real search response maps every field the adapter promises")
    adapter = LiuhuamallAdapter()
    fake = FakeFetcher(REAL_RESPONSE)
    adapter._fetcher = fake
    offers = list(adapter.search("dress", max_pages=1))

    check("both items mapped", len(offers), 2)
    call = fake.calls[0]
    check("hits the confirmed live endpoint", call["url"],
          "https://www.liuhuamall.com/api/search/buyer/goods")
    check("keyword sent", call["params"]["keyword"], "dress")
    check("filter sent as a JSON string", json.loads(call["params"]["filter"])["keyword"], "dress")

    o = offers[0]
    check("site_product_id is the goodsId", o.site_product_id, "1429649300525309954")
    check("url built from the confirmed /goods/{id} pattern", o.url,
          "https://www.liuhuamall.com/goods/1429649300525309954")
    check("title mapped", o.title,
          "Wholesale Women's Elegant Sleeveless V Neck Mesh Bowknot Dress")
    check("price already in USD, no conversion applied", o.price_min, 10.63)
    check("currency is USD (direct international B2B pricing, not a CNY proxy)",
          o.currency, "USD")
    check("moq mapped", o.moq, 10)
    check("seller mapped from shopName", o.seller_name, "JIN HUI DRESS")
    check("image mapped", o.image_urls,
          ["https://static.liuhuamall.cn/resourcegoods/1362855735074B7E82C0EDCC8CACEA53.jpeg"])

    o2 = offers[1]
    check("an empty shopName maps to None, not an empty string", o2.seller_name, None)

    print("\na moved/broken endpoint (HTTP 200, application-level error) is "
          "reported plainly, not treated as an empty catalog")
    error_fetcher = FakeFetcher({"status": "false", "code": "500", "msg": "internal error"})
    adapter2 = LiuhuamallAdapter()
    adapter2._fetcher = error_fetcher
    check("no offers yielded from an error response",
          list(adapter2.search("dress", max_pages=1)), [])

    print("\nan item missing both goodsId and a usable title is skipped, not crashed on")
    empty_fetcher = FakeFetcher({"status": "true", "code": "200", "msg": "",
                                 "info": {"list": [{"original": "x"}]}})
    adapter3 = LiuhuamallAdapter()
    adapter3._fetcher = empty_fetcher
    check("empty item yields nothing",
          list(adapter3.search("dress", max_pages=1)), [])

    print("\n" + "=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("liuhuamall adapter OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
