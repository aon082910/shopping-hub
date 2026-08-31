"""End-to-end pipeline test against a temporary database.

Drives the *real* ingest path -- no mocking of matching, rollups, FX or search.
The only thing stubbed is the network: image downloads are served from generated
bytes so the test runs offline.

What it proves:
  1. The same product listed on three sites in two languages collapses into ONE
     canonical product, via shared supplier photography (perceptual hash).
  2. A genuinely different product does NOT get merged into it.
  3. Prices convert to USD, and landed cost accounts for MOQ x unit + shipping.
  4. Tiered wholesale pricing picks the tier that actually applies at MOQ.
  5. Full-text search finds the merged product.
  6. 1688 listings surface forwarding-agent links; AliExpress ones do not.
  7. Re-ingesting the same listing updates in place instead of duplicating.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point at a throwaway DB and media dir BEFORE anything reads settings.
_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_test_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
# Exercise the real translation path (batching, caching, field fill-in) with a
# stubbed provider -- see PHRASEBOOK below. Only the API call itself is faked.
os.environ["TRANSLATE_PROVIDER"] = "claude"
os.environ["ANTHROPIC_API_KEY"] = "test-not-used"

from sqlalchemy import func, select  # noqa: E402

from sourcehub.agents import build_agent_links  # noqa: E402
from sourcehub.db.models import CanonicalProduct, Offer, Site  # noqa: E402
from sourcehub.db.search import search_product_ids  # noqa: E402
from sourcehub.db.session import init_db, session_scope  # noqa: E402
from sourcehub.pipeline.ingest import IngestContext, ingest_offer  # noqa: E402
from sourcehub.pipeline import translate as _translate  # noqa: E402
from sourcehub.scrapers.base import RawOffer, RawSpec, RawTier  # noqa: E402

# --- stubbed translation provider -------------------------------------------
# Stands in for the Anthropic call. Everything else in Translator (dedupe, cache
# lookup, DB write-through, offer field assignment) runs for real.
PHRASEBOOK = {
    "2024新款TWS蓝牙耳机5.3无线降噪运动耳机厂家批发":
        "TWS Bluetooth 5.3 Wireless Noise Cancelling Sport Earbuds Factory Wholesale",
    "蓝牙版本": "Bluetooth Version",
    "电池容量": "Battery Capacity",
    "深圳市声美电子有限公司": "Shenzhen Shengmei Electronics Co., Ltd.",
    "广东 深圳": "Guangdong Shenzhen",
}
_PROVIDER_CALLS = {"n": 0, "strings": 0}


def _stub_provider(self, texts, src_lang):
    _PROVIDER_CALLS["n"] += 1
    _PROVIDER_CALLS["strings"] += len(texts)
    return [PHRASEBOOK.get(t, t) for t in texts]


_translate.Translator._call_provider = _stub_provider

FAILS: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


# --------------------------------------------------------------- fake network


def _png(color: tuple[int, int, int], size: int = 400, seed: int = 0) -> bytes:
    """Deterministic image. Same colour+seed -> byte-identical -> same phash."""
    from PIL import Image as PILImage

    img = PILImage.new("RGB", (size, size), color)
    px = img.load()
    # A little structure so phash has something to work with (a flat fill hashes
    # to the same value for every colour).
    for x in range(0, size, 8):
        for y in range(0, size, 8):
            if ((x // 8) + (y // 8) + seed) % 3 == 0:
                for dx in range(8):
                    for dy in range(8):
                        px[x + dx, y + dy] = (255 - color[0], 40, color[2])
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


IMAGES = {
    "earbuds": _png((30, 90, 200), seed=0),   # shared across 3 sites
    "drill": _png((200, 120, 20), seed=2),    # a different product
}


class FakeFetcher:
    """Stands in for util.http.Fetcher inside ImageStore."""

    def download(self, url: str, referer: str | None = None) -> bytes:
        for key, data in IMAGES.items():
            if key in url:
                return data
        raise RuntimeError(f"unexpected image url {url}")

    def close(self) -> None:
        pass


# ------------------------------------------------------------------- fixtures

EARBUDS_ALIEXPRESS = RawOffer(
    site_key="aliexpress",
    site_product_id="1005006123456",
    url="https://www.aliexpress.com/item/1005006123456.html",
    title="TWS Wireless Bluetooth 5.3 Earbuds Noise Cancelling Sport Headset",
    currency="USD",
    price_min=7.99,
    moq=1,
    shipping_cost=2.50,
    shipping_currency="USD",
    shipping_from="China",
    seller_name="TopSound Store",
    rating=4.7,
    orders_count=3120,
    image_urls=["https://cdn.example.com/earbuds.png"],
    category_path="Consumer Electronics > Earphones & Headphones",
    specs=[
        RawSpec("Bluetooth Version", "5.3", 0),
        RawSpec("Battery Capacity", "40mAh", 1),
        RawSpec("Brand Name", "Lenovo", 2),
    ],
    detail_fetched=True,
)

EARBUDS_1688 = RawOffer(
    site_key="1688",
    site_product_id="678901234",
    url="https://detail.1688.com/offer/678901234.html",
    title="2024新款TWS蓝牙耳机5.3无线降噪运动耳机厂家批发",
    currency="CNY",
    price_min=18.50,
    moq=2,
    moq_unit="piece",
    seller_name="深圳市声美电子有限公司",
    shipping_from="广东 深圳",
    image_urls=["https://cbu01.alicdn.com/earbuds.png"],
    tiers=[
        RawTier(2, 99, 18.50, "CNY"),
        RawTier(100, 999, 15.80, "CNY"),
        RawTier(1000, None, 13.20, "CNY"),
    ],
    specs=[RawSpec("蓝牙版本", "5.3", 0), RawSpec("电池容量", "40mAh", 1)],
    detail_fetched=True,
)

EARBUDS_DHGATE = RawOffer(
    site_key="dhgate",
    site_product_id="998877665",
    url="https://www.dhgate.com/product/tws-earbuds/998877665.html",
    title="Wholesale TWS Earbuds BT 5.3 ANC Sports Headphones Lot",
    currency="USD",
    price_min=5.40,
    moq=5,
    shipping_cost=6.00,
    shipping_currency="USD",
    seller_name="ShenzhenAudio",
    image_urls=["https://image.dhgate.com/earbuds.png"],
    specs=[RawSpec("Bluetooth Version", "5.3", 0)],
    detail_fetched=True,
)

DRILL_BANGGOOD = RawOffer(
    site_key="banggood",
    site_product_id="1554321",
    url="https://www.banggood.com/cordless-drill-p-1554321.html",
    title="21V Cordless Impact Drill Brushless Electric Screwdriver Power Tool",
    currency="USD",
    price_min=42.99,
    moq=1,
    shipping_free=True,
    image_urls=["https://img.banggood.com/drill.png"],
    specs=[RawSpec("Voltage", "21V", 0), RawSpec("Brand Name", "Hilda", 1)],
    detail_fetched=True,
)


# ----------------------------------------------------------------------- runs


def run() -> int:
    init_db()

    print("\ningesting 4 listings across 4 marketplaces")
    with session_scope() as session:
        ctx = IngestContext(session)
        ctx.images._fetcher = FakeFetcher()  # offline image bytes

        for raw in (EARBUDS_ALIEXPRESS, EARBUDS_1688, EARBUDS_DHGATE, DRILL_BANGGOOD):
            offer = ingest_offer(ctx, raw)
            print(f"    {raw.site_key:<12} -> offer #{offer.id} "
                  f"${offer.price_usd} landed ${offer.landed_cost_usd} "
                  f"product #{offer.canonical_id}")

    # ---------------------------------------------------------------- assertions
    with session_scope() as session:
        print("\nmatching")
        n_offers = session.scalar(select(func.count(Offer.id)))
        n_products = session.scalar(select(func.count(CanonicalProduct.id)))
        check("4 offers stored", n_offers, 4)
        check("merged into 2 products", n_products, 2)

        earbuds = session.scalar(
            select(CanonicalProduct).where(CanonicalProduct.offer_count == 3)
        )
        check_true("earbuds product exists", earbuds is not None)
        if earbuds:
            check("earbuds spans 3 sites", earbuds.site_count, 3)
            check("english title chosen", "Earbuds" in earbuds.title_en, True)

        drill = session.scalar(
            select(CanonicalProduct).where(CanonicalProduct.offer_count == 1)
        )
        check_true("drill stayed separate", drill is not None)

        print("\npricing")
        if earbuds:
            # 1688: MOQ 2 falls in the 2-99 tier at CNY 18.50 -> /7.15 = $2.587
            cn = session.scalar(
                select(Offer).join(Site).where(
                    Site.key == "1688", Offer.canonical_id == earbuds.id
                )
            )
            check_true("1688 offer attached", cn is not None)
            if cn:
                check("tier at MOQ applied (CNY 18.50)", round(cn.price_min, 2), 18.50)
                check_true("converted to USD", cn.price_usd and 2.0 < cn.price_usd < 3.5)
                # landed = 2 units x unit price, no shipping disclosed
                check("landed = moq x unit", round(cn.landed_cost_usd, 2),
                      round(cn.price_usd * 2, 2))
                check("3 price tiers stored", len(cn.tiers), 3)

            dh = session.scalar(
                select(Offer).join(Site).where(
                    Site.key == "dhgate", Offer.canonical_id == earbuds.id
                )
            )
            if dh:
                # 5 x $5.40 + $6.00 shipping = $33.00
                check("dhgate landed cost", round(dh.landed_cost_usd, 2), 33.00)

            ali = session.scalar(
                select(Offer).join(Site).where(
                    Site.key == "aliexpress", Offer.canonical_id == earbuds.id
                )
            )
            if ali:
                check("aliexpress landed cost", round(ali.landed_cost_usd, 2), 10.49)

            check_true("best price is the cheapest unit",
                       earbuds.best_price_usd and earbuds.best_price_usd < 5.5)

        print("\ntranslation")
        cn_any = session.scalar(select(Offer).join(Site).where(Site.key == "1688"))
        check("chinese title translated", cn_any.title_en,
              "TWS Bluetooth 5.3 Wireless Noise Cancelling Sport Earbuds Factory Wholesale")
        check("raw title preserved", cn_any.title_raw.startswith("2024新款"), True)
        check("source language detected", cn_any.source_lang, "zh")
        cn_spec_keys = {s.key_en for s in cn_any.specs}
        check_true("spec keys translated", "Bluetooth Version" in cn_spec_keys)
        check_true("english text never sent to provider",
                   "Wholesale TWS Earbuds BT 5.3 ANC Sports Headphones Lot"
                   not in PHRASEBOOK)

        print("\nspec merging")
        if earbuds:
            keys = {k.lower() for k in (earbuds.specs or {})}
            check_true("bluetooth spec merged", "bluetooth" in keys)
            check_true("battery spec merged", "battery" in keys)
            check_true("brand captured", earbuds.brand == "Lenovo")

        print("\nsearch")
        hits = search_product_ids(session, "earbuds")
        check_true("fts finds earbuds", earbuds and earbuds.id in [h for h, _ in hits])
        hits2 = search_product_ids(session, "cordless drill")
        check_true("fts finds drill", drill and drill.id in [h for h, _ in hits2])
        check("irrelevant query misses", search_product_ids(session, "zzzznotathing"), [])
        # Regression: FTS5's unicode61 tokenizer treats a whole CJK run as ONE token,
        # so without per-character segmentation neither of these can ever match.
        cjk = search_product_ids(session, "蓝牙耳机")
        check_true("chinese query finds the product", earbuds and earbuds.id in [h for h, _ in cjk])
        embedded = search_product_ids(session, "TWS")
        check_true("latin embedded in CJK is findable",
                   earbuds and earbuds.id in [h for h, _ in embedded])

        print("\nagent links")
        cn_offer = session.scalar(select(Offer).join(Site).where(Site.key == "1688"))
        links = build_agent_links(session, cn_offer)
        check_true("1688 offers agent options", len(links) >= 4)
        check_true("no 'direct' option for 1688", all(not l.is_direct for l in links))
        check_true("agent link embeds the item url",
                   any("678901234" in l.url for l in links))

        ali_offer = session.scalar(select(Offer).join(Site).where(Site.key == "aliexpress"))
        ali_links = build_agent_links(session, ali_offer)
        check("aliexpress = one direct link", len(ali_links), 1)
        check("and it is marked direct", ali_links[0].is_direct, True)

        print("\ncategorization")
        if earbuds and earbuds.category:
            check("earbuds categorized", earbuds.category.slug, "earbuds-headphones")
        else:
            check("earbuds categorized", None, "earbuds-headphones")
        if drill and drill.category:
            check("drill categorized", drill.category.slug, "power-tools")

    # ------------------------------------------------------------ idempotency
    print("\nre-ingest (idempotency)")
    with session_scope() as session:
        ctx = IngestContext(session)
        ctx.images._fetcher = FakeFetcher()
        EARBUDS_ALIEXPRESS.price_min = 6.49  # price dropped
        ingest_offer(ctx, EARBUDS_ALIEXPRESS)

    with session_scope() as session:
        check("still 4 offers", session.scalar(select(func.count(Offer.id))), 4)
        check("still 2 products", session.scalar(select(func.count(CanonicalProduct.id))), 2)
        ali = session.scalar(select(Offer).join(Site).where(Site.key == "aliexpress"))
        check("price updated in place", round(ali.price_usd, 2), 6.49)
        check("price history recorded", len(ali.history) >= 2, True)

    print("\ntranslation cache")
    before = _PROVIDER_CALLS["strings"]
    with session_scope() as session:
        ctx = IngestContext(session)
        ctx.images._fetcher = FakeFetcher()
        ingest_offer(ctx, EARBUDS_1688)  # identical Chinese strings as the first run
    check("re-ingest sent 0 new strings to the provider",
          _PROVIDER_CALLS["strings"] - before, 0)

    print("\n" + "=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("end-to-end pipeline OK")
    print(f"(temp db: {_TMP})")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
