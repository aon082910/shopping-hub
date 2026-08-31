"""Replay saved site HTML through the real adapters, offline.

Selector rot is the main ongoing cost of this project, and it is invisible until a
crawl quietly returns nothing. These tests replay HTML captured from the live sites
so that a change *you* make which breaks parsing fails immediately, in CI, without
network.

Sites with no fixture are reported as SKIP, not as passes -- a green run that
silently tested nothing would be worse than a red one.

To add a site:
    python -m sourcehub.cli selftest --site dhgate --save-fixture

A fixture is a snapshot, so it cannot tell you whether your selectors match the
site *today*; only `selftest` against the live site answers that. What it does tell
you is that parsing of known-good HTML still works.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_adp_"))
os.environ.setdefault("SOURCEHUB_DB_URL", f"sqlite:///{(_TMP / 't.db').as_posix()}")
os.environ.setdefault("SOURCEHUB_MEDIA_DIR", str(_TMP / "media"))

from sourcehub.fixtures import bind_fixture, has_fixture, load_manifest  # noqa: E402
from sourcehub.scrapers.registry import ADAPTERS, get_adapter  # noqa: E402

FAILS: list[str] = []
SKIPS: list[str] = []

# A parsed listing is only useful if it has these. Anything less and the offer
# cannot be ingested at all, so they are hard requirements rather than ratios.
REQUIRED = ("site_product_id", "url", "title")

# Ratios that must hold across the parsed page. Set deliberately below 1.0: real
# result pages legitimately contain ad slots and "price on request" listings.
MIN_PRICED_RATIO = 0.5
MIN_IMAGE_RATIO = 0.6
MIN_OFFERS = 3

# Per-site overrides, each with a reason. A site is only allowed to fall below the
# general bar where its markup genuinely cannot do better -- writing the reason down
# keeps this a considered exception rather than a quietly lowered standard.
SITE_EXPECTATIONS: dict[str, dict] = {
    "dhgate": {
        "min_image_ratio": 0.1,
        "why": "search cards lazy-load their photos, so the <img> is a promo "
               "placeholder and the real image only exists after the detail fetch",
    },
    "globalsources": {
        "min_priced_ratio": 0.2,
        "why": "a large share of listings are 'price on request' by design",
    },
}


def expectation(site: str, key: str, default: float) -> float:
    return SITE_EXPECTATIONS.get(site, {}).get(key, default)


def fail(site: str, msg: str) -> None:
    FAILS.append(f"{site}: {msg}")
    print(f"    FAIL  {msg}")


def check_offers(site: str, offers: list) -> None:
    if len(offers) < MIN_OFFERS:
        fail(site, f"only {len(offers)} listings parsed (expected >= {MIN_OFFERS})")
        return
    print(f"    ok    {len(offers)} listings parsed")

    for field in REQUIRED:
        missing = [o for o in offers if not getattr(o, field, None)]
        if missing:
            fail(site, f"{len(missing)}/{len(offers)} listings missing {field}")
        else:
            print(f"    ok    every listing has {field}")

    ids = [o.site_product_id for o in offers]
    if len(set(ids)) != len(ids):
        fail(site, f"duplicate product ids ({len(ids) - len(set(ids))} repeats)")
    else:
        print("    ok    product ids are unique")

    priced = sum(1 for o in offers if o.price_min is not None)
    min_priced = expectation(site, "min_priced_ratio", MIN_PRICED_RATIO)
    if priced / len(offers) < min_priced:
        fail(site, f"only {priced}/{len(offers)} listings have a price")
    else:
        print(f"    ok    {priced}/{len(offers)} priced")

    imaged = sum(1 for o in offers if o.image_urls)
    min_imaged = expectation(site, "min_image_ratio", MIN_IMAGE_RATIO)
    if imaged / len(offers) < min_imaged:
        fail(site, f"only {imaged}/{len(offers)} listings have an image")
    else:
        note = SITE_EXPECTATIONS.get(site, {}).get("why", "")
        print(f"    ok    {imaged}/{len(offers)} with images"
              + (f"  (relaxed: {note})" if note and min_imaged < MIN_IMAGE_RATIO else ""))

    # Zero is not a price. It would win every cheapest-price comparison in the
    # catalog, so an adapter emitting one is a defect, not a cheap product.
    zero = [o for o in offers if o.price_min is not None and o.price_min <= 0]
    if zero:
        fail(site, f"{len(zero)} listings priced at or below zero")

    bad_urls = [o for o in offers if not str(o.url).startswith("http")]
    if bad_urls:
        fail(site, f"{len(bad_urls)} listings have a non-absolute URL "
                   f"(e.g. {bad_urls[0].url!r})")
    else:
        print("    ok    all URLs absolute")




def check_detail(site: str, adapter, offer, manifest: dict) -> None:
    if not manifest.get("detail_url"):
        print("    --    no detail page captured")
        return
    before_specs = len(offer.specs)
    enriched = adapter.fetch_detail(offer)
    if not enriched.detail_fetched:
        fail(site, "fetch_detail did not mark the offer as fetched")
        return

    gained = len(enriched.specs) - before_specs
    # A product page that yields no specs, no images and no description means the
    # detail selectors have rotted even though search still works.
    signals = gained > 0 or len(enriched.image_urls) > 0 or bool(enriched.description)
    if not signals:
        fail(site, "detail page produced no specs, images or description")
    else:
        print(f"    ok    detail: +{gained} specs, {len(enriched.image_urls)} images"
              + (", description" if enriched.description else ""))


def run_site(site: str) -> None:
    print(f"\n{site}")
    if not has_fixture(site):
        SKIPS.append(site)
        print("    SKIP  no fixture "
              f"(python -m sourcehub.cli selftest --site {site} --save-fixture)")
        return

    manifest = load_manifest(site)
    tag = "  [SYNTHETIC]" if manifest.get("synthetic") else ""
    print(f"    captured {manifest.get('captured_at', '?')[:19]}"
          f" keyword={manifest.get('keyword', '?')!r}{tag}")

    adapter = get_adapter(site)
    try:
        bind_fixture(adapter, site)
        keyword = manifest.get("keyword") or "usb hub"
        offers = list(adapter.search(keyword, max_pages=1))
        check_offers(site, offers)
        if offers:
            check_detail(site, adapter, offers[0], manifest)
    except Exception as e:
        import traceback

        fail(site, f"{type(e).__name__}: {e}")
        traceback.print_exc(limit=6)
    finally:
        adapter.close()


def main() -> int:
    sites = sys.argv[1:] or sorted(ADAPTERS)
    for site in sites:
        run_site(site)

    print()
    print("=" * 66)
    tested = len(sites) - len(SKIPS)
    print(f"{tested}/{len(sites)} adapters replayed, {len(SKIPS)} skipped")
    if SKIPS:
        print(f"  no fixture: {', '.join(SKIPS)}")
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    if tested == 0:
        print()
        print("Nothing was actually tested. Capture a fixture first:")
        print("  python -m sourcehub.cli selftest --save-fixture")
    print("adapter replay OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
