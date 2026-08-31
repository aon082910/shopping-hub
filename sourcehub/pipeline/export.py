"""CSV export and bulk sourcing (BOM mode).

Two things the UI could not do before:

* **Export** a comparison or a search result as CSV, because sourcing work ends in
  a spreadsheet and there was no way out of the browser.
* **BOM mode**: paste a parts list, get the best source per line plus a total. This
  is the actual job -- "what does this bill of materials cost, and from where" --
  and doing it by searching 40 times by hand is the thing worth automating.

BOM costing uses **landed cost** (unit x MOQ + shipping), never unit price. A line
that says $0.90 at MOQ 500 is a $450 commitment; totalling unit prices would produce
a number that looks great and cannot be ordered. Where a line needs fewer units than
the MOQ, the overage is reported rather than hidden.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import CanonicalProduct, Offer, Site
from ..db.search import search_product_ids

log = logging.getLogger(__name__)

PRODUCT_COLUMNS = [
    "product", "brand", "model", "mpn", "gtin", "category",
    "sites", "listings", "best_unit_usd", "highest_unit_usd", "spread_pct",
    "best_landed_usd", "min_moq", "best_site", "url", "last_seen",
]

OFFER_COLUMNS = [
    "product", "site", "title", "unit_price_usd", "currency", "native_price",
    "moq", "moq_unit", "shipping_usd", "landed_usd", "seller", "verified",
    "rating", "orders", "needs_agent", "url", "last_seen",
]


def _site_map(session: Session) -> dict[int, Site]:
    return {s.id: s for s in session.scalars(select(Site)).all()}


def export_products_csv(session: Session, products: Sequence[CanonicalProduct]) -> str:
    """One row per product -- the comparison summary."""
    sites = _site_map(session)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=PRODUCT_COLUMNS, lineterminator="\n")
    w.writeheader()
    for p in products:
        spread = None
        if p.best_price_usd and p.max_price_usd and p.max_price_usd > p.best_price_usd:
            spread = round(100 * (1 - p.best_price_usd / p.max_price_usd))
        best_site = sites.get(p.best_price_site_id) if p.best_price_site_id else None
        w.writerow({
            "product": p.title_en,
            "brand": p.brand or "",
            "model": p.model or "",
            "mpn": p.mpn or "",
            "gtin": p.gtin or "",
            "category": p.category.path if p.category else "",
            "sites": p.site_count,
            "listings": p.offer_count,
            "best_unit_usd": _money(p.best_price_usd),
            "highest_unit_usd": _money(p.max_price_usd),
            "spread_pct": spread if spread is not None else "",
            "best_landed_usd": _money(p.best_landed_usd),
            "min_moq": p.min_moq or "",
            "best_site": best_site.name if best_site else "",
            "url": f"/product/{p.slug}",
            "last_seen": p.last_seen.strftime("%Y-%m-%d") if p.last_seen else "",
        })
    return buf.getvalue()


def export_offers_csv(session: Session, product: CanonicalProduct) -> str:
    """One row per listing -- every site's terms for a single product."""
    sites = _site_map(session)
    offers = session.scalars(
        select(Offer)
        .where(Offer.canonical_id == product.id, Offer.is_active.is_(True))
        .order_by(Offer.price_usd.is_(None), Offer.price_usd.asc())
    ).all()

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=OFFER_COLUMNS, lineterminator="\n")
    w.writeheader()
    for o in offers:
        site = sites.get(o.site_id)
        w.writerow({
            "product": product.title_en,
            "site": site.name if site else "",
            "title": o.title_en or o.title_raw,
            "unit_price_usd": _money(o.price_usd),
            "currency": o.currency,
            "native_price": _money(o.price_min),
            "moq": o.moq,
            "moq_unit": o.moq_unit,
            # Empty, not 0: the site did not disclose it. Writing 0 would let a
            # spreadsheet sum undisclosed freight into a confident wrong total.
            "shipping_usd": _money(o.shipping_cost_usd),
            "landed_usd": _money(o.landed_cost_usd),
            "seller": o.seller_name or "",
            "verified": "yes" if o.is_verified_supplier else "",
            "rating": o.rating or "",
            "orders": o.orders_count or "",
            "needs_agent": "yes" if (site and site.needs_agent) else "",
            "url": o.url,
            "last_seen": o.last_seen.strftime("%Y-%m-%d") if o.last_seen else "",
        })
    return buf.getvalue()


def _money(v: Optional[float]) -> str:
    return "" if v is None else f"{v:.4f}".rstrip("0").rstrip(".")


# ------------------------------------------------------------------- BOM mode


@dataclass
class BomLine:
    query: str
    qty: int
    product: Optional[CanonicalProduct] = None
    offer: Optional[Offer] = None
    site_name: str = ""
    unit_usd: Optional[float] = None
    moq: int = 1
    order_qty: int = 0          # what you must actually buy (>= qty, >= moq)
    shipping_usd: Optional[float] = None
    line_total_usd: Optional[float] = None
    needs_agent: bool = False
    note: str = ""

    @property
    def matched(self) -> bool:
        return self.product is not None

    @property
    def overage(self) -> int:
        """Units bought beyond what the line asked for, forced by the MOQ."""
        return max(0, self.order_qty - self.qty)


@dataclass
class BomResult:
    lines: list[BomLine] = field(default_factory=list)
    total_usd: float = 0.0
    unmatched: int = 0
    agent_lines: int = 0
    undisclosed_shipping: int = 0

    @property
    def matched_lines(self) -> int:
        return sum(1 for line in self.lines if line.matched)


def parse_bom(text: str) -> list[tuple[str, int]]:
    """Parse a pasted parts list into (query, qty).

    Accepts the shapes people actually paste::

        esp32 devkit x10
        10 x esp32 devkit
        esp32 devkit, 10
        esp32 devkit          -> qty 1

    Quantity is optional and may lead or trail; a bare line means one unit.
    """
    import re

    out: list[tuple[str, int]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip().strip("-•*").strip()
        if not line or line.startswith("#"):
            continue

        qty = 1
        m = re.search(r"[\s,]+x?\s*(\d{1,6})\s*$", line, re.I)
        if m:
            qty, line = int(m.group(1)), line[: m.start()].strip().rstrip(",")
        else:
            m = re.match(r"^(\d{1,6})\s*x?\s+(.+)$", line, re.I)
            if m:
                qty, line = int(m.group(1)), m.group(2).strip()

        line = line.strip().strip(",")
        if line:
            out.append((line, max(1, qty)))
    return out


def price_bom(
    session: Session,
    entries: Sequence[tuple[str, int]],
    *,
    direct_only: bool = False,
    max_moq: Optional[int] = None,
) -> BomResult:
    """Cost a parts list: best landed source per line, plus a total."""
    result = BomResult()

    for query, qty in entries:
        line = BomLine(query=query, qty=qty)

        hits = search_product_ids(session, query, limit=25)
        best: Optional[tuple[float, Offer, CanonicalProduct, Site]] = None

        for product_id, _rank in hits:
            product = session.get(CanonicalProduct, product_id)
            if product is None or not product.is_active:
                continue
            for offer in session.scalars(
                select(Offer).where(
                    Offer.canonical_id == product.id,
                    Offer.is_active.is_(True),
                    Offer.price_usd.is_not(None),
                )
            ).all():
                site = session.get(Site, offer.site_id)
                if site is None:
                    continue
                if direct_only and site.needs_agent:
                    continue
                if max_moq is not None and (offer.moq or 1) > max_moq:
                    continue

                order_qty = max(qty, offer.moq or 1)
                total = offer.price_usd * order_qty + (offer.shipping_cost_usd or 0.0)
                if best is None or total < best[0]:
                    best = (total, offer, product, site)

        if best is None:
            line.note = "no priced listing found"
            result.unmatched += 1
        else:
            total, offer, product, site = best
            line.product, line.offer, line.site_name = product, offer, site.name
            line.unit_usd = offer.price_usd
            line.moq = offer.moq or 1
            line.order_qty = max(qty, line.moq)
            line.shipping_usd = offer.shipping_cost_usd
            line.line_total_usd = round(total, 2)
            line.needs_agent = bool(site.needs_agent)
            result.total_usd += total
            if line.needs_agent:
                result.agent_lines += 1
            if offer.shipping_cost_usd is None:
                result.undisclosed_shipping += 1
            if line.overage:
                line.note = f"MOQ {line.moq} forces {line.overage} extra"

        result.lines.append(line)

    result.total_usd = round(result.total_usd, 2)
    return result


BOM_COLUMNS = [
    "line", "qty_needed", "qty_ordered", "overage", "product", "site",
    "unit_usd", "moq", "shipping_usd", "line_total_usd", "needs_agent", "note", "url",
]


def export_bom_csv(result: BomResult) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=BOM_COLUMNS, lineterminator="\n")
    w.writeheader()
    for line in result.lines:
        w.writerow({
            "line": line.query,
            "qty_needed": line.qty,
            "qty_ordered": line.order_qty or "",
            "overage": line.overage or "",
            "product": line.product.title_en if line.product else "",
            "site": line.site_name,
            "unit_usd": _money(line.unit_usd),
            "moq": line.moq if line.matched else "",
            "shipping_usd": _money(line.shipping_usd),
            "line_total_usd": _money(line.line_total_usd),
            "needs_agent": "yes" if line.needs_agent else "",
            "note": line.note,
            "url": f"/product/{line.product.slug}" if line.product else "",
        })
    w.writerow({"line": "TOTAL", "line_total_usd": _money(result.total_usd)})
    return buf.getvalue()
