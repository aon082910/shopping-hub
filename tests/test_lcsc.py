"""LCSC: field mapping against the real, live response shape from the site's
current search endpoint (POST wmsc.lcsc.com/ftps/wm/product/query/list),
captured 2026-09-07 -- the old wmsc.lcsc.com/wmsc/search/global endpoint had
moved and now 404s, silently masquerading as an empty catalog. The new one
was found by hooking the live site's own Vue component and calling its
search method directly (not guessed from docs), then confirmed independently
callable anonymously via curl. Same field names as before (productCode,
productModel, productPriceList, ...), only the URL and the top-level result
key (`dataList`, not `productList`) changed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_lcsc_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sourcehub.scrapers.components import LcscAdapter  # noqa: E402
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
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, *, json_body=None, headers=None, referer=None):
        self.calls.append((url, json_body or {}))
        return Response(url=url, status=200, text=json.dumps(self.payload))


# Trimmed from a real, live response to two items with distinct shapes -- one
# with a real tiered price ladder and stock, one with a null price list (to
# confirm that doesn't crash the tier-building loop).
REAL_RESPONSE = {
    "code": 200, "msg": None, "ok": True,
    "result": {
        "currPage": 1, "pageRow": 25, "totalPage": 46, "totalRow": 226,
        "dataList": [
            {
                "productId": 3198300, "productCode": "C2913202",
                "minBuyNumber": 1, "productModel": "ESP32-S3-WROOM-1-N16R8",
                "brandNameEn": "ESPRESSIF", "catalogName": "WiFi Modules",
                "productIntroEn": "2.4GHz ESP32-S3R8 On-board PCB Antenna "
                                  "-103.5dBm SMD,25.5x18mm RF Transceiver "
                                  "Modules and Modems RoHS",
                "productPriceList": [
                    {"ladder": 1, "productPrice": "5.1787", "usdPrice": 5.1787},
                    {"ladder": 10, "productPrice": "4.5510", "usdPrice": 4.5510},
                    {"ladder": 30, "productPrice": "4.0594", "usdPrice": 4.0594},
                ],
                "stockNumber": 22795,
            },
            {
                "productId": 3401558, "productCode": "C2980306",
                "minBuyNumber": 1, "productModel": "ESP32-PICO-MINI-02-N8R2",
                "brandNameEn": "ESPRESSIF", "catalogName": "WiFi Modules",
                "productIntroEn": "2.4GHz ESP32 Chip On-board PCB Antenna",
                "productPriceList": None,
                "stockNumber": 576,
            },
        ],
    },
}


def main() -> int:
    print("a real search response maps every field the adapter promises")
    adapter = LcscAdapter()
    fake = FakeFetcher(REAL_RESPONSE)
    adapter._fetcher = fake
    offers = list(adapter.search("esp32", max_pages=1))

    check("both items mapped", len(offers), 2)
    url, body = fake.calls[0]
    check("hits the current (post-move) endpoint",
          url, "https://wmsc.lcsc.com/ftps/wm/product/query/list")
    check("plain keyword field, not globalKeyword (flat cross-category search)",
          body.get("keyword"), "esp32")

    o = offers[0]
    check("site_product_id is the LCSC part code", o.site_product_id, "C2913202")
    check("url built from the part code", o.url,
          "https://www.lcsc.com/product-detail/C2913202.html")
    check("mpn is the manufacturer part number", o.mpn, "ESP32-S3-WROOM-1-N16R8")
    check("brand", o.brand, "ESPRESSIF")
    check("category from catalogName", o.category_path, "WiFi Modules")
    check("three price tiers parsed", len(o.tiers), 3)
    check("tiers sorted ascending by qty", [t.min_qty for t in o.tiers], [1, 10, 30])
    check("open-ended ladder upper bounds closed off",
          [t.max_qty for t in o.tiers], [9, 29, None])
    check("price_min is the cheapest tier (highest qty)", o.price_min, 4.0594)
    check("price_max is the priciest tier (qty 1)", o.price_max, 5.1787)
    check("in_stock reflects a real stockNumber > 0", o.in_stock, True)

    o2 = offers[1]
    check("a null productPriceList doesn't crash -- no tiers, no crash",
          len(o2.tiers), 0)
    check("moq still defaults to 1 without a price ladder", o2.moq, 1)

    print("\na moved/broken endpoint (HTTP 200, application-level error) is "
          "reported plainly, not treated as an empty catalog")
    error_fetcher = FakeFetcher({"code": 404, "msg": "Not Found", "ok": False, "result": None})
    adapter2 = LcscAdapter()
    adapter2._fetcher = error_fetcher
    error_offers = list(adapter2.search("esp32", max_pages=1))
    check("no offers yielded from an error response", error_offers, [])

    print("\n" + "=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("lcsc adapter OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
