"""Seller trust signals.

A price comparison that ignores who is selling is half a tool: the cheapest listing
on these marketplaces is frequently the one you should not buy. The signals that
predict trouble are already in the data being collected -- they just need reading
together rather than shown as isolated numbers.

Every signal is stated as a **reason**, never as a bare score. "4.1 stars over 12
reviews, seller is 3 months old" is actionable; "trust: 42/100" is not, and a number
with no explanation invites more confidence than the underlying data deserves.

Heuristic. It flags patterns worth a second look, not fraud.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# A price this far below the median of its own product is the classic bait pattern.
SUSPICIOUS_DISCOUNT = 0.45


@dataclass
class TrustAssessment:
    level: str                       # ok | caution | risk | unknown
    reasons: list = field(default_factory=list)
    positives: list = field(default_factory=list)

    @property
    def is_concerning(self) -> bool:
        return self.level in ("caution", "risk")


def assess_offer(offer: dict, peer_prices: Optional[list] = None) -> TrustAssessment:
    """Evaluate one offer view-model, optionally against its sibling listings."""
    reasons: list = []
    positives: list = []

    rating = offer.get("rating")
    reviews = offer.get("review_count") or offer.get("orders_count")
    years = offer.get("seller_years")
    price = offer.get("price_usd")

    if offer.get("verified"):
        positives.append("verified/audited supplier")
    if years and years >= 5:
        positives.append(f"{years} years on the platform")
    elif years is not None and years < 1:
        reasons.append("seller account is under a year old")

    if rating is not None:
        # Ratings here are heavily inflated; below ~4.3 out of 5 is genuinely poor.
        if rating <= 5 and rating < 4.3:
            reasons.append(f"low rating ({rating:.1f}/5)")
        elif rating <= 5 and rating >= 4.7:
            positives.append(f"rated {rating:.1f}/5")
        elif rating > 5 and rating < 97:
            # eBay-style percentage feedback.
            reasons.append(f"feedback {rating:.0f}%")

    if reviews is not None and reviews < 10:
        reasons.append(f"almost no sales history ({reviews})")
    elif reviews and reviews > 1000:
        positives.append(f"{reviews:,} sales/reviews")

    # Tracked separately from `reasons`: absence of evidence is not evidence of a
    # problem. On its own it means "cannot assess"; alongside real red flags it
    # compounds them, because an unknown seller with a suspicious price is worse
    # than a known one.
    no_reputation = rating is None and not reviews

    # Price outlier, judged against this product's own peers rather than an absolute
    # threshold -- what counts as implausibly cheap depends entirely on the item.
    if price and peer_prices:
        others = sorted(p for p in peer_prices if p and p > 0 and p != price)
        if len(others) >= 2:
            median = others[len(others) // 2]
            if median and price < median * (1 - SUSPICIOUS_DISCOUNT):
                pct = round(100 * (1 - price / median))
                reasons.append(f"{pct}% below the median of other listings for this product")

    if offer.get("site_needs_agent"):
        reasons.append("domestic-China listing: no buyer protection you can invoke")

    if no_reputation:
        reasons.append("no rating or sales history published")

    # "unknown" must mean *no evidence*, not *unremarkable evidence*: a seller with
    # a 4.6 rating and 800 sales is fine, and calling that unknown would send the
    # user to investigate a demonstrably ordinary listing. Equally, a missing
    # reputation on its own is not a red flag -- it is simply nothing to go on.
    substantive = [r for r in reasons if r != "no rating or sales history published"]
    if len(reasons) >= 3:
        level = "risk"
    elif substantive:
        level = "caution"
    elif no_reputation:
        level = "unknown"
    else:
        level = "ok"

    return TrustAssessment(level=level, reasons=reasons, positives=positives)


def assess_offers(offers: list) -> dict:
    """Assess every offer on a product, using the others as the price baseline."""
    prices = [o.get("price_usd") for o in offers if o.get("price_usd")]
    return {o.get("id"): assess_offer(o, prices) for o in offers}
