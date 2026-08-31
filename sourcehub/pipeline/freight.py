"""Freight estimation for forwarding-agent orders.

The agent estimate was a flat $12 regardless of what you were shipping, which is
wrong in both directions: a phone case does not cost $12 to send, and a 5kg toolkit
costs far more. International parcels are priced on **volumetric weight** -- the
greater of actual weight and (L x W x H / divisor) -- so a light bulky item bills as
if it were heavy, which is exactly the case a flat fee gets most wrong.

Nothing here is a quote. Real cost depends on the courier, the lane, fuel surcharges
and the agent's own contract rates, none of which are knowable from a listing page.
What it does give you is an estimate that *moves with the item* instead of a
constant, and it is labelled an estimate everywhere it surfaces.

Rates are editable in ``freight.yaml``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .config_paths import ROOT, config_path

log = logging.getLogger(__name__)

_CACHE: Optional["FreightTable"] = None

# Volumetric divisor in cm^3 per kg. 5000 is the common air-express convention.
DEFAULT_DIVISOR = 5000

# Rough per-category shipping weights (kg) for when a listing says nothing at all.
# Deliberately coarse: a guess that is clearly a guess beats a precise-looking lie.
CATEGORY_WEIGHTS = {
    "electronics/earbuds-headphones": 0.15,
    "electronics": 0.4,
    "computers/usb-hubs-docks": 0.2,
    "computers": 0.6,
    "components": 0.2,
    "lighting": 0.5,
    "tools": 1.5,
    "home": 1.0,
    "auto": 1.2,
    "apparel": 0.4,
    "beauty": 0.3,
    "sports": 1.0,
    "toys": 0.6,
    "packaging": 0.8,
    "security": 0.5,
    "health": 0.5,
}
FALLBACK_WEIGHT = 0.5


@dataclass
class FreightTable:
    enabled: bool = True
    as_of: str = ""
    divisor: int = DEFAULT_DIVISOR
    base_fee_usd: float = 4.0
    per_kg_usd: float = 9.0
    min_usd: float = 6.0
    category_weights: dict = field(default_factory=lambda: dict(CATEGORY_WEIGHTS))
    note: str = ""

    def weight_for(self, category_path: Optional[str]) -> float:
        """Longest matching category prefix wins."""
        path = (category_path or "").strip("/")
        best, best_len = FALLBACK_WEIGHT, -1
        for prefix, kg in self.category_weights.items():
            p = str(prefix).strip("/")
            if path == p or path.startswith(p + "/"):
                if len(p) > best_len:
                    best, best_len = float(kg), len(p)
        return best

    def estimate(self, qty=1, weight_kg=None, dims_cm=None, category_path=None) -> dict:
        """Estimate international freight for ``qty`` units."""
        guessed = weight_kg is None
        actual = (weight_kg if weight_kg else self.weight_for(category_path)) * max(1, qty)

        volumetric = 0.0
        if dims_cm and all(d and d > 0 for d in dims_cm):
            volumetric = (dims_cm[0] * dims_cm[1] * dims_cm[2]) / self.divisor * max(1, qty)

        # Couriers bill the greater of the two.
        chargeable = max(actual, volumetric)
        cost = max(self.base_fee_usd + chargeable * self.per_kg_usd, self.min_usd)

        return {
            "actual_kg": round(actual, 3),
            "volumetric_kg": round(volumetric, 3) if volumetric else None,
            "chargeable_kg": round(chargeable, 3),
            "usd": round(cost, 2),
            "per_unit_usd": round(cost / max(1, qty), 4),
            "guessed_weight": guessed,
            "is_estimate": True,
        }


_WEIGHT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(kg|kgs|kilograms?|g|grams?|lbs?|pounds?|oz|ounces?)\b", re.I
)
_DIMS_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[x*]\s*(\d+(?:\.\d+)?)\s*[x*]\s*(\d+(?:\.\d+)?)\s*"
    r"(cm|mm|m|in|inch|inches)?",
    re.I,
)


def parse_weight_kg(text) -> Optional[float]:
    """Pull a shipping weight out of a spec value, normalized to kg."""
    if not text:
        return None
    m = _WEIGHT_RE.search(str(text))
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2).lower()
    if unit.startswith(("kg", "kilo")):
        return value
    if unit.startswith(("g", "gram")):
        return value / 1000.0
    if unit.startswith(("lb", "pound")):
        return value * 0.45359237
    if unit.startswith(("oz", "ounce")):
        return value * 0.0283495
    return None


def parse_dims_cm(text):
    """Pull LxWxH out of a spec value, normalized to centimetres."""
    if not text:
        return None
    m = _DIMS_RE.search(str(text))
    if not m:
        return None
    dims = [float(m.group(i)) for i in (1, 2, 3)]
    unit = (m.group(4) or "cm").lower()
    factor = {"mm": 0.1, "cm": 1.0, "m": 100.0,
              "in": 2.54, "inch": 2.54, "inches": 2.54}.get(unit, 1.0)
    scaled = [d * factor for d in dims]
    # Guard against nonsense parsed out of a model number like "3x4x5".
    if any(d <= 0 or d > 300 for d in scaled):
        return None
    return (scaled[0], scaled[1], scaled[2])


def from_specs(specs: dict):
    """Best weight and dimensions available from a merged spec sheet."""
    weight = dims = None
    for key, value in (specs or {}).items():
        low = str(key).lower()
        if weight is None and "weight" in low:
            weight = parse_weight_kg(value)
        if dims is None and any(w in low for w in ("dimension", "size", "package")):
            dims = parse_dims_cm(value)
    return weight, dims


def load_freight_table(path=None, refresh: bool = False) -> FreightTable:
    global _CACHE
    if _CACHE is not None and not refresh and path is None:
        return _CACHE
    p = Path(path) if path else config_path("freight.yaml")
    if not p.exists():
        table = FreightTable()
    else:
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        table = FreightTable(
            enabled=bool(data.get("enabled", True)),
            as_of=str(data.get("as_of", "")),
            divisor=int(data.get("volumetric_divisor", DEFAULT_DIVISOR)),
            base_fee_usd=float(data.get("base_fee_usd", 4.0)),
            per_kg_usd=float(data.get("per_kg_usd", 9.0)),
            min_usd=float(data.get("min_usd", 6.0)),
            category_weights={**CATEGORY_WEIGHTS, **(data.get("category_weights") or {})},
            note=str(data.get("note", "")),
        )
    if path is None:
        _CACHE = table
    return table
