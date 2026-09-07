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


def test_probe_on_a_detail_only_preset():
    """agent_lookup and usfans have no search section -- a bare keyword probe
    against either used to raise a bare "no 'search' section" ProviderError
    with no indication of what to do about it. Reported live against usfans.
    """
    print("\nprobe on a detail-only preset (no search section)")
    try:
        probe("agent_lookup", "taobao", "x", FakeFetcher({}))
        check("missing --url on a detail-only preset raises", False, True)
    except ProviderError as e:
        check_true("names the fix", "--url" in str(e) and "item lookup only" in str(e))

    # agent_lookup's detail_path is ["data", "result", "item"] -- a list of
    # candidate *full* paths (first hit wins), not one dotted chain, so the item
    # fields must sit directly under one of those top-level keys.
    detail_payload = {"item": {
        "itemId": "12345", "itemName": "Signed Cable Organizer",
        "price": 9.99, "currency": "USD", "mainImgUrl": "https://example.test/a.jpg",
    }}
    report = probe(
        "agent_lookup", "taobao", "x", FakeFetcher(detail_payload),
        item_url="https://item.taobao.com/item.htm?id=12345",
    )
    check("detail mode is reported", report["mode"], "detail")
    check("no resolve step for a preset that doesn't declare one", report["resolve"], None)
    check_true("mapped the detail response", report["mapped"] is not None)
    check("mapped id", report["mapped"]["id"], "12345")
    check("mapped title", report["mapped"]["title"], "Signed Cable Organizer")
    check("no error fields on a clean success", report["status_fields"], {})

    print("\nan API-level error (401 while logged out) is surfaced directly, "
          "not left for node_keys to hint at")
    error_payload = {"code": 401, "msg": "Please log in to continue", "data": None, "success": False}
    error_report = probe(
        "agent_lookup", "taobao", "x", FakeFetcher(error_payload),
        item_url="https://item.taobao.com/item.htm?id=1",
    )
    check("still reports detail mode", error_report["mode"], "detail")
    check("no mappable item", error_report["mapped"], None)
    check("the real status is surfaced, not just 'node_keys == top_level_keys'",
          error_report["status_fields"], {"code": 401, "msg": "Please log in to continue", "success": False})


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


def test_no_key_preset_still_activates_the_provider():
    """usfans (and anything else with `auth: mode: none`) authenticates via a
    logged-in browser session, not an API key -- it must not be gated behind
    CN_PROVIDER_KEY/CN_PROVIDER_BASE_URL the way otapi/rapidapi are, or
    turning on `driver: hybrid` for taobao/tmall would silently do nothing.
    """
    print("\na no-key preset (usfans) is not gated behind CN_PROVIDER_KEY")
    _with_key(None)
    base_url = os.environ.pop("CN_PROVIDER_BASE_URL", None)
    try:
        check_true(
            "usfans provider client builds with no key and no base_url override",
            _adapter(driver="hybrid", provider_preset="usfans").provider is not None,
        )
    finally:
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


def test_detail_provider_merges_variants_onto_the_search_stage_offer():
    """_detail_provider() merged specs/tiers from the enriched offer onto the
    search-stage one but never variants or price_max -- invisible until now
    because USFans is the first preset whose to_offer() actually populates
    RawOffer.variants (see providers.yaml's usfans `variants:` mapping).
    Confirmed for real: a live crawl enriched 30 offers through USFans and
    every one came back with variant_count=0 despite `detail()` mapping
    variants correctly on its own (per the tests above) -- the bug was
    specifically in this merge step, not in the mapping.
    """
    print("\n_detail_provider merges variants (and price_max) onto the search-stage offer")
    import sourcehub.agents as agents_module
    from sourcehub.scrapers.base import RawOffer

    detail_session = _FakeAgentSession(
        {"code": 200, "success": True, "data": {
            "goodsId": "native-id-1", "titleEn": "Test Jacket",
            "detailUrl": None, "price": 0, "convertedPrice": 0,
            "properties": [{"propId": "1", "propNameEn": "Color", "valuesList": [
                {"valueId": "101", "valueNameEn": "Black"},
                {"valueId": "102", "valueNameEn": "Navy"},
            ]}],
            "skuList": [
                {"skuId": "sku-1", "price": 29.9, "stock": 5, "valueIds": ["1:101"]},
                {"skuId": "sku-2", "price": 31.5, "stock": 0, "valueIds": ["1:102"]},
            ],
        }}
    )
    original = agents_module.agent_browser_session
    agents_module.agent_browser_session = lambda agent_key: detail_session
    try:
        _with_key("test-key")
        adapter = _adapter("taobao", driver="provider", provider_preset="usfans")
        # A shallow offer as produced by provider *search* discovery -- no
        # variants/specs yet, those only come from detail.
        native_url = "https://usfans.com/product/2/native-id-1"
        offer = RawOffer(
            site_key="taobao", site_product_id="native-id-1",
            url=native_url, title="Test Jacket", currency="CNY", price_min=None,
        )
        enriched = adapter.fetch_detail(offer)

        check_true("enriched via provider", enriched.detail_fetched)
        check("two variants merged onto the search-stage offer", len(enriched.variants), 2)
        check("variant attrs resolved", enriched.variants[0].attrs, {"Color": "Black"})
        check("price_min derived from variants merged onto the offer",
              enriched.price_min, 29.9)
        check("price_max also merged (not just price_min)", enriched.price_max, 31.5)
    finally:
        agents_module.agent_browser_session = original


# ------------------------------------------------------- via_agent_login routing


class _FakeAgentSession:
    """Stands in for BrowserSession: no real Playwright/browser in this test."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []
        self.started = False
        self.closed = False

    def start(self):
        self.started = True
        return self

    def close(self):
        self.closed = True

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def fetch_json(self, url, *, referer=None, headers=None, method="GET", body=None):
        self.calls.append({"url": url, "referer": referer, "headers": headers or {},
                            "method": method, "body": body})
        return self.payload


def test_via_agent_login_routes_through_a_browser_session():
    print("\nvia_agent_login")
    import sourcehub.agents as agents_module

    # agent_lookup's detail_path is ["data", "result", "item"] -- a list of
    # candidate *full* paths (first hit wins), not one dotted chain, so the node
    # the mapper reads must sit directly under one of those top-level keys.
    fake = _FakeAgentSession(
        {"item": {"itemId": "123456", "itemName": "Signed-in Only Gadget"}}
    )
    unused_fetcher = FakeFetcher({})
    original = agents_module.agent_browser_session
    agents_module.agent_browser_session = lambda agent_key: fake
    try:
        client = ProviderClient("agent_lookup", "taobao", unused_fetcher,
                                base_url="https://example.test")
        # Instance-only override -- never mutate the shared preset cache other
        # tests (and the real app) read from.
        client.preset = {**client.preset, "via_agent_login": "cssbuy"}

        offer = client.detail("123456", url="https://item.taobao.com/item.htm?id=123456")
        check_true("the plain Fetcher was never touched", not unused_fetcher.last)
        check("exactly one call went through the agent session", len(fake.calls), 1)
        check_true("browser session was started", fake.started)
        check_true("browser session was closed after the call", fake.closed)
        check_true("mapped using the signed-in-only response",
                  offer is not None and offer.title == "Signed-in Only Gadget")

        # POST also goes through the agent session (confirmed live: USFans'
        # own keyword-search endpoint is a POST with a JSON body) rather than
        # being refused or silently sent anonymously through the plain Fetcher.
        post_fake = _FakeAgentSession({"item": {"itemId": "77", "itemName": "Posted Gadget"}})
        agents_module.agent_browser_session = lambda agent_key: post_fake
        post_client = ProviderClient("agent_lookup", "taobao", FakeFetcher({}),
                                     base_url="https://example.test")
        post_client.preset = {
            **post_client.preset,
            "via_agent_login": "cssbuy",
            "detail": {**post_client.preset["detail"], "method": "POST",
                       "body": {"id": "{id}"}},
        }
        offer = post_client.detail("77", url="https://item.taobao.com/item.htm?id=77")
        check_true("POST through an agent session is mapped",
                  offer is not None and offer.title == "Posted Gadget")
        check("exactly one POST call went through the agent session", len(post_fake.calls), 1)
        check("POST call used the POST method", post_fake.calls[0]["method"], "POST")
        check("POST body was expanded and sent", post_fake.calls[0]["body"], {"id": "77"})
    finally:
        agents_module.agent_browser_session = original

    print("\nwithout via_agent_login, nothing changes -- plain Fetcher still used")
    fetcher = FakeFetcher({"item": {"itemName": "Anonymous Gadget"}})
    plain_client = ProviderClient("agent_lookup", "taobao", fetcher,
                                  base_url="https://example.test")
    plain_client.detail("1", url="https://item.taobao.com/item.htm?id=1")
    check_true("the plain fetcher recorded the call", bool(fetcher.last))


# --------------------------------------------------------- usfans (resolve step)


def test_usfans_resolve_then_detail():
    """The usfans preset shipped in providers.yaml, exercised against the exact
    response shapes captured from the live site (fabricated item id, so the
    shape is real but every value inside it is null -- see the preset's own
    comment). Two calls, two different transports: resolve is always anonymous
    (confirmed live even for taobao, which gates the detail call itself), so it
    must go through the plain Fetcher while detail goes through the agent
    session -- if resolve were accidentally routed through the browser too, or
    detail accidentally skipped it, this would catch either mistake.
    """
    print("\nusfans: resolve (anonymous, plain Fetcher) then detail (agent session)")
    import sourcehub.agents as agents_module

    resolve_fetcher = FakeFetcher(
        {"code": 200, "msg": "操作成功",
         "data": {"itemNo": "KP8y0DJFSf2MTPSNU1ufjiwJtV2oWkGfx7MeDdhp1qyGqHmMPQ",
                   "channelType": 2},
         "success": True}
    )
    detail_session = _FakeAgentSession(
        {"code": 200, "msg": "操作成功", "success": True,
         "data": {"goodsId": "KP8y0DJFSf2MTPSNU1ufjiwJtV2oWkGfx7MeDdhp1qyGqHmMPQ",
                   "title": "USB C Hub 8-in-1", "titleEn": "USB C Hub 8-in-1",
                   "convertedPrice": 15.5, "images": ["https://example.test/a.jpg"],
                   "detailUrl": "https://item.taobao.com/item.htm?id=700000000",
                   "shopName": "深圳前海店", "shopNameEn": "Shenzhen Qianhai Store"}}
    )
    original = agents_module.agent_browser_session
    agents_module.agent_browser_session = lambda agent_key: detail_session
    try:
        client = ProviderClient("usfans", "taobao", resolve_fetcher,
                                base_url="https://www.usfans.com")
        offer = client.detail(
            "700000000", url="https://item.taobao.com/item.htm?id=700000000"
        )
        check_true("resolve went through the plain Fetcher", bool(resolve_fetcher.last))
        check("resolve sent the raw url in its JSON body",
              resolve_fetcher.last["body"], {"url": "https://item.taobao.com/item.htm?id=700000000"})
        check("exactly one call went through the agent session", len(detail_session.calls), 1)
        check_true("the detail call used the *resolved* opaque id, not the raw item id",
                  "goodsId=KP8y0DJFSf2MTPSNU1ufjiwJtV2oWkGfx7MeDdhp1qyGqHmMPQ"
                  in detail_session.calls[0]["url"])
        check_true("the detail call carries the taobao provider_code (channel=2)",
                  "channel=2" in detail_session.calls[0]["url"])
        check_true("offer mapped", offer is not None)
        check("title prefers the English field", offer.title, "USB C Hub 8-in-1")
        check("price mapped from convertedPrice", offer.price_min, 15.5)
        check("seller prefers the English shop name", offer.seller_name, "Shenzhen Qianhai Store")
    finally:
        agents_module.agent_browser_session = original

    print("\nusfans: a resolve step that finds nothing is a clean miss, not a crash")
    empty_resolve = FakeFetcher({"code": 200, "data": {"itemNo": None}, "success": True})
    client2 = ProviderClient("usfans", "taobao", empty_resolve, base_url="https://www.usfans.com")
    result = client2.detail("1", url="https://item.taobao.com/item.htm?id=1")
    check("no resolved id -> no offer, not an exception", result, None)


def test_usfans_search_is_a_post_with_a_json_body():
    """usfans' keyword search, captured live from a real logged-in session
    (POST /api/goods/search/keyword, not the GET query-param shape every
    other search-capable preset uses). Confirmed live: USFans never exposes
    the real Taobao item id anywhere in its own API -- even here, with no
    external URL involved at all -- so `url` must come from item_url_template
    (USFans' own product page), never from a `url`/`detailUrl` field the
    response doesn't even have.
    """
    print("\nusfans: keyword search is a POST with a JSON body, via the agent session")
    import sourcehub.agents as agents_module

    # Trimmed from a real response to the two records that matter for this
    # test; the rest are identically shaped.
    search_session = _FakeAgentSession(
        {"code": 200, "msg": "操作成功", "success": True, "data": {
            "records": [
                {
                    "goodsId": "9Z_NVTvkbn66Zw48e9p0iC4_mMRlUWnjEkUjId0dZluR8ozIH6fTjSpT",
                    "title": "Suitable for Reading Device C30 Bluetooth Headset",
                    "image": "https://img.alicdn.com/imgextra/i4/2212704965261/O1CN01TP4f51.jpg",
                    "price": 78.00, "priceCurrency": 12.96, "monthSold": 0,
                    "channel": 2, "shopId": None, "inventory": 200,
                    "discountPrice": None, "discountPriceCurrency": None,
                    "goodsLabelType": 1,
                },
                {
                    "goodsId": "W4N3GSmOSzv8kz7NJdIucHT_sfFlhOBN1uQdBxMmElKqczRCyZxL3ugC",
                    "title": "Suitable for Oppoa1Pro Headphones Bluetooth",
                    "image": "https://img.alicdn.com/imgextra/i4/2212704965261/O1CN01TP4f52.jpg",
                    "price": 78.00, "priceCurrency": 12.96, "monthSold": 0,
                    "channel": 2, "shopId": None, "inventory": 200,
                    "discountPrice": None, "discountPriceCurrency": None,
                    "goodsLabelType": 1,
                },
            ],
            "total": "1000", "size": "20", "current": "1", "pages": "50",
        }}
    )
    original = agents_module.agent_browser_session
    agents_module.agent_browser_session = lambda agent_key: search_session
    try:
        client = ProviderClient("usfans", "taobao", FakeFetcher({}),
                                base_url="https://www.usfans.com")
        check_true("usfans reports search capability now that it's mapped", client.can_search)
        offers = list(client.search("bluetooth earbuds", page=1))

        check("exactly one call went through the agent session", len(search_session.calls), 1)
        check("search used POST", search_session.calls[0]["method"], "POST")
        check("body carries the taobao provider_code (channel=2)",
              search_session.calls[0]["body"]["channel"], "2")
        check("body carries the keyword", search_session.calls[0]["body"]["keyWord"],
              "bluetooth earbuds")
        check("body carries the page number", search_session.calls[0]["body"]["pageNum"], "1")

        check("both records mapped", len(offers), 2)
        check("title mapped straight from `title` (already English, no titleEn here)",
              offers[0].title, "Suitable for Reading Device C30 Bluetooth Headset")
        check("price mapped from the raw CNY `price`, not the converted one",
              offers[0].price_min, 78.00)
        check("image mapped from the singular `image` field", offers[0].image_urls,
              ["https://img.alicdn.com/imgextra/i4/2212704965261/O1CN01TP4f51.jpg"])
        check("url falls back to USFans' own product page, not a broken "
              "reconstruction from the opaque goodsId as a Taobao id",
              offers[0].url,
              "https://usfans.com/product/2/9Z_NVTvkbn66Zw48e9p0iC4_mMRlUWnjEkUjId0dZluR8ozIH6fTjSpT")
    finally:
        agents_module.agent_browser_session = original


def test_usfans_1688_search_uses_the_real_numeric_item_id():
    """Unlike taobao/tmall, confirmed live: USFans' 1688 goodsId IS the
    genuine numeric 1688 offer id, not an opaque token -- a real
    detail.1688.com URL built from one landed on the correct item in a
    browser. 1688's item_url_template therefore builds a real, cross-agent-
    compatible URL rather than USFans' own product page.
    """
    print("\nusfans: 1688 search results carry a real, reusable numeric item id")
    import sourcehub.agents as agents_module

    search_session = _FakeAgentSession(
        {"code": 200, "msg": "操作成功", "success": True, "data": {
            "records": [{
                "goodsId": "1041786306896",
                "title": "[Four Headphones] New Wireless Bluetooth Earphones, "
                         "Ear-Clip Style, Semi-In-Ear,",
                "image": "https://cbu01.alicdn.com/img/ibank/earbuds.jpg",
                "price": 28.5, "priceCurrency": 4.74, "monthSold": 0,
                "channel": 1, "shopId": None, "inventory": 200,
                "discountPrice": None, "discountPriceCurrency": None,
                "goodsLabelType": 1,
            }],
            "total": "20", "size": "20", "current": "1", "pages": "1",
        }}
    )
    original = agents_module.agent_browser_session
    agents_module.agent_browser_session = lambda agent_key: search_session
    try:
        client = ProviderClient("usfans", "1688", FakeFetcher({}),
                                base_url="https://www.usfans.com")
        offers = list(client.search("bluetooth earbuds", page=1))

        check("body carries 1688's provider_code (channel=1)",
              search_session.calls[0]["body"]["channel"], "1")
        check("one record mapped", len(offers), 1)
        check("url is a real, cross-agent-compatible detail.1688.com URL "
              "built from the genuine numeric id -- not a USFans-only link",
              offers[0].url, "https://detail.1688.com/offer/1041786306896.html")
    finally:
        agents_module.agent_browser_session = original


def test_usfans_search_origin_detail_skips_resolve():
    """Enrichment of a search-discovered item must NOT run `resolve` -- the
    item's goodsId is already USFans' own native id (confirmed live: it works
    directly against `detail`), and resolve exists to convert an *external*
    source-site id into that native one, which is meaningless to do twice.
    """
    print("\nusfans: skip_resolve bypasses resolve for a search-origin id")
    import sourcehub.agents as agents_module

    detail_session = _FakeAgentSession(
        {"code": 200, "success": True, "data": {
            "goodsId": "9Z_NVTvkbn66Zw48e9p0iC4_mMRlUWnjEkUjId0dZluR8ozIH6fTjSpT",
            "titleEn": "Reading Device Bluetooth Headset",
            "detailUrl": "https://item.taobao.com/item.htm?id=someOpaqueToken",
            "price": 78.0,
        }}
    )
    original = agents_module.agent_browser_session
    agents_module.agent_browser_session = lambda agent_key: detail_session
    try:
        resolve_fetcher = FakeFetcher({})  # would raise if resolve were ever attempted
        client = ProviderClient("usfans", "taobao", resolve_fetcher,
                                base_url="https://www.usfans.com")
        native_url = ("https://usfans.com/product/2/"
                      "9Z_NVTvkbn66Zw48e9p0iC4_mMRlUWnjEkUjId0dZluR8ozIH6fTjSpT")
        offer = client.detail(
            "9Z_NVTvkbn66Zw48e9p0iC4_mMRlUWnjEkUjId0dZluR8ozIH6fTjSpT",
            url=native_url, skip_resolve=True,
        )
        check_true("resolve was never called", not resolve_fetcher.last)
        check("exactly one call went through the agent session", len(detail_session.calls), 1)
        check_true("the detail call used the native id directly, unresolved",
                  "goodsId=9Z_NVTvkbn66Zw48e9p0iC4_mMRlUWnjEkUjId0dZluR8ozIH6fTjSpT"
                  in detail_session.calls[0]["url"])
        check_true("offer mapped", offer is not None)
        check("the caller's own (USFans product page) URL wins over the "
              "opaque-token detailUrl, even with resolve skipped",
              offer.url, native_url)
    finally:
        agents_module.agent_browser_session = original


def test_usfans_multi_sku_item_a_real_login_actually_returned():
    """Two bugs found running against a real, successfully authenticated
    USFans response (not a fabrication -- this is what a real multi-SKU
    Taobao listing actually sent back once login worked): detailUrl is null,
    and price/convertedPrice are both 0 because the real price is per-SKU.
    Both used to produce a genuinely broken offer (a URL built from an opaque
    id that goes nowhere, and a $0.00 "price" that would win every
    cheapest-price comparison in the catalog) rather than a merely incomplete
    one.
    """
    print("\nusfans: a real multi-SKU response with a null detailUrl and a "
          "placeholder 0 price")
    import sourcehub.agents as agents_module

    resolve_fetcher = FakeFetcher(
        {"code": 200, "data": {"itemNo": "qrEfh2Xg37nnf9l_3lpnTfyLteaF4R9wRsOcG2OdBfIe_jgcfp-VFGs"},
         "success": True}
    )
    # Trimmed to the fields that matter for this test; the real response also
    # carries categoryName/skuList/properties/etc., none of which change the
    # two things under test here.
    detail_session = _FakeAgentSession(
        {"code": 200, "msg": "操作成功", "success": True, "data": {
            "goodsId": "lU3jD8xO6rYGeF6VorxEuriL3o_Wd34GiWpllUwsjsol-xzPP8wTDP8",
            "title": "汽车后备箱收纳盒防水折叠可爱卡通收纳袋车内用品",
            "titleEn": "Car Trunk Storage Box Waterproof Foldable Cute Cartoon Storage Bag",
            "detailUrl": None, "price": 0, "convertedPrice": 0,
            "images": ["https://cbu01.alicdn.com/img/a.jpg"],
            "shopName": "某某店铺", "shopNameEn": None,
        }}
    )
    original = agents_module.agent_browser_session
    agents_module.agent_browser_session = lambda agent_key: detail_session
    try:
        client = ProviderClient("usfans", "taobao", resolve_fetcher,
                                base_url="https://www.usfans.com")
        real_url = "https://item.taobao.com/item.htm?id=1075605616467"
        offer = client.detail("1075605616467", url=real_url)
        check_true("offer still mapped despite the missing price", offer is not None)
        check("url falls back to the real one it was called with, "
              "not a reconstruction from the opaque goodsId", offer.url, real_url)
        check("a top-level 0 is treated as 'not disclosed', not a real price",
              offer.price_min, None)
        check("title still maps normally", offer.title, "Car Trunk Storage Box Waterproof "
              "Foldable Cute Cartoon Storage Bag")
    finally:
        agents_module.agent_browser_session = original


def test_usfans_detailurl_populated_but_wrong_is_not_trusted():
    """A second, worse variant of the same real bug: on a later real request,
    USFans' detailUrl was NOT null -- it was populated with a URL built from
    its own opaque per-request id substituted for the real Taobao one
    (https://item.taobao.com/item.htm?id=<the opaque token>). That passes
    "is it empty" fine, which is exactly why prefer_fallback_url exists for
    any resolve-based preset: emptiness isn't a reliable enough test that the
    field is trustworthy.
    """
    print("\nusfans: detailUrl is not merely sometimes empty -- it can be "
          "populated with a wrong URL, which 'is it empty' would miss")
    import sourcehub.agents as agents_module

    resolve_fetcher = FakeFetcher(
        {"code": 200, "data": {"itemNo": "AhpZxqim-k6PPakxjhfWgm6RLH8Xs9QvGt4hsKzSFFwFB-V89QLpeHs"},
         "success": True}
    )
    opaque_id = "0xv5eEC5nNOVT7zZ8CkSyTJFfMtC38gTe7AGUjqF6Xe-flVl9FPqhOQ"
    detail_session = _FakeAgentSession(
        {"code": 200, "success": True, "data": {
            "goodsId": "AhpZxqim-k6PPakxjhfWgm6RLH8Xs9QvGt4hsKzSFFwFB-V89QLpeHs",
            "titleEn": "Car Trunk Storage Box",
            # Syntactically a perfectly normal-looking Taobao item URL --
            # semantically wrong, since {id} is USFans' own opaque token.
            "detailUrl": f"https://item.taobao.com/item.htm?id={opaque_id}",
            "price": 12.5,
        }}
    )
    original = agents_module.agent_browser_session
    agents_module.agent_browser_session = lambda agent_key: detail_session
    try:
        client = ProviderClient("usfans", "taobao", resolve_fetcher,
                                base_url="https://www.usfans.com")
        real_url = "https://item.taobao.com/item.htm?id=1075605616467"
        offer = client.detail("1075605616467", url=real_url)
        check_true("offer mapped", offer is not None)
        check("the real caller-supplied URL wins over a populated-but-wrong API field",
              offer.url, real_url)
        check("a genuinely present price is not discarded by this fix", offer.price_min, 12.5)
    finally:
        agents_module.agent_browser_session = original


def test_usfans_multi_sku_variants_map_to_readable_attrs():
    """The full skuList -> RawVariant mapping, against a trimmed-but-real
    multi-SKU jacket listing (Color x Size) pasted from an actual logged-in
    USFans session. skuList entries only carry opaque "propId:valueId"
    references via valueIds -- the human-readable names live in the separate
    top-level `properties` array and have to be cross-referenced by id.
    """
    print("\nusfans: a real multi-SKU jacket (Color x Size) maps to readable variants")
    import sourcehub.agents as agents_module

    resolve_fetcher = FakeFetcher(
        {"code": 200, "data": {"itemNo": "opaque-jacket-token"}, "success": True}
    )
    detail_session = _FakeAgentSession(
        {"code": 200, "success": True, "data": {
            "goodsId": "opaque-jacket-token",
            "titleEn": "Men's Casual Jacket",
            "detailUrl": None,
            "price": 0, "convertedPrice": 0,
            "images": ["https://cbu01.alicdn.com/jacket.jpg"],
            "properties": [
                {"propId": "1", "propName": "颜色", "propNameEn": "Color",
                 "valuesList": [
                     {"valueId": "101", "valueName": "黑色", "valueNameEn": "Black"},
                     {"valueId": "102", "valueName": "藏青色", "valueNameEn": "Navy"},
                 ]},
                {"propId": "2", "propName": "尺码", "propNameEn": "Size",
                 "valuesList": [
                     {"valueId": "201", "valueName": "M", "valueNameEn": "M"},
                     {"valueId": "202", "valueName": "L", "valueNameEn": "L"},
                 ]},
            ],
            "skuList": [
                {"skuId": "sku-1", "price": 29.9, "stock": 12,
                 "imgUrl": "https://cbu01.alicdn.com/jacket-black.jpg",
                 "valueIds": ["1:101", "2:201"]},
                {"skuId": "sku-2", "price": 31.5, "stock": 0,
                 "imgUrl": "https://cbu01.alicdn.com/jacket-navy.jpg",
                 "valueIds": ["1:102", "2:202"]},
            ],
        }}
    )
    original = agents_module.agent_browser_session
    agents_module.agent_browser_session = lambda agent_key: detail_session
    try:
        client = ProviderClient("usfans", "taobao", resolve_fetcher,
                                base_url="https://www.usfans.com")
        real_url = "https://item.taobao.com/item.htm?id=1075605616467"
        offer = client.detail("1075605616467", url=real_url)
        check_true("offer mapped", offer is not None)
        check("two SKUs mapped to two variants", len(offer.variants), 2)

        v1 = next(v for v in offer.variants if v.sku == "sku-1")
        check("variant price mapped", v1.price, 29.9)
        check("variant stock mapped", v1.stock, 12)
        check("variant image mapped", v1.image_url, "https://cbu01.alicdn.com/jacket-black.jpg")
        check_true("in-stock variant flagged in_stock", v1.in_stock)
        check("valueIds resolved to readable English attrs via the properties lookup",
              v1.attrs, {"Color": "Black", "Size": "M"})

        v2 = next(v for v in offer.variants if v.sku == "sku-2")
        check_true("zero-stock variant flagged out of stock", not v2.in_stock)
        check("second variant's attrs also resolved", v2.attrs, {"Color": "Navy", "Size": "L"})

        check("offer.price_min derived from variant prices, not the unset top-level 0",
              offer.price_min, 29.9)
        check("offer.price_max derived from variant prices", offer.price_max, 31.5)
    finally:
        agents_module.agent_browser_session = original


def main() -> int:
    for fn in (test_dig, test_otapi_mapping, test_rapidapi_mapping, test_url_fallback,
               test_capabilities, test_probe, test_probe_on_a_detail_only_preset,
               test_driver_resolution, test_no_key_preset_still_activates_the_provider,
               test_hybrid_detail_flow, test_detail_provider_merges_variants_onto_the_search_stage_offer,
               test_via_agent_login_routes_through_a_browser_session,
               test_usfans_resolve_then_detail, test_usfans_search_is_a_post_with_a_json_body,
               test_usfans_1688_search_uses_the_real_numeric_item_id,
               test_usfans_search_origin_detail_skips_resolve,
               test_usfans_multi_sku_item_a_real_login_actually_returned,
               test_usfans_detailurl_populated_but_wrong_is_not_trusted,
               test_usfans_multi_sku_variants_map_to_readable_attrs):
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
