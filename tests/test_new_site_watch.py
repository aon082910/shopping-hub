"""The on_new_site watch: alert when a site that wasn't selling this product starts to.

This is the cross-marketplace thesis the whole app is built on, turned into an
alert: SourceHub's value is comparing the same product across sites, and "a second
seller just showed up" is the single most direct signal that comparison newly
matters for a listing. Nothing scraped this before -- the feature is new.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_newsite_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sqlalchemy import select  # noqa: E402

from sourcehub.db.models import CanonicalProduct, Offer, Site, Watch  # noqa: E402
from sourcehub.db.session import init_db, session_scope  # noqa: E402
from sourcehub.pipeline.ingest import IngestContext, ingest_offer  # noqa: E402
from sourcehub.pipeline.watch import Trigger, _trigger_text, check_watches  # noqa: E402
from sourcehub.scrapers.base import RawOffer  # noqa: E402

FAILS: list[str] = []


def check(label, got, want=True) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


class NoImages:
    def download(self, url, referer=None):
        raise RuntimeError("no images")

    def close(self):
        pass


def _ingest(price, pid, site, title="Cross-Site Widget") -> int:
    raw = RawOffer(site_key=site, site_product_id=pid,
                   url=f"https://example.test/{site}/{pid}", title=title,
                   currency="USD", price_min=price, moq=1, detail_fetched=True)
    with session_scope() as s:
        ctx = IngestContext(s)
        ctx.images._fetcher = NoImages()
        o = ingest_offer(ctx, raw, fetch_images=False)
        return o.canonical_id


def _add_offer_to(canonical_id: int, site_key: str, price: float, pid: str) -> None:
    """Attach a second listing to an existing product without going through the
    matcher -- these tests are about the watch, not about matching."""
    with session_scope() as s:
        site = s.scalar(select(Site).where(Site.key == site_key))
        s.add(Offer(
            site_id=site.id, canonical_id=canonical_id, site_product_id=pid,
            url=f"https://example.test/{site_key}/{pid}", title_raw="Cross-Site Widget",
            price_usd=price, is_active=True, in_stock=True,
        ))


def main() -> None:
    init_db()

    print("a watch seeded at creation with the sites it already has")
    pid = _ingest(9.99, "a1", "aliexpress")
    with session_scope() as s:
        ali_id = s.scalar(select(Site.id).where(Site.key == "aliexpress"))
        s.add(Watch(canonical_id=pid, on_new_site=True, known_site_ids=[ali_id]))

    with session_scope() as s:
        check("no new site yet -> no trigger", len(check_watches(s, notify=False)), 0)

    _add_offer_to(pid, "dhgate", 8.50, "d1")
    with session_scope() as s:
        fired = check_watches(s, notify=False)
        check("a genuinely new site fires exactly one trigger", len(fired), 1)
        check("names the new site", fired[0].new_sites, ["DHgate"])
        w = s.scalar(select(Watch).where(Watch.canonical_id == pid))
        check("known_site_ids grows to include it", len(w.known_site_ids), 2)

    with session_scope() as s:
        check("does not re-fire for a site it already knows about",
              len(check_watches(s, notify=False)), 0)

    print("\na watch created with an empty baseline doesn't treat existing sites as new")
    pid2 = _ingest(12.0, "a2", "aliexpress", title="Second Unrelated Cross-Site Gadget")
    _add_offer_to(pid2, "dhgate", 11.0, "d2")
    with session_scope() as s:
        s.add(Watch(canonical_id=pid2, on_new_site=True))  # known_site_ids defaults to []

    with session_scope() as s:
        check("first check only seeds the baseline, does not fire",
              len(check_watches(s, notify=False)), 0)
        w = s.scalar(select(Watch).where(Watch.canonical_id == pid2))
        check("baseline now has both existing sites", len(w.known_site_ids), 2)

    _add_offer_to(pid2, "banggood", 13.0, "b2")
    with session_scope() as s:
        fired = check_watches(s, notify=False)
        check("a real third site still fires after the seed check", len(fired), 1)
        check("names only the actually-new site", fired[0].new_sites, ["Banggood"])

    print("\n_trigger_text never crashes on a watch with no target_usd")
    with session_scope() as s:
        product = s.get(CanonicalProduct, pid)
        restock_watch = Watch(canonical_id=pid, on_restock=True, target_usd=None)
        newsite_watch = Watch(canonical_id=pid, on_new_site=True, target_usd=None)
        price_watch = Watch(canonical_id=pid, target_usd=7.5)

        restock_text = _trigger_text(Trigger(restock_watch, product, 9.99, None, "eBay"))
        newsite_text = _trigger_text(
            Trigger(newsite_watch, product, 9.99, None, "eBay", new_sites=["DHgate"])
        )
        price_text = _trigger_text(Trigger(price_watch, product, 6.5, 9.99, "eBay"))

        check("restock message mentions stock, not a target price", "back in stock" in restock_text)
        check("new-site message names the site", "DHgate" in newsite_text)
        check("price message includes the target", "target $7.50" in price_text)

    print("\n" + "=" * 60)
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(" -", f)
        sys.exit(1)
    print("new-site watch OK")


if __name__ == "__main__":
    main()
