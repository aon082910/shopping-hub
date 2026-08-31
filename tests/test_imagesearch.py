"""Reverse image search, offline.

Covers the property that makes the feature work at all: an identical or re-encoded
supplier photo finds the product, an unrelated photo does not, and the reported
distance actually tracks how different the images are.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_img_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sourcehub.db.session import init_db, session_scope  # noqa: E402
from sourcehub.pipeline.imagesearch import (  # noqa: E402
    UndecodableImage,
    find_by_bytes,
    phash_of_bytes,
)
from sourcehub.pipeline.ingest import IngestContext, ingest_offer  # noqa: E402
from sourcehub.scrapers.base import RawOffer  # noqa: E402

FAILS: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


def check_true(label, got):
    check(label, bool(got), True)


def _img(kind: str, size: int = 320, quality: int = 95, fmt: str = "PNG") -> bytes:
    """Render at a fixed canvas, then resize.

    Drawing at coordinates derived from ``size`` would make the "smaller" image a
    different *composition* rather than a scaled copy of the same one -- which is
    not what the downscale case is meant to test.
    """
    from PIL import Image as PILImage, ImageDraw

    canvas = 320
    img = PILImage.new("RGB", (canvas, canvas), (245, 245, 240))
    d = ImageDraw.Draw(img)
    if kind == "hub":
        d.rounded_rectangle([40, 90, canvas - 40, canvas - 90], radius=28, fill=(30, 40, 55))
        for i in range(4):
            x = 70 + i * 48
            d.rectangle([x, 130, x + 30, 170], fill=(220, 220, 210))
    elif kind == "drill":
        d.ellipse([50, 50, canvas - 50, canvas - 50], fill=(210, 120, 20))
        d.rectangle([canvas // 2 - 18, 30, canvas // 2 + 18, canvas - 30], fill=(40, 40, 40))
    if size != canvas:
        img = img.resize((size, size), PILImage.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, fmt, quality=quality)
    return buf.getvalue()


HUB_PHOTO = _img("hub")
DRILL_PHOTO = _img("drill")
HUB_REENCODED = _img("hub", quality=45, fmt="JPEG")   # same picture, lossy re-save
HUB_RESIZED = _img("hub", size=180)                    # same picture, downscaled


class FakeFetcher:
    MAP = {"hub": HUB_PHOTO, "drill": DRILL_PHOTO}

    def download(self, url, referer=None):
        for k, v in self.MAP.items():
            if k in url:
                return v
        raise RuntimeError(url)

    def close(self):
        pass


HUB = RawOffer(
    site_key="banggood", site_product_id="img-hub-1",
    url="https://www.banggood.com/usb-hub-p-991.html",
    title="8-in-1 USB C Hub Docking Station 4K HDMI",
    currency="USD", price_min=24.99, moq=1,
    image_urls=["https://img.example.com/hub.png"], detail_fetched=True,
)
DRILL = RawOffer(
    site_key="banggood", site_product_id="img-drill-1",
    url="https://www.banggood.com/drill-p-992.html",
    title="21V Cordless Impact Drill Brushless Power Tool",
    currency="USD", price_min=42.99, moq=1,
    image_urls=["https://img.example.com/drill.png"], detail_fetched=True,
)


def run() -> int:
    init_db()

    print()
    print("setup")
    with session_scope() as s:
        ctx = IngestContext(s)
        ctx.images._fetcher = FakeFetcher()
        for raw in (HUB, DRILL):
            offer = ingest_offer(ctx, raw)
            print(f"    {raw.site_product_id} -> product {offer.canonical_id}")

    print()
    print("exact image")
    with session_scope() as s:
        hits = find_by_bytes(s, HUB_PHOTO)
        check_true("finds something", hits)
        if hits:
            check("top hit is the hub", "hub" in hits[0].product.title_en.lower(), True)
            check("distance is zero", hits[0].distance, 0)
            check("labelled identical", hits[0].confidence, "identical image")
            check("score is full", round(hits[0].score, 2), 1.0)
            # One product owns several image rows; it must appear once.
            slugs = [h.product.slug for h in hits]
            check("no duplicate products", len(slugs), len(set(slugs)))

    print()
    print("re-encoded and resized copies")
    with session_scope() as s:
        for label, data in (("jpeg re-encode", HUB_REENCODED), ("downscale", HUB_RESIZED)):
            hits = find_by_bytes(s, data)
            top = hits[0] if hits else None
            ok = top is not None and "hub" in (top.product.title_en or "").lower()
            check(f"{label} still finds the hub", ok, True)
            if ok:
                print(f"          distance {top.distance} ({top.confidence})")

    print()
    print("unrelated image")
    with session_scope() as s:
        hits = find_by_bytes(s, DRILL_PHOTO)
        check_true("drill photo finds the drill", hits)
        if hits:
            check("and it ranks first",
                  "drill" in hits[0].product.title_en.lower(), True)
            hub_hits = [h for h in hits if "hub" in (h.product.title_en or "").lower()]
            # The hub may appear as a weak visual-similarity hit, but must never
            # outrank the actual product, and must never be called a match.
            if hub_hits:
                check("hub does not outrank the drill",
                      hub_hits[0].distance > hits[0].distance, True)
                check("hub is not labelled a photo match",
                      hub_hits[0].confidence, "visually similar")

    print()
    print("distance ordering")
    with session_scope() as s:
        hits = find_by_bytes(s, HUB_PHOTO)
        distances = [h.distance for h in hits]
        check("results are sorted closest first", distances, sorted(distances))

    print()
    print("bad input")
    with session_scope() as s:
        for label, payload in (("not an image", b"this is not an image"),
                               ("empty", b"")):
            try:
                find_by_bytes(s, payload)
                check(f"{label} raises", False, True)
            except UndecodableImage:
                check(f"{label} raises UndecodableImage", True, True)

    print()
    print("hashing is deterministic")
    check("same bytes, same hash",
          phash_of_bytes(HUB_PHOTO), phash_of_bytes(HUB_PHOTO))

    print()
    print("=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("reverse image search OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
