"""Break-even analysis: at what quantity does wholesale actually win?

This is the question a sourcer is really asking, and a price table alone cannot
answer it. A listing at $0.90 with MOQ 500 looks ten times cheaper than one at
$9.00 with MOQ 1, but you cannot buy one unit of it, and at small quantities the
cheap unit price is irrelevant -- you would pay $455 to avoid paying $9.

So the comparison has to be made **at a quantity**, using landed cost:

    landed(q) = unit_price * max(q, moq) + shipping

Because of the MOQ floor this is a step function, not a line: below the MOQ you pay
for units you do not want, so the effective per-unit cost falls as q rises until it
flattens at the MOQ. The crossover point is where the wholesale curve drops below
the retail one, and that is the number worth putting on the page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

# Quantities the table is evaluated at. Roughly log-spaced: the interesting
# behaviour is all in the first couple of orders of magnitude.
LADDER = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000)


@dataclass
class Candidate:
    """One purchasable option, flattened out of the ORM for the calculator."""

    label: str
    site: str
    unit_usd: float
    moq: int = 1
    shipping_usd: float = 0.0
    needs_agent: bool = False
    is_baseline: bool = False
    slug: Optional[str] = None
    url: Optional[str] = None

    def landed(self, qty: int) -> float:
        """Total outlay for ``qty`` units, including the MOQ overage."""
        return self.unit_usd * max(qty, self.moq) + self.shipping_usd

    def effective_unit(self, qty: int) -> float:
        """What each unit you actually *wanted* ends up costing."""
        return self.landed(qty) / max(1, qty)

    def overage(self, qty: int) -> int:
        return max(0, self.moq - qty)


@dataclass
class BreakEven:
    candidate: Candidate
    versus: Candidate
    quantity: Optional[int]      # None = never wins within the ladder
    saving_at_quantity: float = 0.0

    @property
    def wins(self) -> bool:
        return self.quantity is not None


def candidates_from_offers(offers: Sequence[dict]) -> list[Candidate]:
    """Build calculator inputs from the offer view-models used by the web layer."""
    out: list[Candidate] = []
    for o in offers:
        price = o.get("price_usd")
        if not price or price <= 0:
            continue          # unpriced listings cannot be compared
        out.append(
            Candidate(
                label=o.get("title") or o.get("site_name") or "",
                site=o.get("site_name") or "",
                unit_usd=float(price),
                moq=max(1, int(o.get("moq") or 1)),
                # Undisclosed shipping is treated as zero here, which makes the
                # estimate optimistic; the page says so rather than inventing a cost.
                shipping_usd=float(o.get("shipping_cost_usd") or 0.0),
                needs_agent=bool(o.get("site_needs_agent")),
                is_baseline=bool(o.get("site_is_baseline")),
                slug=o.get("site_key"),
                url=o.get("url"),
            )
        )
    return out


def cheapest_at(candidates: Sequence[Candidate], qty: int) -> Optional[Candidate]:
    priced = [c for c in candidates if c.unit_usd > 0]
    return min(priced, key=lambda c: c.landed(qty)) if priced else None


def break_even(candidate: Candidate, versus: Candidate,
               ladder: Sequence[int] = LADDER) -> BreakEven:
    """Smallest ladder quantity at which ``candidate`` costs less than ``versus``."""
    for qty in ladder:
        if candidate.landed(qty) < versus.landed(qty):
            saving = versus.landed(qty) - candidate.landed(qty)
            return BreakEven(candidate, versus, qty, round(saving, 2))
    return BreakEven(candidate, versus, None, 0.0)


def analyse(offers: Sequence[dict], ladder: Sequence[int] = LADDER) -> dict:
    """Full comparison: per-quantity winners, break-even points, baseline delta."""
    candidates = candidates_from_offers(offers)
    if not candidates:
        return {"rows": [], "break_evens": [], "baseline": None, "sourcing_best": None}

    sourcing = [c for c in candidates if not c.is_baseline]
    baselines = [c for c in candidates if c.is_baseline]

    rows = []
    for qty in ladder:
        winner = cheapest_at(candidates, qty)
        if winner is None:
            continue
        rows.append({
            "qty": qty,
            "site": winner.site,
            "landed": round(winner.landed(qty), 2),
            "effective_unit": round(winner.effective_unit(qty), 4),
            "overage": winner.overage(qty),
            "needs_agent": winner.needs_agent,
            "is_baseline": winner.is_baseline,
        })

    # Break-even is only interesting against the best small-quantity option.
    reference = cheapest_at(candidates, 1)
    break_evens = []
    if reference is not None:
        for c in sourcing:
            if c is reference or c.moq <= 1:
                continue
            be = break_even(c, reference, ladder)
            if be.wins:
                break_evens.append({
                    "site": c.site,
                    "moq": c.moq,
                    "unit_usd": c.unit_usd,
                    "quantity": be.quantity,
                    "saving": be.saving_at_quantity,
                    "versus": reference.site,
                })
    break_evens.sort(key=lambda b: b["quantity"])

    baseline = None
    best_sourcing = cheapest_at(sourcing, 1) if sourcing else None
    best_baseline = cheapest_at(baselines, 1) if baselines else None
    if best_baseline and best_sourcing:
        delta = best_baseline.landed(1) - best_sourcing.landed(1)
        baseline = {
            "site": best_baseline.site,
            "baseline_landed": round(best_baseline.landed(1), 2),
            "sourcing_site": best_sourcing.site,
            "sourcing_landed": round(best_sourcing.landed(1), 2),
            "saving": round(delta, 2),
            "pct": round(100 * delta / best_baseline.landed(1)) if best_baseline.landed(1) else 0,
            # The honest headline: importing is not always cheaper once you buy one.
            "importing_wins": delta > 0,
        }

    return {
        "rows": rows,
        "break_evens": break_evens,
        "baseline": baseline,
        "sourcing_best": best_sourcing.site if best_sourcing else None,
    }
