"""Supplier entities and duty estimation."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_sup_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sqlalchemy import func, select  # noqa: E402

from sourcehub.db.models import Offer, Supplier  # noqa: E402
from sourcehub.db.session import init_db, session_scope  # noqa: E402
from sourcehub.duty import (  # noqa: E402
    DutyTable, check_against_usitc, load_duty_table, parse_usitc_rate,
)
from sourcehub.pipeline.ingest import IngestContext, ingest_offer  # noqa: E402
from sourcehub.scrapers.base import RawOffer  # noqa: E402
from sourcehub.util.http import Response  # noqa: E402

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


def offer(pid, seller, price, site="alibaba", **kw):
    return RawOffer(site_key=site, site_product_id=pid,
                    url=f"https://www.alibaba.com/p/{pid}.html",
                    title=f"Product {pid}", currency="USD", price_min=price,
                    moq=1, seller_name=seller, detail_fetched=True, **kw)


def run() -> int:
    init_db()

    print()
    print("supplier identity")
    with session_scope() as s:
        ctx = IngestContext(s)
        ctx.images._fetcher = NoImages()
        ingest_offer(ctx, offer("s1", "Shenzhen Rocketek Co.", 9.0,
                                is_verified_supplier=True, seller_years=9), fetch_images=False)
        ingest_offer(ctx, offer("s2", "Shenzhen Rocketek Co.", 12.0), fetch_images=False)
        ingest_offer(ctx, offer("s3", "Other Factory Ltd", 7.0), fetch_images=False)
        # Same name on a different site is a different company, not the same one.
        ingest_offer(ctx, offer("s4", "Shenzhen Rocketek Co.", 8.0, site="dhgate"),
                     fetch_images=False)

    with session_scope() as s:
        check("suppliers deduped by name", s.scalar(select(func.count(Supplier.id))), 3)
        rocketek = s.scalar(
            select(Supplier).where(Supplier.name_norm == "shenzhen rocketek co.",
                                   Supplier.site_id == 2)
        )
        check_true("supplier row exists", rocketek is not None)
        attached = s.scalars(
            select(Offer).where(Offer.supplier_id == rocketek.id)
        ).all() if rocketek else []
        check("both alibaba listings attached", len(attached), 2)
        # Values recorded on one listing must survive a later listing that omits them.
        check("verified flag sticky", rocketek.is_verified, True)
        check("years retained", rocketek.years_active, 9)

    print()
    print("duty ships off by default (a fresh DutyTable, not the project's own "
          "duty.yaml -- that file now carries real user-supplied rates, see "
          "below, and load_duty_table() has no way to override its path)")
    check("ships disabled", DutyTable().enabled, False)
    check("estimates nothing", DutyTable().estimate(100.0, "apparel"), (None, None))

    print()
    print("the project's live duty.yaml is wired into the real ingest pipeline")
    table = load_duty_table()
    check_true("duty.yaml is enabled with real, sourced rates", table.enabled)
    with session_scope() as s:
        o = s.scalar(select(Offer).where(Offer.site_product_id == "s1"))
        # s1 has no category set at all (the `offer()` helper above never sets
        # one), so it falls through to default_rate -- 0.0 today, but a
        # computed 0.0 (duty considered, just zero), not None (duty excluded
        # entirely), now that duty.yaml is enabled.
        check("duty computed at the default rate for an uncategorized offer",
              o.duty_usd, 0.0)
        check("landed cost still excludes a $0 duty the same as no duty",
              o.landed_cost_usd, 9.0)
    # Locks in the sourced rate itself -- HQ H348342: USB hubs classify under
    # HTS 8471.80.10, general duty rate Free. Catches a future edit silently
    # drifting this away from what was actually verified.
    check("usb-hubs-docks rate matches the sourced CBP ruling (HQ H348342)",
          table.rate_for("computers/usb-hubs-docks"), 0.0)
    check("hand-tools rate matches the sourced CBP ruling (N363313)",
          table.rate_for("tools/hand-tools"), 0.053)
    check("measuring-test-equipment rate matches the sourced CBP ruling (N363956)",
          table.rate_for("tools/measuring-test-equipment"), 0.053)
    check("gaming-accessories rate matches two corroborating CBP rulings "
          "(N363514, N363251)", table.rate_for("toys/gaming-accessories"), 0.0)
    check("shipping-supplies rate matches the sourced CBP ruling (N363423)",
          table.rate_for("packaging/shipping-supplies"), 0.03)
    check("an unresearched tools subcategory still falls back to the default "
          "rate, not the sibling hand-tools/measuring rate",
          table.rate_for("tools/power-tools"), 0.0)
    check_true("as_of is a real, non-empty date stamp", table.as_of)

    print()
    print("duty when configured")
    t = DutyTable(enabled=True, default_rate=0.05,
                  by_category={"apparel": 0.16, "apparel/shoes": 0.20})
    check("longest prefix wins", t.estimate(100.0, "apparel/shoes"), (0.20, 20.0))
    check("parent prefix applies", t.estimate(100.0, "apparel/bags"), (0.16, 16.0))
    check("default for unmatched", t.estimate(100.0, "tools"), (0.05, 5.0))
    check("default for missing category", t.estimate(100.0, None), (0.05, 5.0))

    dm = DutyTable(enabled=True, default_rate=0.10, de_minimis_usd=800.0)
    check("under de minimis is free", dm.estimate(500.0, "x"), (0.0, 0.0))
    check("over de minimis is charged", dm.estimate(1000.0, "x"), (0.10, 100.0))

    print()
    print("staleness is visible")
    check("no date -> unknown", DutyTable(as_of="").staleness_days, None)
    check("bad date -> unknown", DutyTable(as_of="not-a-date").staleness_days, None)
    check_true("valid date -> a number",
               isinstance(DutyTable(as_of="2020-01-01").staleness_days, int))

    print()
    print("parsing USITC's own rate text")
    check("Free -> 0.0", parse_usitc_rate("Free"), 0.0)
    check("percent -> fraction", parse_usitc_rate("5.3%"), 0.053)
    check("blank -> not comparable", parse_usitc_rate(""), None)
    check("a compound/specific rate this project can't reduce to one "
          "ad valorem number -> not comparable, not a false mismatch",
          parse_usitc_rate("5.3¢/kg + 3%"), None)
    check("None -> not comparable", parse_usitc_rate(None), None)

    print()
    print("re-verifying duty.yaml's rates against USITC's live HTS table "
          "(fake HTTP -- the real endpoint is checked live by `duty-check`, "
          "not by this offline test)")

    class FakeUsitcFetcher:
        """Stands in for one chapter's worth of USITC getRates rows."""

        def __init__(self, rows_by_chapter):
            self.rows_by_chapter = rows_by_chapter
            self.calls: list[str] = []

        def get(self, url, *, params=None, headers=None, referer=None, expect_json=False):
            htsno = (params or {}).get("htsno", "")
            self.calls.append(htsno)
            chapter = htsno.split(".")[0][:2]
            rows = self.rows_by_chapter.get(chapter)
            if rows is None:
                raise RuntimeError("simulated network failure")
            return Response(url=url, status=200, text=json.dumps(rows))

    fake_table = DutyTable(
        enabled=True,
        by_category={"computers/usb-hubs-docks": 0.0, "tools/hand-tools": 0.02,
                     "toys/gaming-accessories": 0.0},
        hts_by_category={
            "computers/usb-hubs-docks": "8471.80.10",
            # Deliberately wrong vs. the fake "live" value below (0.053), to
            # prove drift is actually detected, not just always reported clean.
            "tools/hand-tools": "8205.59.55",
            # No fake data seeded for chapter 95 at all, to prove a request
            # failure is reported as an error, not silently treated as a match.
            "toys/gaming-accessories": "9504.50.00",
        },
    )
    fake_fetcher = FakeUsitcFetcher({
        "84": [{"htsno": "8471.80.10.00", "description": "Control or adapter units",
               "general": "Free"}],
        "82": [{"htsno": "8205.59.55.60", "description": "Other handtools",
               "general": "5.3%"}],
    })
    results = {r["category"]: r for r in check_against_usitc(fake_table, fetcher=fake_fetcher)}

    check("usb-hubs-docks matches -> no drift",
          results["computers/usb-hubs-docks"]["drift"], False)
    check("usb-hubs-docks live rate parsed",
          results["computers/usb-hubs-docks"]["live_rate"], 0.0)
    check_true("hand-tools mismatch is flagged as drift",
               results["tools/hand-tools"]["drift"])
    check("hand-tools live rate still reported alongside the drift flag",
          results["tools/hand-tools"]["live_rate"], 0.053)
    check_true("a request failure is reported as an error, not a false match",
               results["toys/gaming-accessories"]["error"] is not None)
    check("a category with no HTS line configured is never checked at all",
          "computers/monitors" in results, False)

    print()
    print("=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("suppliers and duty OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
