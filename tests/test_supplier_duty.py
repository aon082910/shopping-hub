"""Supplier entities and duty estimation."""

from __future__ import annotations

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
from sourcehub.duty import DutyTable, load_duty_table  # noqa: E402
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
    print("duty is off unless configured")
    table = load_duty_table()
    check("ships disabled", table.enabled, False)
    check("estimates nothing", table.estimate(100.0, "apparel"), (None, None))
    with session_scope() as s:
        o = s.scalar(select(Offer).where(Offer.site_product_id == "s1"))
        check("no duty recorded", o.duty_usd, None)
        check("landed cost excludes duty", o.landed_cost_usd, 9.0)

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
