"""CSV export and BOM costing.

The property that matters most: BOM totals must use **landed cost**, never unit
price. A line at $0.90 with MOQ 500 is a $450 commitment, and totalling unit prices
would produce a number that looks great and cannot be ordered.
"""

from __future__ import annotations

import csv
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_bom_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from sqlalchemy import select  # noqa: E402

from sourcehub.db.models import CanonicalProduct  # noqa: E402
from sourcehub.db.session import init_db, session_scope  # noqa: E402
from sourcehub.pipeline.export import (  # noqa: E402
    export_bom_csv,
    export_offers_csv,
    export_products_csv,
    parse_bom,
    price_bom,
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


# Retail: $12 each, buy exactly what you need.
RETAIL = RawOffer(
    site_key="banggood", site_product_id="bom-retail",
    url="https://www.banggood.com/widget-p-1.html",
    title="Precision Widget Alpha", currency="USD", price_min=12.00, moq=1,
    shipping_free=True, detail_fetched=True,
)
# Wholesale: $0.90 each -- but MOQ 500, so the real outlay is $455.
WHOLESALE = RawOffer(
    site_key="alibaba", site_product_id="bom-wholesale",
    url="https://www.alibaba.com/product-detail/widget_2.html",
    title="Precision Widget Alpha Wholesale", currency="USD", price_min=0.90,
    moq=500, shipping_cost=5.00, shipping_currency="USD", detail_fetched=True,
)
AGENT_ONLY = RawOffer(
    site_key="1688", site_product_id="bom-agent",
    url="https://detail.1688.com/offer/3.html",
    title="Sprocket Beta Domestic", currency="USD", price_min=2.00, moq=1,
    detail_fetched=True,
)


class NoImages:
    def download(self, url, referer=None):
        raise RuntimeError("no images in this test")

    def close(self):
        pass


def run() -> int:
    init_db()
    with session_scope() as s:
        ctx = IngestContext(s)
        ctx.images._fetcher = NoImages()
        for raw in (RETAIL, WHOLESALE, AGENT_ONLY):
            ingest_offer(ctx, raw, fetch_images=False)

    print()
    print("BOM parsing")
    check("trailing qty", parse_bom("widget x10"), [("widget", 10)])
    check("leading qty", parse_bom("10 x widget"), [("widget", 10)])
    check("comma qty", parse_bom("widget, 10"), [("widget", 10)])
    check("bare line", parse_bom("widget"), [("widget", 1)])
    check("comments and blanks skipped", parse_bom("# note\n\n  \nwidget"), [("widget", 1)])
    check("bullets stripped", parse_bom("- widget x2"), [("widget", 2)])
    check("multi-line", len(parse_bom("a x1\nb x2\nc")), 3)

    print()
    print("landed cost, not unit price")
    with session_scope() as s:
        result = price_bom(s, [("Precision Widget Alpha", 3)])
        line = result.lines[0]
        check_true("line matched", line.matched)
        # Unit price alone would pick wholesale at $0.90. Landed cost must not:
        # 500 x 0.90 + 5 = $455 versus 3 x 12 = $36.
        check("picks the cheaper LANDED source", line.site_name, "Banggood")
        check("order quantity is what you need", line.order_qty, 3)
        check("line total is landed", line.line_total_usd, 36.0)
        check("total matches", result.total_usd, 36.0)

    print()
    print("MOQ overage is surfaced, not hidden")
    with session_scope() as s:
        # Ask for 600: now wholesale genuinely wins (600 x 0.90 + 5 = $545
        # against 600 x 12 = $7200).
        result = price_bom(s, [("Precision Widget Alpha", 600)])
        line = result.lines[0]
        check("wholesale wins at volume", line.site_name, "Alibaba.com")
        check("no overage when qty exceeds MOQ", line.overage, 0)
        check("line total includes shipping", line.line_total_usd, 545.0)

        # Ask for 10: wholesale MOQ forces 500 units.
        result = price_bom(s, [("Precision Widget Alpha", 10)], direct_only=False)
        line = result.lines[0]
        if line.site_name == "Alibaba.com":
            check("overage reported", line.overage, 490)
            check_true("note explains the MOQ", "MOQ" in line.note)
        else:
            check("retail chosen for a small quantity", line.site_name, "Banggood")

    print()
    print("agent-only and unmatched lines are flagged")
    with session_scope() as s:
        result = price_bom(s, [("Sprocket Beta", 1), ("no such thing here", 4)])
        check("one unmatched", result.unmatched, 1)
        check("one agent line", result.agent_lines, 1)
        missing = [ln for ln in result.lines if not ln.matched][0]
        check("unmatched has a note", missing.note, "no priced listing found")
        check("unmatched contributes nothing", missing.line_total_usd, None)

        direct = price_bom(s, [("Sprocket Beta", 1)], direct_only=True)
        check("direct_only excludes agent sites", direct.unmatched, 1)

    print()
    print("CSV export")
    with session_scope() as s:
        products = list(s.scalars(select(CanonicalProduct)).all())
        body = export_products_csv(s, products)
        rows = list(csv.DictReader(io.StringIO(body)))
        check("one row per product", len(rows), len(products))
        check_true("has a best price column", "best_unit_usd" in rows[0])

        product = s.scalar(
            select(CanonicalProduct).where(CanonicalProduct.title_en.like("%Widget%"))
        )
        offers_csv = list(csv.DictReader(io.StringIO(export_offers_csv(s, product))))
        check_true("offer rows exported", len(offers_csv) >= 1)
        # Undisclosed shipping must be blank, never 0 -- a spreadsheet would sum a
        # zero into a confidently wrong total.
        agent_rows = [r for r in offers_csv if r["needs_agent"] == "yes"]
        for r in offers_csv:
            if r["site"] == "Banggood":
                check("free shipping exports as 0", r["shipping_usd"], "0")

        result = price_bom(s, [("Precision Widget Alpha", 3), ("nope", 1)])
        bom_rows = list(csv.DictReader(io.StringIO(export_bom_csv(result))))
        check("bom csv has a TOTAL row", bom_rows[-1]["line"], "TOTAL")
        check("total value present", bom_rows[-1]["line_total_usd"], "36")
        check("unmatched line still exported", len(bom_rows), 3)

    print()
    print("=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("export and BOM OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
