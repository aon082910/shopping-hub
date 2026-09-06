"""match-explain: scoring two specific listings against each other on demand.

The gap this closes: ``MatchEngine.match()`` only ever scores an offer against
whatever candidate search happens to surface (image LSH bands, shared title
tokens, model codes). Most pairs in a thin catalog never reach that scoring step
at all, so "why didn't these two merge" has historically had no answer beyond
re-reading the matching code. ``explain_pair`` bypasses candidate search and
scores two specific listings directly; this file checks it agrees with what the
real pipeline actually decided, and that its extra diagnostics (blocked-by-human,
was-a-candidate, tier-1 shortcuts) are each individually correct.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_explain_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sqlalchemy import select  # noqa: E402

from sourcehub.config import load_crawl_config  # noqa: E402
from sourcehub.db.models import CanonicalProduct, Offer, OfferSpec, Site  # noqa: E402
from sourcehub.db.session import init_db, session_scope  # noqa: E402
from sourcehub.pipeline.ingest import IngestContext, ingest_offer  # noqa: E402
from sourcehub.pipeline.matching import (  # noqa: E402
    MatchEngine,
    detach_offer,
    record_rejection,
)
from sourcehub.scrapers.base import RawOffer, RawSpec  # noqa: E402

FAILS: list[str] = []


def check(label, got, want=True) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


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


# Two visually unrelated photos (same shapes test_rejections.py uses, and for the
# same reason: a plain color swap is not enough distance apart to trust a phash
# assertion on -- these differ in structure, not just color).
PHOTOS = {"pb.png": _stripes(), "mouse.png": _rings()}


class FakeFetcher:
    def download(self, url, referer=None):
        for name, data in PHOTOS.items():
            if name in url:
                return data
        raise RuntimeError(f"no fixture photo for {url}")

    def close(self):
        pass


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

MOUSE = RawOffer(
    site_key="geekbuying", site_product_id="ms-ccc",
    url="https://www.geekbuying.com/item/3003.html",
    title="Wireless Ergonomic Vertical Mouse 2.4G Silent Click",
    currency="USD", price_min=15.00, moq=1,
    image_urls=["https://cdn.example.com/mouse.png"],
    detail_fetched=True,
)


def _ingest(raw: RawOffer, cfg) -> Offer:
    with session_scope() as session:
        ctx = IngestContext(session, cfg)
        ctx.images._fetcher = FakeFetcher()
        offer = ingest_offer(ctx, raw)
        oid = offer.id
    with session_scope() as session:
        return session.get(Offer, oid)


def _assert_fixture_photos_differ() -> None:
    from sourcehub.pipeline.images import compute_phash, hamming

    hashes = [compute_phash(_pil_open(b)) for b in PHOTOS.values()]
    distance = hamming(*hashes)
    check(f"fixture photos are distinguishable (phash distance {distance})",
          distance is not None and distance > 10)


def _pil_open(data: bytes):
    from PIL import Image as PILImage

    return PILImage.open(io.BytesIO(data))


def main() -> None:
    init_db()
    cfg = load_crawl_config()
    _assert_fixture_photos_differ()

    offer_a = _ingest(CHARGER_A, cfg)
    offer_b = _ingest(CHARGER_B, cfg)
    offer_m = _ingest(MOUSE, cfg)

    with session_scope() as session:
        b = session.get(Offer, offer_b.id)
        check("fixture merges B onto A's product before the split", b.canonical_id == offer_a.canonical_id)
        product_b_alone = detach_offer(session, b)
        product_b_id = product_b_alone.id

    print("weighted merge candidates (same photo, near-identical titles)")
    with session_scope() as session:
        offer = session.get(Offer, offer_b.id)
        product_a = session.get(CanonicalProduct, offer_a.canonical_id)
        specs = session.scalars(select(OfferSpec).where(OfferSpec.offer_id == offer.id)).all()
        engine = MatchEngine(session, cfg)
        exp = engine.explain_pair(offer, product_a, specs)

        check("candidate search finds this pair", exp.was_candidate)
        check("not blocked", exp.blocked_by_rejection, False)
        check("scores above the auto-merge threshold", exp.score >= engine.auto_threshold)
        check("method is weighted", exp.method, "weighted")
        check("image signal present and strong", exp.signals.get("image", 0) > 0.85)
        check("title signal present", exp.signals.get("title", 0) > 0)
        check("base score recorded", "base" in exp.signals)

    print("\nunrelated listings (different product entirely)")
    with session_scope() as session:
        offer = session.get(Offer, offer_m.id)
        product_a = session.get(CanonicalProduct, offer_a.canonical_id)
        engine = MatchEngine(session, cfg)
        exp = engine.explain_pair(offer, product_a, [])

        check("scores below the review threshold", exp.score < engine.review_threshold)
        check("method is below_threshold", exp.method, "below_threshold")
        check("image signal reflects unrelated photos", exp.signals.get("image", 1) < 0.5)

    print("\na human rejection overrides an otherwise-passing score")
    with session_scope() as session:
        offer = session.get(Offer, offer_b.id)
        product_a = session.get(CanonicalProduct, offer_a.canonical_id)
        record_rejection(session, offer.id, product_a.id)

        specs = session.scalars(select(OfferSpec).where(OfferSpec.offer_id == offer.id)).all()
        engine = MatchEngine(session, cfg)
        exp = engine.explain_pair(offer, product_a, specs)

        check("blocked_by_rejection is true", exp.blocked_by_rejection)
        check("method reflects the block despite a passing score", exp.method, "blocked")

    print("\ntier 1: matching GTIN short-circuits the weighted signals entirely")
    with session_scope() as session:
        site = session.scalar(select(Site).where(Site.key == "aliexpress"))
        offer = Offer(
            site_id=site.id, site_product_id="gtin-test", url="https://example.test/g1",
            title_raw="Totally Unrelated Title", gtin="00012345678905",
        )
        session.add(offer)
        product = CanonicalProduct(
            slug=f"gtin-product-{offer_m.id}", title_en="Another Unrelated Title",
            gtin="00012345678905",
        )
        session.add(product)
        session.flush()

        engine = MatchEngine(session, cfg)
        exp = engine.explain_pair(offer, product, [])
        check("gtin_hit flagged", exp.gtin_hit)
        check("method is gtin", exp.method, "gtin")
        check("score is 1.0", exp.score, 1.0)

    print("\n" + "=" * 60)
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(" -", f)
        sys.exit(1)
    print("match-explain OK")


if __name__ == "__main__":
    main()
