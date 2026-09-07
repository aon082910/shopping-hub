"""Temu: field mapping and the ``window.rawData`` extractor, against the real
page shape captured live (2026-09-07) via a HAR export from a real logged-in
session -- this project has no way to reach Temu's logged-in experience
itself (an anonymous session hits a mandatory sign-in wall within seconds,
even for the homepage), so this is built from the user's own capture, not
guessed from general knowledge of Temu's API.

Confirmed live: unlike USFans, Temu's product page URLs and ``goodsId`` are
the real thing -- no obfuscation -- so this doesn't need any of the
resolve/skip_resolve machinery providers.yaml's usfans preset needed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_temu_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sourcehub.scrapers.base import RawOffer  # noqa: E402
from sourcehub.scrapers.temu import TemuAdapter, _extract_raw_data  # noqa: E402

FAILS: list[str] = []


def check(label, got, want=True) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


# Item 0, trimmed: real, live -- and specifically the tricky case. Its
# tagsInfo.adTags[].extMap.ad value is itself a JSON-*string* full of braces
# ('{"ad_id_str":...}' with escaped quotes) -- if the bracket-matcher ever
# stopped treating string contents as opaque, this is the item that would
# break first (truncating the object well before its real end).
AD_ITEM = {
    "goodsId": 601100119291036,
    "title": "KODAK USB C Multiport Adapter(T341/T343), 4/5-in-1",
    "itemType": 0,
    "priceInfo": {
        "price": 392, "currency": "USD", "priceStr": "$3.92",
        "marketPrice": 3450, "marketPriceStr": "$34.50",
    },
    "image": {"url": "https://img.kwcdn.com/product/fancy/ad-item.jpg"},
    "seoLinkUrl": "/kodak-usb-c-multiport-adapter-g-601100119291036.html?search_key=usb+hub",
    "linkUrl": "goods.html?goods_id=601100119291036",
    "tagsInfo": {
        "adTags": [{
            "color": "#FFFFFF",
            "extMap": {
                "ad": ('{"ad_id_str":"100000000053046074","goods_id":601100119291036,'
                       '"is_fallback_bid":0,"mall_id":634418220112086}')
            },
            "backColor": "#000000", "text": "AD", "font": 10,
        }],
    },
}

# Item 1, trimmed: a normal (non-ad) listing with the review/sales fields.
NORMAL_ITEM = {
    "goodsId": 601104367612078,
    "title": "[4K HD HDTV Hub] 7-in-1 SD/TF With Card Reader HDTV Hub, USB "
             "2.0/3.0 Docking Station, 3 USB Ports, PD 100W Charging Port",
    "itemType": 0,
    "priceInfo": {
        "price": 547, "currency": "USD", "priceStr": "$5.47",
        "marketPrice": 5152, "marketPriceStr": "$51.52",
    },
    "image": {"url": "https://img.kwcdn.com/product/fancy/normal-item.jpg"},
    "seoLinkUrl": "/7-in-1-sd-tf-hdtv-hub-g-601104367612078.html?"
                  "_oak_mp_inf=abc123&search_key=usb%20hub",
    "linkUrl": "goods.html?goods_id=601104367612078",
    "salesNum": "122",
    "comment": {"goodsScore": 4.3, "hiddenComment": True, "commentNumTips": "6"},
}

RAW_DATA = {
    "store": {
        "goodsList": [
            {"data": AD_ITEM, "customizeReportData": {}},
            {"data": NORMAL_ITEM, "customizeReportData": {}},
        ]
    }
}

PAGE_HTML = (
    "<html><head></head><body><script>"
    "window.rawData=" + json.dumps(RAW_DATA) +
    ";window.otherVar={\"unrelated\": true};"
    "</script></body></html>"
)


def main() -> int:
    print("_extract_raw_data survives a nested JSON-string full of braces")
    extracted = _extract_raw_data(PAGE_HTML)
    check("parsed successfully", extracted is not None)
    check("goodsList has both items",
          len(extracted["store"]["goodsList"]) if extracted else 0, 2)

    print("\na missing window.rawData is a clean miss, not a crash")
    check("no rawData -> None", _extract_raw_data("<html>no data here</html>"), None)

    print("\nsearch() maps every field the adapter promises")
    adapter = TemuAdapter.__new__(TemuAdapter)
    adapter.fetch_html = lambda url, phase="search": PAGE_HTML
    offers = list(adapter.search("usb hub"))
    check("both items mapped", len(offers), 2)

    ad_offer, normal_offer = offers[0], offers[1]
    check("ad item's price converted from cents", ad_offer.price_min, 3.92)
    check("ad item's real numeric goodsId used as site_product_id",
          ad_offer.site_product_id, "601100119291036")
    check("ad item's url built from seoLinkUrl, tracking params stripped",
          ad_offer.url,
          "https://www.temu.com/kodak-usb-c-multiport-adapter-g-601100119291036.html")

    check("normal item's price converted from cents", normal_offer.price_min, 5.47)
    check("normal item's currency", normal_offer.currency, "USD")
    check("normal item's image mapped", normal_offer.image_urls,
          ["https://img.kwcdn.com/product/fancy/normal-item.jpg"])
    check("normal item's rating mapped from comment.goodsScore",
          normal_offer.rating, 4.3)
    check("normal item's review_count parsed from commentNumTips",
          normal_offer.review_count, 6)
    check("normal item's orders_count parsed from salesNum",
          normal_offer.orders_count, 122)
    check("moq is always 1 (retail)", normal_offer.moq, 1)
    check("seller is Temu itself", normal_offer.seller_name, "Temu")

    print("\nan item missing both goodsId and title is skipped, not crashed on")
    empty_html = PAGE_HTML.replace(
        json.dumps(RAW_DATA),
        json.dumps({"store": {"goodsList": [{"data": {}, "customizeReportData": {}}]}}),
    )
    adapter2 = TemuAdapter.__new__(TemuAdapter)
    adapter2.fetch_html = lambda url, phase="search": empty_html
    check("empty item yields nothing", list(adapter2.search("usb hub")), [])

    print("\nfetch_detail() maps every field, from a second real captured page "
          "(a storage cabinet with 10 real colour variants)")
    detail_store = {
        "goods": {
            "goodsId": 601099572358007,
            "goodsName": "Storage Cabinet with 5 Drawers: Fabric Closet "
                         "Organizer, Metal Frame And Wood Tabletop",
            "minOnSalePrice": 2570, "maxOnSalePrice": 3610,
            "gallery": [
                {"url": "https://img.kwcdn.com/product/fancy/img1.jpg"},
                {"url": "https://img.kwcdn.com/product/fancy/img2.jpg"},
            ],
            "goodsProperty": [
                {"key": "Material", "values": ["Metal"]},
                {"key": "Style", "values": ["Contemporary"]},
            ],
        },
        "mall": {"mallData": {"mallName": "Modern Elegance", "mallStar": 4.6}},
        "reviewStore": {"showScore": 4.5, "reviewNum": 12397},
        "productDetail": {"floorList": [
            {"items": [{"text": "Dear customer, the height is 21 inches."}]},
            {"items": [{"url": "https://img.kwcdn.com/product/fancy/detail1.jpg"}]},
        ]},
        "sku": [
            {"skuId": 17595036941674, "salePrice": 2565, "stockQuantity": 1000,
             "specs": [{"specKey": "Color", "specValue": "Black - 5 Drawers"}],
             "thumbUrl": "https://img.kwcdn.com/product/fancy/black.jpg"},
            {"skuId": 88418679805188, "salePrice": 3595, "stockQuantity": 0,
             "specs": [{"specKey": "Color", "specValue": "Light Gray - 5 Drawer"}],
             "thumbUrl": "https://img.kwcdn.com/product/fancy/lightgray.jpg"},
        ],
    }
    detail_html = "<html><head></head><body><script>window.rawData=" + \
        json.dumps({"store": detail_store}) + ";</script></body></html>"

    adapter3 = TemuAdapter.__new__(TemuAdapter)
    adapter3.fetch_html = lambda url, phase="detail": detail_html
    shallow = RawOffer(
        site_key="temu", site_product_id="601099572358007",
        url="https://www.temu.com/storage-cabinet.html",
        title="placeholder from search", currency="USD", price_min=25.70,
    )
    enriched = adapter3.fetch_detail(shallow)

    check("detail_fetched flagged", enriched.detail_fetched)
    check("title replaced with the fuller goodsName", enriched.title,
          "Storage Cabinet with 5 Drawers: Fabric Closet Organizer, Metal "
          "Frame And Wood Tabletop")
    check("price_min converted from cents", enriched.price_min, 25.70)
    check("price_max converted from cents (search only ever had one price)",
          enriched.price_max, 36.10)
    check("gallery images appended", enriched.image_urls,
          ["https://img.kwcdn.com/product/fancy/img1.jpg",
           "https://img.kwcdn.com/product/fancy/img2.jpg"])
    check("seller replaced with the real mall name", enriched.seller_name,
          "Modern Elegance")
    check("rating mapped", enriched.rating, 4.5)
    check("review_count mapped", enriched.review_count, 12397)
    specs = {s.key: s.value for s in enriched.specs}
    check("goodsProperty mapped to specs", specs,
          {"Material": "Metal", "Style": "Contemporary"})
    check("description built from productDetail's text floors",
               "21 inches" in (enriched.description or ""))

    check("both real SKU variants mapped", len(enriched.variants), 2)
    in_stock_variant = next(v for v in enriched.variants if v.sku == "17595036941674")
    check("in-stock variant price converted from cents", in_stock_variant.price, 25.65)
    check("in-stock variant attrs from specs", in_stock_variant.attrs,
          {"Color": "Black - 5 Drawers"})
    check("in-stock variant flagged in_stock", in_stock_variant.in_stock)

    out_of_stock_variant = next(v for v in enriched.variants if v.sku == "88418679805188")
    check("zero-stock variant flagged out of stock",
               not out_of_stock_variant.in_stock)

    print("\nfetch_detail() on a page with no window.rawData is a clean "
          "no-op, not a crash (e.g. the saved session expired mid-crawl)")
    adapter4 = TemuAdapter.__new__(TemuAdapter)
    adapter4.fetch_html = lambda url, phase="detail": "<html>sign in wall</html>"
    unchanged = RawOffer(site_key="temu", site_product_id="x", url="https://x",
                         title="original title", currency="USD")
    result = adapter4.fetch_detail(unchanged)
    check("title untouched", result.title, "original title")
    check("not marked detail_fetched", not result.detail_fetched)

    print("\n" + "=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("temu adapter OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
