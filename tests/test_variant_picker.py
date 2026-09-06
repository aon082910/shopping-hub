"""The product page's interactive Color/Size picker: the API's view model.

Interactive behaviour (clicking a swatch, watching price/stock/thumbnail update)
lives in JS and isn't something this offline suite can exercise. What it can and
must check is the contract the JS depends on: offer_view() has to actually put
`attrs` and `image_url` on each variant (the raw OfferVariant model has both; it
was easy to forget one when first wiring the API view), and the page has to pick
the interactive picker over the old flat list exactly when a variant carries real
options -- not always, not never.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_vpicker_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from fastapi.testclient import TestClient  # noqa: E402

from sourcehub.api.main import app, offer_view  # noqa: E402
from sourcehub.db.models import CanonicalProduct, Offer  # noqa: E402
from sourcehub.db.session import init_db, session_scope  # noqa: E402
from sourcehub.pipeline.ingest import IngestContext, ingest_offer  # noqa: E402
from sourcehub.scrapers.base import RawOffer, RawVariant  # noqa: E402

FAILS: list[str] = []


def check(label, got, want=True) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


WITH_OPTIONS = RawOffer(
    site_key="aliexpress", site_product_id="vp-with-options",
    url="https://www.aliexpress.com/item/9001.html",
    title="RGB LED Strip Light 5V USB Powered", currency="USD", price_min=2.95, moq=1,
    detail_fetched=True,
    variants=[
        RawVariant(sku="380--1", name="Length: 50FT", price=2.01, currency="USD",
                   attrs={"Length": "50FT"}, in_stock=False),
        RawVariant(sku="380--3", name="Length: 65.6FT", price=2.95, currency="USD",
                   attrs={"Length": "65.6FT"}, in_stock=True, image_url="https://cdn.example.test/65ft.jpg"),
        RawVariant(sku="380--4", name="Length: 100FT", price=3.35, currency="USD",
                   attrs={"Length": "100FT"}, in_stock=True),
    ],
)

FLAT_ONLY = RawOffer(
    site_key="dhgate", site_product_id="vp-flat-only",
    url="https://www.dhgate.com/product/flat/9002.html",
    title="USB Hub 4-Port and 8-Port", currency="USD", price_min=8.0, moq=1,
    detail_fetched=True,
    variants=[
        RawVariant(sku="4-port", name="4-Port", price=8.0, currency="USD"),
        RawVariant(sku="8-port", name="8-Port", price=14.0, currency="USD"),
    ],
)


def _ingest(raw: RawOffer) -> tuple[int, int]:
    with session_scope() as session:
        ctx = IngestContext(session)
        offer = ingest_offer(ctx, raw)
        return offer.id, offer.canonical_id


def main() -> None:
    init_db()  # seeds the real site rows, aliexpress/dhgate included

    offer_id, product_id = _ingest(WITH_OPTIONS)
    flat_id, flat_product_id = _ingest(FLAT_ONLY)

    print("offer_view() exposes what the picker's JS needs")
    with session_scope() as session:
        offer = session.get(Offer, offer_id)
        view = offer_view(session, offer)
        check("three variants surfaced", len(view["variants"]), 3)
        check("variants_have_options is true", view["variants_have_options"], True)
        by_sku = {v["sku"]: v for v in view["variants"]}
        check("attrs pass through", by_sku["380--3"]["attrs"], {"Length": "65.6FT"})
        check("image_url passes through", by_sku["380--3"]["image_url"],
              "https://cdn.example.test/65ft.jpg")
        check("variant without an image is None, not missing", by_sku["380--4"]["image_url"], None)
        check("sold-out variant keeps in_stock False", by_sku["380--1"]["in_stock"], False)

    print("\na variant with no attrs falls back to the flat list, not the picker")
    with session_scope() as session:
        flat_offer = session.get(Offer, flat_id)
        flat_view = offer_view(session, flat_offer)
        check("no attrs on either variant", flat_view["variants_have_options"], False)

    print("\nthe rendered page picks the right widget for each case")
    client = TestClient(app)
    with session_scope() as session:
        product = session.get(CanonicalProduct, product_id)
        slug = product.slug
        flat_product = session.get(CanonicalProduct, flat_product_id)
        flat_slug = flat_product.slug

    # The JS block itself references the ".variant-picker" class name on every
    # product page regardless of this offer, so the real signal is the rendered
    # element -- its opening tag -- not the bare substring.
    picker_tag = 'class="variant-picker"'

    r = client.get(f"/product/{slug}")
    check("product page renders", r.status_code, 200)
    check("interactive picker markup present", picker_tag in r.text)
    check("all three lengths appear in the embedded JSON", all(
        s in r.text for s in ("50FT", "65.6FT", "100FT")
    ))

    r2 = client.get(f"/product/{flat_slug}")
    check("flat-list page renders", r2.status_code, 200)
    check("no interactive picker for a variant with no real attrs",
          picker_tag not in r2.text)
    check("falls back to the old options disclosure", "options</summary>" in r2.text)

    print("\n" + "=" * 60)
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(" -", f)
        sys.exit(1)
    print("variant picker OK")


if __name__ == "__main__":
    main()
