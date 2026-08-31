"""Per-SKU variants.

The gap this closes: a listing is not one price. A "USB Hub" listing sells a 4-port
at $9 and an 8-port at $24 under one title and one photo. Storing a single number
made the catalog wrong in both directions -- the headline looked too cheap, and the
SKU you actually wanted was invisible.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_var_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "claude"
os.environ["ANTHROPIC_API_KEY"] = "unused"

from sqlalchemy import func, select  # noqa: E402

from sourcehub.db.models import Offer, OfferVariant  # noqa: E402
from sourcehub.db.session import init_db, session_scope  # noqa: E402
from sourcehub.pipeline import translate as _tr  # noqa: E402
from sourcehub.pipeline.ingest import IngestContext, ingest_offer  # noqa: E402
from sourcehub.scrapers.base import RawOffer, RawVariant  # noqa: E402

PHRASEBOOK = {"黑色 128GB": "Black 128GB", "白色 256GB": "White 256GB"}
_tr.Translator._call_provider = lambda self, texts, src: [
    PHRASEBOOK.get(t, t) for t in texts
]

FAILS: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


def check_true(label, got):
    check(label, bool(got), True)


class NoImages:
    def download(self, url, referer=None):
        raise RuntimeError("no images")

    def close(self):
        pass


HUB = RawOffer(
    site_key="banggood", site_product_id="var-hub",
    url="https://www.banggood.com/hub-p-77.html",
    title="USB C Hub Docking Station", currency="USD",
    price_min=99.00,          # a misleading headline; variants must override it
    moq=1, detail_fetched=True,
    variants=[
        RawVariant(sku="HUB-4", name="4 Port", price=9.00),
        RawVariant(sku="HUB-8", name="8 Port", price=24.00),
        RawVariant(sku="HUB-11", name="11 Port", price=39.00, in_stock=False),
    ],
)

CN = RawOffer(
    site_key="1688", site_product_id="var-cn",
    url="https://detail.1688.com/offer/88.html",
    title="Storage Drive", currency="CNY", price_min=200.0, moq=1,
    detail_fetched=True,
    variants=[
        RawVariant(sku="S-128", name="黑色 128GB", price=140.0),
        RawVariant(sku="S-256", name="白色 256GB", price=210.0),
    ],
)


def run() -> int:
    init_db()
    with session_scope() as s:
        ctx = IngestContext(s)
        ctx.images._fetcher = NoImages()
        ingest_offer(ctx, HUB, fetch_images=False)
        ingest_offer(ctx, CN, fetch_images=False)

    print()
    print("variants stored")
    with session_scope() as s:
        check("all SKUs persisted", s.scalar(select(func.count(OfferVariant.id))), 5)
        hub = s.scalar(select(Offer).where(Offer.site_product_id == "var-hub"))
        check("variant_count denormalized", hub.variant_count, 3)
        skus = sorted(v.sku for v in hub.variants)
        check("SKUs intact", skus, ["HUB-11", "HUB-4", "HUB-8"])

    print()
    print("headline price comes from the cheapest in-stock SKU")
    with session_scope() as s:
        hub = s.scalar(select(Offer).where(Offer.site_product_id == "var-hub"))
        # The listing-level 99.00 must be overridden by the real SKU prices, and the
        # out-of-stock 11-port must not set the ceiling.
        check("min price is the 4-port", hub.price_min, 9.00)
        check("max price is the dearest in-stock SKU", hub.price_max, 24.00)
        check("usd conversion follows", hub.price_usd, 9.00)
        check("landed cost follows", hub.landed_cost_usd, 9.00)

    print()
    print("currency conversion per variant")
    with session_scope() as s:
        cn = s.scalar(select(Offer).where(Offer.site_product_id == "var-cn"))
        check("min from variants", cn.price_min, 140.0)
        check("max from variants", cn.price_max, 210.0)
        cheap = min(cn.variants, key=lambda v: v.price)
        check_true("variant converted to USD",
                   cheap.price_usd and 15 < cheap.price_usd < 25)
        check("variant currency recorded", cheap.currency, "CNY")

    print()
    print("variant names are translated")
    with session_scope() as s:
        cn = s.scalar(select(Offer).where(Offer.site_product_id == "var-cn"))
        names = sorted(v.name_en for v in cn.variants)
        check("translated", names, ["Black 128GB", "White 256GB"])
        raws = sorted(v.name_raw for v in cn.variants)
        check("raw preserved", raws, ["白色 256GB", "黑色 128GB"])

    print()
    print("re-ingest replaces rather than accumulates")
    HUB.variants = [RawVariant(sku="HUB-4", name="4 Port", price=8.00)]
    with session_scope() as s:
        ctx = IngestContext(s)
        ctx.images._fetcher = NoImages()
        ingest_offer(ctx, HUB, fetch_images=False)
    with session_scope() as s:
        hub = s.scalar(select(Offer).where(Offer.site_product_id == "var-hub"))
        check("old SKUs removed", hub.variant_count, 1)
        check("total rows dropped too",
              s.scalar(select(func.count(OfferVariant.id))), 3)
        check("price follows the surviving SKU", hub.price_min, 8.00)
        check("single SKU clears the range", hub.price_max, None)

    print()
    print("a listing with no variants is unaffected")
    plain = RawOffer(site_key="dhgate", site_product_id="var-none",
                     url="https://www.dhgate.com/p/1.html", title="Plain Item",
                     currency="USD", price_min=5.0, moq=1, detail_fetched=True)
    with session_scope() as s:
        ctx = IngestContext(s)
        ctx.images._fetcher = NoImages()
        offer = ingest_offer(ctx, plain, fetch_images=False)
        check("price untouched", offer.price_min, 5.0)
        check("no variants", offer.variant_count, 0)

    print()
    print("=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("variants OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
