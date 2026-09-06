"""Best Buy: field mapping against the shapes documented at
https://bestbuyapis.github.io/api-documentation/ -- there is no way to verify this
against a live account without a key (the same honesty caveat test_provider.py
carries for providers.yaml), so this checks that the mapping is internally
consistent and matches the documented attribute names, not that Best Buy's API
still looks exactly like this today.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_bestbuy_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sourcehub.config import get_settings  # noqa: E402
from sourcehub.scrapers.bestbuy import BestBuyAdapter, _escape  # noqa: E402
from sourcehub.util.http import Response  # noqa: E402

FAILS: list[str] = []


def check(label, got, want=True) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


# Shaped from the documentation's own examples: sku/name/salePrice from "Returned
# Attributes", upc/manufacturer/modelNumber/color/condition from "Detail",
# regularPrice/onSale from "Pricing", onlineAvailability/orderable from
# "Availability", shippingLevelsOfService from "Shipping and Delivery",
# image/thumbnailImage/largeImage from "Images", class/subclass from
# "Categorizations".
SAMPLE_PRODUCT = {
    "sku": 6535138,
    "name": "Anker - 7-in-1 USB-C Hub",
    "url": "https://www.bestbuy.com/site/anker-hub/6535138.p",
    "manufacturer": "Anker",
    "modelNumber": "A83870A1",
    "upc": "885909950805",
    "color": "Black",
    "condition": "New",
    "salePrice": 34.99,
    "regularPrice": 39.99,
    "onSale": True,
    "onlineAvailability": True,
    "orderable": "Available",
    "shortDescription": "Connect a monitor, drives and more through one port.",
    "class": "Computer Cables & Connectors",
    "subclass": "USB Hubs",
    "image": "https://pisces.bbystatic.com/image2/full.jpg",
    "thumbnailImage": "https://pisces.bbystatic.com/image2/thumb.jpg",
    "largeImage": "https://pisces.bbystatic.com/image2/large.jpg",
    "customerReviewAverage": 4.6,
    "customerReviewCount": 812,
    "shippingLevelsOfService": [
        {"serviceLevelId": 1, "serviceLevelName": "Standard", "unitShippingPrice": 0.0},
    ],
    "weight": "0.3 lbs",
    "depth": "0.8",
    "width": "4.5",
    "height": "0.6",
}


class FakeFetcher:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, *, params=None, headers=None, referer=None, expect_json=False):
        self.calls.append((url, params or {}))
        return Response(url=url, status=200, text=json.dumps(self.payload))


def main() -> None:
    print("no API key -> skips entirely, no network call")
    a = BestBuyAdapter()
    offers = list(a.search("usb c hub"))
    check("nothing yielded without a key", offers, [])

    print("\nwith a key, a search page maps every documented field")
    os.environ["BESTBUY_API_KEY"] = "test-key-123"
    get_settings.cache_clear()  # get_settings() is lru_cache'd for the app's own
    # lifetime (one real process, one .env read); this test changes the env
    # mid-process, which the real app never does.
    a2 = BestBuyAdapter()
    fake = FakeFetcher({"products": [SAMPLE_PRODUCT], "totalPages": 1, "currentPage": 1})
    a2._fetcher = fake
    offers2 = list(a2.search("usb c hub"))
    check("one offer parsed", len(offers2), 1)
    o = offers2[0]
    check("title", o.title, "Anker - 7-in-1 USB-C Hub")
    check("site_product_id is the sku", o.site_product_id, "6535138")
    check("url passed through", o.url, SAMPLE_PRODUCT["url"])
    check("price is the sale price, not the regular price", o.price_min, 34.99)
    check("moq is always 1 (retail)", o.moq, 1)
    check("brand", o.brand, "Anker")
    check("model", o.model, "A83870A1")
    check("gtin normalized and checksum-validated", o.gtin, "00885909950805")
    check("seller is Best Buy itself, not a marketplace seller", o.seller_name, "Best Buy")
    check("in stock", o.in_stock, True)
    check("rating", o.rating, 4.6)
    check("review count", o.review_count, 812)
    check("category path joins class and subclass",
          o.category_path, "Computer Cables & Connectors > USB Hubs")
    check("primary image present", SAMPLE_PRODUCT["image"] in o.image_urls)
    check("free shipping recognized as such", o.shipping_free, True)
    specs = {s.key: s.value for s in o.specs}
    check("condition spec recorded", specs.get("Condition"), "New")
    check("color spec recorded", specs.get("Color"), "Black")
    check("weight spec already has a unit, left alone", specs.get("Weight"), "0.3 lbs")
    check("package dimensions combined for the freight parser",
          specs.get("Package Dimensions"), "0.8 x 4.5 x 0.6 in")

    print("\nthe request URL matches Best Buy's documented (search=a&search=b) syntax")
    url, params = fake.calls[0]
    check("filter expression embedded in the path, not a query param",
          url, "https://api.bestbuy.com/v1/products(search=usb&search=c&search=hub)")
    check("apiKey sent", params.get("apiKey"), "test-key-123")

    print("\nquery-syntax characters are stripped from search terms, not sent raw")
    check("parens stripped", _escape("(evil)"), "evil")
    check("boolean operators stripped", _escape("a&b|c"), "abc")
    check("field-search operator stripped", _escape("sku=123"), "sku123")

    print("\npagination stops at totalPages rather than looping forever")
    fake2 = FakeFetcher({"products": [SAMPLE_PRODUCT], "totalPages": 1, "currentPage": 1})
    a3 = BestBuyAdapter()
    a3._fetcher = fake2
    list(a3.search("usb c hub", max_pages=5))
    check("only one page requested once totalPages says so", len(fake2.calls), 1)

    print("\nfetch_detail adds the long description and features, without a key it's a no-op")
    del os.environ["BESTBUY_API_KEY"]
    get_settings.cache_clear()
    a4 = BestBuyAdapter()
    from sourcehub.scrapers.base import RawOffer

    offer_stub = RawOffer(site_key="bestbuy", site_product_id="1", url="https://x", title="t")
    a4.fetch_detail(offer_stub)
    check("no key -> detail is a no-op, still marked fetched is not required",
          offer_stub.description, None)

    print("\n" + "=" * 60)
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(" -", f)
        sys.exit(1)
    print("bestbuy OK")


if __name__ == "__main__":
    main()
