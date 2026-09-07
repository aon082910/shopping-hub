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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .config import ROOT, config_path

log = logging.getLogger(__name__)

_CACHE: Optional["DutyTable"] = None

USITC_RATES_URL = "https://hts.usitc.gov/reststop/getRates"


@dataclass
class DutyTable:
    enabled: bool = False
    as_of: str = ""
    source: str = ""
    default_rate: float = 0.0
    de_minimis_usd: Optional[float] = None
    by_category: dict = field(default_factory=dict)
    # category path -> HTS number backing that rate, e.g. "8471.80.10". Only
    # entries sourced from a specific HTS classification (a CBP ruling, USITC
    # itself) can go here -- see check_against_usitc().
    hts_by_category: dict = field(default_factory=dict)
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
            hts_by_category=dict(data.get("hts_by_category") or {}),
            note=str(data.get("note", "")),
        )
    if path is None:
        _CACHE = table
    return table


def parse_usitc_rate(text: str | None) -> Optional[float]:
    """"Free" -> 0.0, "5.3%" -> 0.053, "" or a compound/specific rate this
    project can't express as a single ad valorem number -> None (not a
    mismatch -- just not comparable).
    """
    if text is None:
        return None
    t = text.strip()
    if not t:
        return None
    if t.lower() == "free":
        return 0.0
    m = re.match(r"^(\d+(?:\.\d+)?)\s*%$", t)
    return float(m.group(1)) / 100 if m else None


def fetch_usitc_general_rate(htsno: str, fetcher: Any = None) -> tuple[Optional[str], Optional[str]]:
    """The live "general" (Column 1) rate USITC's own HTS lookup shows for
    ``htsno`` right now, as the raw text CBP/USITC use ("Free", "5.3%", ...).

    Confirmed live (2026-09): hts.usitc.gov's search UI itself calls this
    same endpoint (found by inspecting its own network traffic, not from any
    published API docs -- there don't appear to be any). ``htsno`` alone
    doesn't filter server-side; it determines which full tariff *chapter*
    comes back (a couple thousand rows), and the exact line has to be found
    client-side by matching ``htsno`` -- slow and a bit absurd for looking up
    one line, but it is real, public, and needs no API key.

    Returns (raw_rate_text, error). Anything in ``error`` means don't trust
    ``raw_rate_text``.
    """
    if fetcher is None:
        from .util.http import Fetcher

        fetcher = Fetcher(retries=1, timeout=20.0)
    try:
        resp = fetcher.get(
            USITC_RATES_URL, params={"htsno": htsno, "keyword": "x"},
            headers={"Accept": "application/json"}, expect_json=True,
        )
        rows = resp.json()
    except Exception as e:
        return None, f"request failed: {e}"
    if not isinstance(rows, list):
        return None, "unexpected response shape (not a list)"
    # Confirmed live: which digit depth actually carries the rate varies by
    # heading -- some put it on the bare (no statistical-suffix) line (e.g.
    # 8205.59.55), others only on a specific 10-digit ".00" line beneath it
    # (e.g. 8471.80.10.00, where 8471.80.10 alone has a blank general field).
    # A prefix match, taking the first row under `htsno` that actually has a
    # non-blank rate, handles both shapes without needing to know which one
    # applies ahead of time.
    prefix = htsno.rstrip(".")
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_hts = str(row.get("htsno", ""))
        if (row_hts == prefix or row_hts.startswith(prefix + ".")) and str(row.get("general") or "").strip():
            return row.get("general"), None
    return None, f"HTS {htsno!r} not found in the returned chapter -- may have been reclassified"


def check_against_usitc(table: DutyTable, fetcher: Any = None) -> list[dict]:
    """Re-verify every by_category rate that has a known HTS line against
    USITC's own live rate table, and report drift.

    Only checks entries present in ``hts_by_category`` -- a rate sourced some
    other way (a broker, a forwarding agent's own reference page) has no HTS
    line to check against and is silently skipped, not flagged as an error.
    """
    results = []
    for category, htsno in table.hts_by_category.items():
        expected = table.by_category.get(category)
        raw, error = fetch_usitc_general_rate(str(htsno), fetcher=fetcher)
        live_rate = parse_usitc_rate(raw) if error is None else None
        drift = (
            error is None and live_rate is not None and expected is not None
            and abs(live_rate - float(expected)) > 1e-9
        )
        results.append({
            "category": category,
            "htsno": htsno,
            "expected_rate": expected,
            "live_rate_raw": raw,
            "live_rate": live_rate,
            "error": error,
            "drift": drift,
        })
    return results
