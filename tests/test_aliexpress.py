"""AliExpress: the official Affiliate API path -- field mapping (including the
real, live price-parsing bug this session fixed) and category-tree discovery,
against a real trimmed captured response.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_aliexpress_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"
os.environ["ALIEXPRESS_APP_KEY"] = "test_key"
os.environ["ALIEXPRESS_APP_SECRET"] = "test_secret"
os.environ["ALIEXPRESS_TRACKING_ID"] = "test"

from sourcehub.config import get_settings  # noqa: E402
from sourcehub.scrapers.aliexpress import AliExpressAdapter  # noqa: E402
from sourcehub.util.http import Response  # noqa: E402

get_settings.cache_clear()

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
        self.calls.append({"url": url, "params": params or {}})
        return Response(url=url, status=200, text=__import__("json").dumps(self.payload))


# Trimmed from a real aliexpress.affiliate.product.query response (2026-09-08).
# The API returns typed numeric strings ("6.17"), not formatted price text
# ("$6.17") -- the bug this test guards against is calling parse_price() on
# these directly, which silently returns None for a bare number with no
# currency symbol.
PRODUCT_RESPONSE = {
    "aliexpress_affiliate_product_query_response": {
        "resp_result": {
            "result": {
                "current_record_count": 2,
                "products": {
                    "product": [
                        {
                            "product_id": 3256812497109248,
                            "product_title": "Wireless Bluetooth Earbuds",
                            "product_detail_url": "https://www.aliexpress.com/item/3256812497109248.html?x=1",
                            "target_sale_price": "4.69",
                            "target_sale_price_currency": "USD",
                            "target_original_price": "10.43",
                            "original_price": "69.62",
                            "original_price_currency": "CNY",
                            "evaluate_rate": "96.5%",
                            "lastest_volume": "2000",
                            "shop_name": "Real Store",
                            "shop_url": "https://www.aliexpress.com/store/1105550285",
                            "second_level_category_name": "Games & Accessories",
                            "product_main_image_url": "https://ae-pic-a1.aliexpress-media.com/kf/main.jpg",
                            "product_small_image_urls": {"string": ["https://ae-pic-a1.aliexpress-media.com/kf/s1.jpg"]},
                        },
                        # Missing target_sale_price entirely -- falls back to sale_price.
                        {
                            "product_id": 3256810000000001,
                            "product_title": "USB Hub 4 Port",
                            "product_detail_url": "https://www.aliexpress.com/item/3256810000000001.html",
                            "sale_price": "6.17",
                            "target_sale_price_currency": "USD",
                        },
                    ]
                },
            }
        }
    }
}

# Trimmed from a real aliexpress.affiliate.category.get response -- one
# top-level category with two leaf children, plus a second top-level category
# with no children (itself a leaf).
CATEGORY_RESPONSE = {
    "aliexpress_affiliate_category_get_response": {
        "resp_result": {
            "result": {
                "categories": {
                    "category": [
                        {"category_name": "Computer & Office", "category_id": 7},
                        {"parent_category_id": 7, "category_name": "Laptops", "category_id": 702},
                        {"parent_category_id": 7, "category_name": "Computer Peripherals", "category_id": 200001081},
                        {"category_name": "Food", "category_id": 2},
                    ]
                }
            }
        }
    }
}


def main() -> int:
    print("a real product-search API response maps every field the adapter promises")
    adapter = AliExpressAdapter()
    fake = FakeFetcher(PRODUCT_RESPONSE)
    adapter._fetcher = fake
    offers = list(adapter.crawl_category("200001081", max_pages=1))

    check("both items mapped", len(offers), 2)
    call = fake.calls[0]
    check("category_ids sent, not keywords", call["params"]["category_ids"], "200001081")
    check("keywords not sent for a category crawl", "keywords" in call["params"], False)

    o = offers[0]
    check("site_product_id mapped", o.site_product_id, "3256812497109248")
    check("title mapped", o.title, "Wireless Bluetooth Earbuds")
    check("a bare numeric price string parses correctly (the fixed bug)",
          o.price_min, 4.69)
    check("original price used as the ceiling", o.price_max, 10.43)
    check("currency mapped", o.currency, "USD")
    check("rating parsed from a percent string", o.rating, 96.5)
    check("orders_count parsed", o.orders_count, 2000)
    check("image mapped", o.image_urls[0], "https://ae-pic-a1.aliexpress-media.com/kf/main.jpg")

    o2 = offers[1]
    check("falls back to sale_price when target_sale_price is absent",
          o2.price_min, 6.17)
    check("no original price -> no price_max", o2.price_max, None)

    print("\ncategory discovery keeps only leaves (nothing else lists as a parent)")
    fake2 = FakeFetcher(CATEGORY_RESPONSE)
    adapter2 = AliExpressAdapter()
    adapter2._fetcher = fake2
    seeds = adapter2.category_seeds()
    seed_ids = {cid for _, cid in seeds}
    check("Computer & Office (has children) excluded", "7" in seed_ids, False)
    check("Laptops (a leaf) included", "702" in seed_ids, True)
    check("Computer Peripherals (a leaf) included", "200001081" in seed_ids, True)
    check("Food (childless top-level, itself a leaf) included", "2" in seed_ids, True)
    check("exactly 3 leaves", len(seeds), 3)

    print("\nan application-level error response yields no offers, and no seeds")
    error_fetcher = FakeFetcher({"error_response": {"code": "IllegalAccess", "msg": "bad sign"}})
    adapter3 = AliExpressAdapter()
    adapter3._fetcher = error_fetcher
    check("no offers yielded from an error response",
          list(adapter3.crawl_category("7", max_pages=1)), [])
    check("no seeds yielded from an error response", adapter3.category_seeds(), [])

    print("\nwithout credentials, category crawling is a no-op (no HTML fallback exists for it)")
    os.environ["ALIEXPRESS_APP_KEY"] = ""
    get_settings.cache_clear()
    adapter4 = AliExpressAdapter()
    check("no seeds without credentials", adapter4.category_seeds(), [])
    check("no offers from crawl_category without credentials",
          list(adapter4.crawl_category("7")), [])
    os.environ["ALIEXPRESS_APP_KEY"] = "test_key"
    get_settings.cache_clear()

    print("\n" + "=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("aliexpress adapter OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
