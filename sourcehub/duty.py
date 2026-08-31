"""Import duty estimation.

Duty is often the largest line item missing from a landed-cost comparison, so
omitting it makes cheap-looking sources look cheaper than they are. But rates are
not something this project can hardcode responsibly:

* they depend on the HTS classification of the specific good, not on a website
  category, and classification is genuinely hard;
* they change, sometimes sharply, and de-minimis treatment for China-origin goods
  in particular has moved more than once in recent years;
* they depend on origin, trade programme and the importer's own circumstances.

So this ships **switched off**, with a rate table *you* fill in from a source you
trust, stamped with the date you checked. Off, nothing is invented and the UI says
duty is not included. On, every figure is labelled an estimate and the as-of date is
shown, so a stale table is visible rather than silently wrong.

Configure in ``duty.yaml``. Nothing here is tax advice, and an estimate is not a
customs ruling.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .config import ROOT, config_path

log = logging.getLogger(__name__)

_CACHE: Optional["DutyTable"] = None


@dataclass
class DutyTable:
    enabled: bool = False
    as_of: str = ""
    source: str = ""
    default_rate: float = 0.0
    de_minimis_usd: Optional[float] = None
    by_category: dict = field(default_factory=dict)
    note: str = ""

    def rate_for(self, category_path: str | None) -> float:
        """Longest matching category prefix wins, else the default."""
        if not self.enabled:
            return 0.0
        path = (category_path or "").strip("/")
        best_rate, best_len = self.default_rate, -1
        for prefix, rate in self.by_category.items():
            p = str(prefix).strip("/")
            if path == p or path.startswith(p + "/"):
                if len(p) > best_len:
                    best_rate, best_len = float(rate), len(p)
        return float(best_rate)

    def estimate(self, goods_usd, category_path=None):
        """Return (rate, duty_usd). (None, None) when duty is not configured."""
        if not self.enabled or goods_usd is None:
            return None, None
        # De minimis, when configured, exempts shipments under a threshold. Left
        # unset by default precisely because its treatment has been in flux.
        if self.de_minimis_usd is not None and goods_usd < self.de_minimis_usd:
            return 0.0, 0.0
        rate = self.rate_for(category_path)
        return rate, round(goods_usd * rate, 2)

    @property
    def staleness_days(self) -> Optional[int]:
        if not self.as_of:
            return None
        try:
            checked = dt.date.fromisoformat(str(self.as_of))
        except ValueError:
            return None
        return (dt.date.today() - checked).days


def load_duty_table(path=None, refresh: bool = False) -> DutyTable:
    global _CACHE
    if _CACHE is not None and not refresh and path is None:
        return _CACHE
    p = Path(path) if path else config_path("duty.yaml")
    if not p.exists():
        table = DutyTable()
    else:
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        dm = data.get("de_minimis_usd")
        table = DutyTable(
            enabled=bool(data.get("enabled")),
            as_of=str(data.get("as_of", "")),
            source=str(data.get("source", "")),
            default_rate=float(data.get("default_rate", 0.0) or 0.0),
            de_minimis_usd=float(dm) if dm is not None else None,
            by_category=dict(data.get("by_category") or {}),
            note=str(data.get("note", "")),
        )
    if path is None:
        _CACHE = table
    return table
