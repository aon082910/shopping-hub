"""Price watches, trust signals, freight and break-even."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_watch_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sqlalchemy import select  # noqa: E402

from sourcehub.db.models import CanonicalProduct, Offer, Watch  # noqa: E402
from sourcehub.db.session import init_db, session_scope  # noqa: E402
from sourcehub.pipeline.breakeven import analyse  # noqa: E402
from sourcehub.pipeline.freight import (  # noqa: E402
    load_freight_table,
    parse_dims_cm,
    parse_weight_kg,
)
from sourcehub.pipeline.ingest import IngestContext, ingest_offer  # noqa: E402
from sourcehub.pipeline.trust import assess_offer, assess_offers  # noqa: E402
from sourcehub.pipeline.watch import check_watches  # noqa: E402
from sourcehub.scrapers.base import RawOffer  # noqa: E402

FAILS: list = []


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


def _ingest(price, pid="w-1", site="banggood", **kw):
    raw = RawOffer(site_key=site, site_product_id=pid,
                   url=f"https://example.test/{pid}", title="Watched Widget",
                   currency="USD", price_min=price, moq=1, detail_fetched=True, **kw)
    with session_scope() as s:
        ctx = IngestContext(s)
        ctx.images._fetcher = NoImages()
        o = ingest_offer(ctx, raw, fetch_images=False)
        return o.canonical_id


def run() -> int:
    init_db()

    print()
    print("watches")
    pid = _ingest(20.0)
    with session_scope() as s:
        s.add(Watch(canonical_id=pid, target_usd=15.0, last_price_usd=20.0))
    with session_scope() as s:
        check("no trigger above target", len(check_watches(s, notify=False)), 0)

    _ingest(12.0)                      # price drops through the target
    with session_scope() as s:
        fired = check_watches(s, notify=False)
        check("fires on crossing", len(fired), 1)
        if fired:
            check("reports the new price", fired[0].price, 12.0)
            check("reports the drop", fired[0].drop, 8.0)

    with session_scope() as s:
        # Still below target, but already reported: firing again every crawl would
        # be noise, so it must stay quiet until the price recovers.
        check("does not re-fire while still low", len(check_watches(s, notify=False)), 0)

    _ingest(25.0)                      # back above
    with session_scope() as s:
        check_watches(s, notify=False)
    _ingest(11.0)                      # and down again
    with session_scope() as s:
        check("re-arms after recovery", len(check_watches(s, notify=False)), 1)

    print()
    print("watch respects direct-only")
    with session_scope() as s:
        w = s.scalar(select(Watch))
        w.direct_only = True
        w.last_price_usd = 99.0
    _ingest(3.0, pid="w-cn", site="1688")   # cheaper but needs an agent
    with session_scope() as s:
        fired = check_watches(s, notify=False)
        # The 1688 price must be ignored; the banggood one is what counts.
        if fired:
            check("agent-only price ignored", fired[0].price, 11.0)

    print()
    print("trust signals")
    good = assess_offer({"rating": 4.8, "review_count": 4200, "seller_years": 8,
                         "verified": True})
    check("healthy seller is ok", good.level, "ok")
    check_true("and says why", good.positives)

    bait = assess_offer(
        {"price_usd": 2.0, "rating": 3.9, "review_count": 2, "seller_years": 0},
        peer_prices=[12.0, 11.5, 13.0, 12.5],
    )
    check("bait listing is risk", bait.level, "risk")
    check_true("flags the price outlier",
               any("below the median" in r for r in bait.reasons))
    check_true("flags the new account",
               any("under a year old" in r for r in bait.reasons))

    check("middling data is ok, not unknown",
          assess_offer({"rating": 4.5, "review_count": 500}).level, "ok")
    check("genuinely no data is unknown",
          assess_offer({"price_usd": 5.0}).level, "unknown")
    # A single cheap listing with no peers must not be called an outlier.
    check("no peers, no outlier claim",
          any("below the median" in r
              for r in assess_offer({"price_usd": 1.0}, [1.0]).reasons), False)

    print()
    print("freight")
    check("grams", parse_weight_kg("250 g"), 0.25)
    check("kg", parse_weight_kg("1.2 kg"), 1.2)
    check("pounds", round(parse_weight_kg("3 lbs"), 3), 1.361)
    check("no weight", parse_weight_kg("blue"), None)
    check("dims cm", parse_dims_cm("20 x 15 x 5 cm"), (20.0, 15.0, 5.0))
    check("dims mm", parse_dims_cm("200x150x50 mm"), (20.0, 15.0, 5.0))
    check("model number is not dimensions", parse_dims_cm("ESP32"), None)

    t = load_freight_table()
    small = t.estimate(1, weight_kg=0.15)
    bulky = t.estimate(1, weight_kg=0.3, dims_cm=(40, 30, 25))
    # The whole point: a light bulky parcel bills on volume, not on weight.
    check_true(f"volumetric dominates ({bulky['chargeable_kg']}kg from 0.3kg actual)",
               bulky["chargeable_kg"] > bulky["actual_kg"])
    check_true("and costs more than the small one", bulky["usd"] > small["usd"])
    check("guessed weight is flagged",
          t.estimate(1, category_path="tools")["guessed_weight"], True)

    print()
    print("break-even")
    offers = [
        {"title": "retail", "site_name": "AliExpress", "price_usd": 9.0, "moq": 1,
         "shipping_cost_usd": 2.0},
        {"title": "bulk", "site_name": "Alibaba", "price_usd": 0.90, "moq": 500,
         "shipping_cost_usd": 50.0},
        {"title": "us", "site_name": "eBay", "price_usd": 14.0, "moq": 1,
         "shipping_cost_usd": 0.0, "site_is_baseline": True},
    ]
    econ = analyse(offers)
    check("one break-even found", len(econ["break_evens"]), 1)
    if econ["break_evens"]:
        be = econ["break_evens"][0]
        # 100 x 0.90 + 50 = 140 (buying 500) vs 100 x 9 + 2 = 902.
        check("crossover quantity", be["quantity"], 100)
        check("names the wholesale site", be["site"], "Alibaba")
    check_true("baseline computed", econ["baseline"])
    if econ["baseline"]:
        check("importing wins at one unit", econ["baseline"]["importing_wins"], True)
        check("and by how much", econ["baseline"]["saving"], 3.0)
    check("single unit picks retail, not bulk", econ["rows"][0]["site"], "AliExpress")
    overage_rows = [r for r in econ["rows"] if r["overage"]]
    check_true("MOQ overage is surfaced", overage_rows)

    print()
    print("=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("watches, trust, freight and break-even OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
