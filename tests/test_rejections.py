"""Human match rulings must be durable.

The defect this covers: rejecting a proposed merge only set a status string that
nothing ever read, so the next `rematch` re-proposed the identical merge. Review
effort accumulated nowhere.

Scenario throughout: two listings that the matcher genuinely wants to merge (same
supplier photo, near-identical titles) but which a human declares different -- the
realistic case being two capacity/colour variants sharing one product photo.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_rej_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sqlalchemy import func, select  # noqa: E402

from sourcehub.db.models import CanonicalProduct, MatchRejection, Offer  # noqa: E402
from sourcehub.db.session import init_db, session_scope  # noqa: E402
from sourcehub.pipeline.ingest import IngestContext, ingest_offer  # noqa: E402
from sourcehub.pipeline.matching import (  # noqa: E402
    MatchEngine,
    clear_rejections,
    detach_offer,
    record_rejection,
)
from sourcehub.scrapers.base import RawOffer, RawSpec  # noqa: E402

FAILS: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


def check_true(label, got):
    check(label, bool(got), True)


def _stripes(size: int = 320) -> bytes:
    from PIL import Image as PILImage, ImageDraw

    img = PILImage.new("RGB", (size, size), (40, 90, 190))
    d = ImageDraw.Draw(img)
    for i in range(0, size, 40):
        d.rectangle([i, 0, i + 20, size], fill=(230, 230, 240))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _rings(size: int = 320) -> bytes:
    from PIL import Image as PILImage, ImageDraw

    img = PILImage.new("RGB", (size, size), (235, 235, 225))
    d = ImageDraw.Draw(img)
    for i in range(6):
        pad = 18 + i * 24
        d.ellipse([pad, pad, size - pad, size - pad],
                  outline=(20, 20, 30) if i % 2 == 0 else (200, 60, 40), width=10)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# Two visually unrelated photos. Each product pair shares one of them; the pairs must
# NOT share with each other, or every listing collapses into a single product and the
# test proves nothing. Distance between them is asserted at startup.
PHOTOS = {"pb.png": _stripes(), "rugged.png": _rings()}


class FakeFetcher:
    """Serves a different photo per URL, unlike a single global blob."""

    def download(self, url, referer=None):
        for name, data in PHOTOS.items():
            if name in url:
                return data
        raise RuntimeError(f"no fixture photo for {url}")

    def close(self):
        pass


def assert_fixture_photos_differ() -> None:
    """Guard the fixture itself.

    If these two images ever land within the matcher's phash threshold, every
    assertion below starts failing for reasons that have nothing to do with
    rejections. Better to say so directly.
    """
    import io as _io

    from PIL import Image as PILImage

    from sourcehub.pipeline.images import compute_phash, hamming

    hashes = [compute_phash(PILImage.open(_io.BytesIO(b))) for b in PHOTOS.values()]
    distance = hamming(*hashes)
    check_true(f"fixture photos are distinguishable (phash distance {distance})",
               distance is not None and distance > 10)


# Two listings the matcher genuinely wants to merge: one supplier photograph reused
# by a reseller, near-identical titles, no conflicting numbers. A human knows the
# second is a different item (the photo was simply reused), and says so.
CHARGER_A = RawOffer(
    site_key="aliexpress", site_product_id="pb-aaa",
    url="https://www.aliexpress.com/item/1001.html",
    title="Slim Power Bank USB C PD Fast Charging Portable Charger Black",
    currency="USD", price_min=12.99, moq=1,
    image_urls=["https://cdn.example.com/pb.png"],
    specs=[RawSpec("Color", "Black", 0)],
    detail_fetched=True,
)

CHARGER_B = RawOffer(
    site_key="dhgate", site_product_id="pb-bbb",
    url="https://www.dhgate.com/product/pb/2002.html",
    title="Portable Power Bank USB C PD Fast Charge Charger Slim Black",
    currency="USD", price_min=18.50, moq=2,
    image_urls=["https://image.dhgate.com/pb.png"],
    specs=[RawSpec("Color", "Black", 0)],
    detail_fetched=True,
)

# Same photo, titles differing only by capacity. The matcher should keep these apart
# on its own -- differing model codes ("BANK10000MAH" vs "BANK20000MAH") are a
# conflict signal -- so no human ruling should ever be needed here.
POWERBANK_10K = RawOffer(
    site_key="aliexpress", site_product_id="pb-10000",
    url="https://www.aliexpress.com/item/3001.html",
    title="Rugged Power Bank 10000mAh Solar Charger Waterproof",
    currency="USD", price_min=12.99, moq=1,
    image_urls=["https://cdn.example.com/rugged.png"],
    specs=[RawSpec("Battery Capacity", "10000mAh", 0)],
    detail_fetched=True,
)

POWERBANK_20K = RawOffer(
    site_key="dhgate", site_product_id="pb-20000",
    url="https://www.dhgate.com/product/pb/3002.html",
    title="Rugged Power Bank 20000mAh Solar Charger Waterproof",
    currency="USD", price_min=18.50, moq=2,
    image_urls=["https://image.dhgate.com/rugged.png"],
    specs=[RawSpec("Battery Capacity", "20000mAh", 0)],
    detail_fetched=True,
)


def _ingest(raw):
    with session_scope() as s:
        ctx = IngestContext(s)
        ctx.images._fetcher = FakeFetcher()
        offer = ingest_offer(ctx, raw)
        return offer.id, offer.canonical_id


def _rematch_all():
    """Re-run the matcher over every offer sitting on a single-offer product,
    exactly as `python -m sourcehub.cli rematch` does."""
    from sourcehub.db.models import OfferSpec
    from sourcehub.db.search import index_product
    from sourcehub.pipeline.matching import rebuild_product

    merged = 0
    with session_scope() as s:
        engine = MatchEngine(s)
        singles = s.scalars(
            select(Offer)
            .join(CanonicalProduct, CanonicalProduct.id == Offer.canonical_id)
            .where(CanonicalProduct.offer_count <= 1, Offer.is_active.is_(True))
        ).all()
        for offer in singles:
            old_id = offer.canonical_id
            specs = s.scalars(select(OfferSpec).where(OfferSpec.offer_id == offer.id)).all()
            result = engine.match(offer, specs)
            if result.matched and result.product.id != old_id:
                offer.canonical_id = result.product.id
                s.flush()
                rebuild_product(s, result.product)
                index_product(s, result.product)
                old = s.get(CanonicalProduct, old_id) if old_id else None
                if old is not None:
                    rebuild_product(s, old)
                merged += 1
    return merged


def run() -> int:
    init_db()

    print("\nfixture sanity")
    assert_fixture_photos_differ()

    print("\nsetup: matcher merges two listings sharing a supplier photo")
    id_a, prod_a = _ingest(CHARGER_A)
    id_b, prod_b = _ingest(CHARGER_B)
    print(f"    listing A -> offer {id_a} product {prod_a}")
    print(f"    listing B -> offer {id_b} product {prod_b}")
    # The matcher must *want* these together -- otherwise the rejection below would
    # prove nothing at all.
    check("matcher merged them", prod_a, prod_b)

    print("\nhuman rejects the merge")
    with session_scope() as s:
        offer_b = s.get(Offer, id_b)
        target_id = offer_b.canonical_id      # the shared product, still holding A
        detach_offer(s, offer_b)
        added = record_rejection(s, id_b, target_id, note="photo reused, different item")
        check_true("rejection pairs recorded", added >= 1)

    with session_scope() as s:
        check("now two separate products",
              s.scalar(select(func.count(CanonicalProduct.id))
                       .where(CanonicalProduct.offer_count > 0)), 2)
        a, b = s.get(Offer, id_a), s.get(Offer, id_b)
        check("offers are on different products", a.canonical_id != b.canonical_id, True)

        engine = MatchEngine(s)
        check("blocklist resolves to the other product",
              a.canonical_id in engine.blocked_canonical_ids(b), True)
        result = engine.match(b)
        check("matcher no longer proposes it",
              result.product.id if result.product else None, None)

    print("\nthe ruling survives a rematch  (this is the defect)")
    merged = _rematch_all()
    check("rematch merged nothing", merged, 0)
    with session_scope() as s:
        a, b = s.get(Offer, id_a), s.get(Offer, id_b)
        check("still separate after rematch", a.canonical_id != b.canonical_id, True)

    print("\nand across a re-crawl of the same listing")
    _ingest(CHARGER_B)
    with session_scope() as s:
        a, b = s.get(Offer, id_a), s.get(Offer, id_b)
        check("re-ingest did not re-merge", a.canonical_id != b.canonical_id, True)
        check("no duplicate offers", s.scalar(select(func.count(Offer.id))), 2)

    print("\nrejection is anchored to offers, not product ids")
    with session_scope() as s:
        # Move the rejected offer onto a brand-new product. A rejection stored
        # against a product id would be orphaned by this; an offer-anchored one
        # still resolves through whatever product the counterpart now sits on.
        detach_offer(s, s.get(Offer, id_b))
    with session_scope() as s:
        a, b = s.get(Offer, id_a), s.get(Offer, id_b)
        engine = MatchEngine(s)
        check("blocklist survives product churn",
              a.canonical_id in engine.blocked_canonical_ids(b), True)

    print("\nundo restores matchability")
    with session_scope() as s:
        a, b = s.get(Offer, id_a), s.get(Offer, id_b)
        removed = clear_rejections(s, b.id, a.canonical_id)
        check_true("rejection rows removed", removed >= 1)
    with session_scope() as s:
        check("no rejections left", s.scalar(select(func.count(MatchRejection.id))), 0)
    check("rematch merges again once unblocked", _rematch_all(), 1)

    print("\nvariants stay apart with no human input needed")
    id_10k, prod_10k = _ingest(POWERBANK_10K)
    id_20k, prod_20k = _ingest(POWERBANK_20K)
    # Same photo, but "10000mAh" vs "20000mAh" is a model-code conflict. The matcher
    # should separate these unaided -- if this ever starts merging, capacity variants
    # are silently collapsing into one price comparison.
    check("10k and 20k not merged", prod_10k != prod_20k, True)
    with session_scope() as s:
        check("and no rejection was needed",
              s.scalar(select(func.count(MatchRejection.id))), 0)

    print("\n" + "=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("rejections are durable")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
