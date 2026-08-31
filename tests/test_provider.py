"""Provider-driver tests: path resolution and field mapping, fully offline.

The mapping layer is where a misconfigured provider silently produces an empty
catalog, so it is worth testing hard. Payloads below are synthesized to match the
*shapes declared in providers.yaml* -- so these tests verify the shipped presets are
at least internally consistent, and that a wrong path is reported rather than
swallowed.

They do NOT prove any vendor's live API matches its preset. Only
``provider-probe`` against a real key can do that.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_prov_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["CN_PROVIDER_KEY"] = "test-key"

from sourcehub.scrapers.provider import (  # noqa: E402
    ProviderClient,
    ProviderError,
    dig,
    get_preset,
    probe,
)

FAILS: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


def check_true(label, got):
    check(label, bool(got), True)


# ----------------------------------------------------------------- fake network


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class FakeFetcher:
    """Records the request and replays a canned payload."""

    def __init__(self, payload):
        self.payload = payload
        self.last = {}

    def get(self, url, params=None, headers=None, referer=None, expect_json=False):
        self.last = {"url": url, "params": params or {}, "headers": headers or {}}
        return _Resp(self.payload)

    def post(self, url, data=None, json_body=None, headers=None, referer=None):
        self.last = {"url": url, "body": json_body, "headers": headers or {}}
        return _Resp(self.payload)

    def close(self):
        pass


# --------------------------------------------------------------- path resolution


def test_dig():
    print("\npath resolution")
    obj = {
        "Result": {"Items": {"Content": [{"Id": "1"}, {"Id": "2"}]}},
        "Pictures": [{"Url": "a.jpg"}, {"Url": "b.jpg"}],
        "Price": {"ConvertedPriceWithoutSign": "18.50", "CurrencyCode": "CNY"},
        "rows": [{"a": {"b": "x"}}, {"a": {"b": "y"}}],
        "zero": 0,
        "blank": "",
    }
    check("dotted", dig(obj, "Price.CurrencyCode"), "CNY")
    check("deep dotted", dig(obj, "Result.Items.Content[0].Id"), "1")
    check("fan-out", dig(obj, "Pictures[].Url"), ["a.jpg", "b.jpg"])
    check("nested fan-out", dig(obj, "rows[].a.b"), ["x", "y"])
    check("bare array", len(dig(obj, "Result.Items.Content")), 2)
    check("missing path", dig(obj, "No.Such.Path"), None)
    check("missing with default", dig(obj, "No.Such", "fallback"), "fallback")
    check("index out of range", dig(obj, "Pictures[9].Url"), None)
    check("zero is preserved", dig(obj, "zero"), 0)

    # Candidate lists: first non-empty wins, and empty string is treated as empty.
    check("candidate list first hit", dig(obj, ["Price.CurrencyCode", "nope"]), "CNY")
    check("candidate list falls through", dig(obj, ["nope", "Price.CurrencyCode"]), "CNY")
    check("candidate skips blank", dig(obj, ["blank", "Price.CurrencyCode"]), "CNY")
    check("all candidates miss", dig(obj, ["a", "b"], "d"), "d")

    # Repeated segment names must not confuse the fan-out slicing.
    nested = {"a": [{"a": [{"b": "deep"}]}]}
    check("repeated segment names", dig(nested, "a[].a[].b"), ["deep"])


# ------------------------------------------------------------------ otapi shape


OTAPI_SEARCH = {
    "Result": {
        "Items": {
            "Items": {
                "Content": [
                    {
                        "Id": "678901234",
                        "Title": "TWS Bluetooth 5.3 Wireless Earbuds",
                        "ExternalItemUrl": "https://detail.1688.com/offer/678901234.html",
                        "Price": {"ConvertedPriceWithoutSign": "18.50", "CurrencyCode": "CNY"},
                        "MinimumOrderQuantity": 2,
                        "MainPictureUrl": "//cbu01.alicdn.com/x.jpg",
                        "Pictures": [{"Url": "//cbu01.alicdn.com/x.jpg"},
                                     {"Url": "//cbu01.alicdn.com/y.jpg"}],
                        "VendorName": "深圳市声美电子有限公司",
                        "Location": {"City": "深圳", "State": "广东"},
                        "SalesInLast30Days": 4210,
                        "CategoryName": "数码配件 > 蓝牙耳机",
                        "Attributes": [
                            {"PropertyName": "蓝牙版本", "Value": "5.3"},
                            {"PropertyName": "电池容量", "Value": "40mAh"},
                        ],
                        "QuantityRanges": [
                            {"MinQuantity": 2, "MaxQuantity": 99,
                             "Price": {"ConvertedPriceWithoutSign": "18.50"}},
                            {"MinQuantity": 100, "MaxQuantity": 999,
                             "Price": {"ConvertedPriceWithoutSign": "15.80"}},
                            {"MinQuantity": 1000, "MaxQuantity": None,
                             "Price": {"ConvertedPriceWithoutSign": "13.20"}},
                        ],
                    }
                ]
            }
        }
    }
}


def test_otapi_mapping():
    print("\notapi preset mapping")
    fetcher = FakeFetcher(OTAPI_SEARCH)
    client = ProviderClient("otapi", "1688", fetcher)

    offers = list(client.search("earbuds", page=1))
    check("one offer mapped", len(offers), 1)
    if not offers:
        return
    o = offers[0]
    check("id", o.site_product_id, "678901234")
    check("title", o.title, "TWS Bluetooth 5.3 Wireless Earbuds")
    check("url", o.url, "https://detail.1688.com/offer/678901234.html")
    check("currency", o.currency, "CNY")
    check("images (protocol fixed)", o.image_urls[0], "https://cbu01.alicdn.com/x.jpg")
    check("image count deduped by mapping", len(o.image_urls), 2)
    check("seller", o.seller_name, "深圳市声美电子有限公司")
    check("orders", o.orders_count, 4210)
    check("specs mapped", len(o.specs), 2)
    check("spec key", o.specs[0].key, "蓝牙版本")
    check("tiers mapped", len(o.tiers), 3)
    # MOQ and headline price must come from the ladder, not the top-level field.
    check("moq from ladder", o.moq, 2)
    check("min price from ladder", o.price_min, 13.20)
    check("max price from ladder", o.price_max, 18.50)
    check_true("agent-required note attached", "forwarding agent" in (o.fees_note or ""))

    # Auth + placeholder expansion actually happened
    check("auth key sent as query param", fetcher.last["params"].get("instanceKey"), "test-key")
    check_true("keyword substituted into request",
               "earbuds" in str(fetcher.last["params"]))


# --------------------------------------------------------------- rapidapi shape


RAPIDAPI_SEARCH = {
    "result": {
        "item": [
            {
                "num_iid": "552211",
                "title": "USB C Hub 8 in 1",
                "detail_url": "//item.taobao.com/item.htm?id=552211",
                "price": "89.00",
                "pic_url": "//img.alicdn.com/hub.jpg",
                "nick": "科技数码专营店",
                "location": "广东 深圳",
                "sales": "1.2万",
            }
        ]
    }
}


def test_rapidapi_mapping():
    print("\nrapidapi preset mapping")
    fetcher = FakeFetcher(RAPIDAPI_SEARCH)
    client = ProviderClient("rapidapi_generic", "taobao", fetcher)
    offers = list(client.search("usb hub"))
    check("one offer mapped", len(offers), 1)
    if not offers:
        return
    o = offers[0]
    check("id", o.site_product_id, "552211")
    check("price", o.price_min, 89.0)
    # Protocol-relative URLs must be absolutized -- they are URL-encoded into agent
    # deep links, where a bare "//" prefix produces a dead link.
    check("url absolutized", o.url, "https://item.taobao.com/item.htm?id=552211")
    check("seller", o.seller_name, "科技数码专营店")
    check("auth sent as header", fetcher.last["headers"].get("X-RapidAPI-Key"), "test-key")
    check_true("extra header applied",
               fetcher.last["headers"].get("X-RapidAPI-Host") == "taobao-api.p.rapidapi.com")


def test_url_fallback():
    print("\nurl fallback")
    payload = {"result": {"item": [{"num_iid": "999", "title": "No URL Item", "price": 5}]}}
    client = ProviderClient("rapidapi_generic", "1688", FakeFetcher(payload))
    offers = list(client.search("x"))
    check("one offer", len(offers), 1)
    if offers:
        # No detail_url in the payload -> built from the site's item_url_template.
        check("url built from template", offers[0].url,
              "https://detail.1688.com/offer/999.html")


# ------------------------------------------------------------------ capabilities


def test_capabilities():
    print("\ncapabilities and errors")
    lookup = get_preset("agent_lookup")
    check("agent preset has detail", bool(lookup.get("detail")), True)
    # The important one: agents do item lookup, not discovery. If this ever gains a
    # search section the crawler will start using it, so assert the shipped default.
    check("agent preset has NO search", bool(lookup.get("search")), False)

    client = ProviderClient("agent_lookup", "taobao", FakeFetcher({}),
                            base_url="https://example.test")
    check("can_search False", client.can_search, False)
    check("can_detail True", client.can_detail, True)

    try:
        ProviderClient("does_not_exist", "taobao", FakeFetcher({}))
        check("unknown preset raises", False, True)
    except ProviderError as e:
        check_true("unknown preset raises", "unknown provider preset" in str(e))

    try:
        ProviderClient("otapi", "dhgate", FakeFetcher({}))
        check("unsupported site raises", False, True)
    except ProviderError as e:
        check_true("unsupported site raises", "does not declare support" in str(e))


# ----------------------------------------------------------------------- probe


def test_probe():
    print("\nprobe diagnostics")
    # A payload whose items live somewhere the preset does NOT expect.
    wrong = {"data": {"products": [{"id": "1", "name": "Thing"}], "total": 1}}
    report = probe("otapi", "taobao", "x", FakeFetcher(wrong))
    check("reports zero items", report["items_found"], 0)
    check_true("suggests the real array path",
               any("data.products" in c for c in report["candidate_item_paths"]))

    good = probe("otapi", "1688", "x", FakeFetcher(OTAPI_SEARCH))
    check("finds items with correct preset", good["items_found"], 1)
    check_true("reports mapped sample", good["mapped"] and good["mapped"]["id"] == "678901234")


# ------------------------------------------------------- driver resolution

def _adapter(site_key="taobao", **site_cfg):
    """Build a real adapter with an inline crawl config."""
    from sourcehub.config import CrawlConfig
    from sourcehub.scrapers.registry import get_adapter

    return get_adapter(site_key, CrawlConfig({"sites": {site_key: site_cfg}}))


def _with_key(value: str | None):
    """Set/clear CN_PROVIDER_KEY and bust the cached Settings."""
    from sourcehub.config import get_settings

    if value is None:
        os.environ.pop("CN_PROVIDER_KEY", None)
    else:
        os.environ["CN_PROVIDER_KEY"] = value
    get_settings.cache_clear()


def test_driver_resolution():
    print("\ndriver resolution")
    _with_key("test-key")

    check("browser preset", _adapter(driver="browser").drivers, ("browser", "browser"))
    # hybrid is the headline: discovery on the browser, enrichment over the API.
    check("hybrid preset",
          _adapter(driver="hybrid", provider_preset="otapi").drivers,
          ("browser", "provider"))
    check("provider preset",
          _adapter(driver="provider", provider_preset="otapi").drivers,
          ("provider", "provider"))
    check("unknown driver falls back",
          _adapter(driver="teleport").drivers, ("browser", "browser"))

    # The important downgrade: a lookup-only agent asked to do everything must
    # resolve to hybrid, not to an empty crawl.
    check("lookup-only agent auto-hybrids",
          _adapter(driver="provider", provider_preset="agent_lookup").drivers,
          ("browser", "provider"))

    # Explicit per-half overrides win over the shorthand.
    check("explicit search override",
          _adapter(driver="browser", search_driver="provider",
                   provider_preset="otapi").drivers,
          ("provider", "browser"))
    check("explicit detail override",
          _adapter(driver="provider", detail_driver="browser",
                   provider_preset="otapi").drivers,
          ("provider", "browser"))

    # No credentials at all -> everything degrades to the browser rather than failing.
    _with_key(None)
    base_url = os.environ.pop("CN_PROVIDER_BASE_URL", None)
    check("no key degrades to browser",
          _adapter(driver="hybrid", provider_preset="otapi").drivers,
          ("browser", "browser"))
    check("provider client is None without creds",
          _adapter(driver="hybrid").provider, None)
    if base_url:
        os.environ["CN_PROVIDER_BASE_URL"] = base_url
    _with_key("test-key")


def test_hybrid_detail_flow():
    print("\nhybrid detail flow")
    from sourcehub.scrapers.base import RawOffer

    _with_key("test-key")
    adapter = _adapter("1688", driver="hybrid", provider_preset="otapi")
    adapter._fetcher = FakeFetcher({"Result": {"Item": {
        "Id": "678901234",
        "Title": "TWS Bluetooth 5.3 Wireless Earbuds",
        "ExternalItemUrl": "https://detail.1688.com/offer/678901234.html",
        "Price": {"ConvertedPriceWithoutSign": "18.50", "CurrencyCode": "CNY"},
        "Attributes": [{"PropertyName": "蓝牙版本", "Value": "5.3"}],
        "Description": "Factory direct earbuds",
    }}})

    # A shallow offer as produced by browser discovery.
    offer = RawOffer(
        site_key="1688", site_product_id="678901234",
        url="https://detail.1688.com/offer/678901234.html",
        title="蓝牙耳机", currency="CNY", price_min=18.50,
        seller_name="from-browser-search", orders_count=4210,
    )
    enriched = adapter.fetch_detail(offer)

    check("enriched via provider", enriched.detail_fetched, True)
    check("specs came from the API", len(enriched.specs), 1)
    check("description came from the API", enriched.description, "Factory direct earbuds")
    # Fields the listing page had must survive the merge -- detail calls often omit them.
    check("browser-only field preserved", enriched.seller_name, "from-browser-search")
    check("browser-only orders preserved", enriched.orders_count, 4210)
    check_true("no browser was ever started", adapter._browser is None)


def main() -> int:
    for fn in (test_dig, test_otapi_mapping, test_rapidapi_mapping, test_url_fallback,
               test_capabilities, test_probe, test_driver_resolution,
               test_hybrid_detail_flow):
        fn()
    print("\n" + "=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("provider driver OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
