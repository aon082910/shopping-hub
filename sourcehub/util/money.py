"""Price parsing and currency conversion.

Listing prices come in every shape these sites can invent:
    "US $12.34"  "$1.20 - $3.40"  "¥12.50"  "12,34 €"  "US $2.10/Piece"
    "￥10.00-￥15.00"  "$0.98 / piece (Min. Order: 100 pieces)"
:func:`parse_price` handles all of the above and returns (min, max, currency).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

SYMBOL_TO_CCY = {
    "$": "USD", "US$": "USD", "usd": "USD",
    "¥": "CNY", "￥": "CNY", "rmb": "CNY", "cny": "CNY", "元": "CNY",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
    "₽": "RUB", "rub": "RUB",
    "₩": "KRW", "₹": "INR", "r$": "BRL", "brl": "BRL",
    "mad": "MAD", "dh": "MAD",  # gearbest.ma
    "a$": "AUD", "c$": "CAD", "hk$": "HKD", "nt$": "TWD", "s$": "SGD",
}

# Fallback rates: only used before the first successful FX refresh. 1 USD = N units.
FALLBACK_RATES = {
    "USD": 1.0, "CNY": 7.15, "EUR": 0.92, "GBP": 0.79, "JPY": 152.0, "KRW": 1360.0,
    "RUB": 92.0, "INR": 83.5, "BRL": 5.4, "AUD": 1.51, "CAD": 1.36, "HKD": 7.82,
    "TWD": 32.3, "SGD": 1.34, "MAD": 9.9, "MXN": 17.5,
}

# Lookup normalized to lowercase-without-spaces. Doing this at match time with a
# bare .lower() silently failed on "US $" -- the commonest prefix on these sites --
# because the table key is "US$" with no space, so the currency never resolved and
# quietly fell through to the site default. On a non-USD site that mislabels real
# dollar prices, so the normalization lives here rather than at each call site.
_SYMBOL_LOOKUP = {k.replace(" ", "").lower(): v for k, v in SYMBOL_TO_CCY.items()}


def symbol_to_currency(symbol: str | None) -> Optional[str]:
    if not symbol:
        return None
    return _SYMBOL_LOOKUP.get(symbol.replace(" ", "").replace(" ", "").lower())


_NUM = r"\d{1,3}(?:[,.]\d{3})*(?:[.,]\d{1,4})?|\d+(?:[.,]\d{1,4})?"
_SYM = r"US\s?\$|R\$|A\$|C\$|HK\$|NT\$|S\$|\$|¥|￥|€|£|₽|₩|₹"
_CODE = r"USD|CNY|RMB|EUR|GBP|JPY|KRW|RUB|INR|BRL|AUD|CAD|HKD|TWD|SGD|MAD|Dh"
# Currency can lead ("$12.34", "US $2.10") or trail ("12,34 €", "129 MAD", "45 Dh").
# GearBest.ma in particular prices as a trailing "Dh"/"MAD", so both must parse.
_PRICE_RE = re.compile(
    r"(?P<sym>" + _SYM + r")?"
    r"\s*(?P<num>" + _NUM + r")"
    r"\s*(?P<post_sym>" + _SYM + r")?"
    r"\s*(?P<code>" + _CODE + r")?",
    re.IGNORECASE,
)
_QTY_RE = re.compile(
    r"(?P<num>\d[\d,\s]*)\s*"
    r"(?P<unit>pieces?|pcs?|sets?|units?|bags?|boxes?|cartons?|pairs?|meters?|m\b|"
    r"kg|kilograms?|tons?|rolls?|packs?|件|个|只|套|箱|袋)",
    re.IGNORECASE,
)
_MOQ_RE = re.compile(
    r"(?:min(?:imum)?\.?\s*order|moq|min\.?\s*order\s*(?:qty|quantity)?|起批|起订)"
    r"\s*[::]?\s*(?P<num>\d[\d,\s]*)",
    re.IGNORECASE,
)


def _to_float(raw: str) -> Optional[float]:
    """Handle both 1,234.56 and 1.234,56 conventions."""
    s = raw.strip().replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        # A single comma with exactly 3 trailing digits is a thousands separator.
        head, _, tail = s.rpartition(",")
        s = s.replace(",", "") if len(tail) == 3 and head else s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_price(
    text: str | None, default_currency: str = "USD"
) -> tuple[Optional[float], Optional[float], str]:
    """Return (min_price, max_price, currency). max is None when not a range."""
    if not text:
        return None, None, default_currency

    values: list[float] = []
    currency = None
    for m in _PRICE_RE.finditer(str(text)):
        val = _to_float(m.group("num"))
        if val is None:
            continue
        sym = m.group("sym") or m.group("post_sym")
        code = m.group("code")
        if currency is None:
            if code:
                currency = symbol_to_currency(code) or code.upper()
            elif sym:
                currency = symbol_to_currency(sym)
        # A bare number with no symbol anywhere is probably not a price fragment
        # unless we already locked a currency from an earlier match.
        if sym or code or currency:
            values.append(val)

    if not values:
        return None, None, default_currency

    currency = currency or default_currency
    lo, hi = min(values), max(values)
    return lo, (hi if hi > lo else None), currency


def parse_moq(text: str | None) -> tuple[int, str]:
    """Return (min_order_quantity, unit). Defaults to (1, 'piece')."""
    if not text:
        return 1, "piece"
    s = str(text)
    m = _MOQ_RE.search(s)
    q = _QTY_RE.search(s)
    unit = _normalize_unit(q.group("unit")) if q else "piece"

    # An explicit "Min. Order: N" wins; otherwise take the first quantity phrase.
    qty_src = m.group("num") if m else (q.group("num") if q else "")
    num = re.sub(r"\D", "", qty_src)
    try:
        n = int(num)
    except (ValueError, TypeError):
        n = 1
    return max(1, n), unit


_UNIT_MAP = {
    "pc": "piece", "pcs": "piece", "piece": "piece", "pieces": "piece",
    "件": "piece", "个": "piece", "只": "piece",
    "set": "set", "sets": "set", "套": "set",
    "unit": "unit", "units": "unit", "pair": "pair", "pairs": "pair",
    "bag": "bag", "bags": "bag", "袋": "bag",
    "box": "box", "boxes": "box", "箱": "carton",
    "carton": "carton", "cartons": "carton",
    "m": "meter", "meter": "meter", "meters": "meter",
    "kg": "kg", "kilogram": "kg", "kilograms": "kg",
    "ton": "ton", "tons": "ton", "roll": "roll", "rolls": "roll",
    "pack": "pack", "packs": "pack",
}


def _normalize_unit(u: str) -> str:
    return _UNIT_MAP.get(u.strip().lower(), u.strip().lower())


def parse_tiers(text: str | None, currency: str = "USD") -> list[dict]:
    """Parse '1-99 pieces $2.30 / 100-999 $2.10 / >=1000 $1.90' style ladders."""
    if not text:
        return []
    tiers: list[dict] = []
    pattern = re.compile(
        r"(?P<lo>\d[\d,]*)\s*(?:-|~|–|to)?\s*(?P<hi>\d[\d,]*)?\s*"
        r"(?:\+|and up|以上)?[^\d$¥€]{0,24}?"
        r"(?P<price>(?:US\s?\$|\$|¥|￥|€)\s*" + _NUM + ")",
        re.IGNORECASE,
    )
    for m in pattern.finditer(str(text)):
        lo = _to_float(m.group("lo"))
        hi = _to_float(m.group("hi")) if m.group("hi") else None
        pmin, _, ccy = parse_price(m.group("price"), currency)
        if lo is None or pmin is None:
            continue
        tiers.append(
            {"min_qty": int(lo), "max_qty": int(hi) if hi else None,
             "price": pmin, "currency": ccy}
        )
    return tiers


# ------------------------------------------------------------------ FX conversion


class FxConverter:
    """Loads rates once per crawl run; falls back to hardcoded table if DB is empty."""

    def __init__(self, rates: dict[str, float] | None = None):
        self.rates = dict(FALLBACK_RATES)
        if rates:
            self.rates.update(rates)

    @classmethod
    def from_db(cls, session: Session) -> "FxConverter":
        from ..db.models import FxRate

        rows = session.scalars(select(FxRate)).all()
        return cls({r.currency: r.per_usd for r in rows if r.per_usd})

    def to_usd(self, amount: float | None, currency: str) -> Optional[float]:
        if amount is None:
            return None
        rate = self.rates.get((currency or "USD").upper())
        if not rate:
            return None
        return round(amount / rate, 4)

    def rate_for(self, currency: str) -> Optional[float]:
        return self.rates.get((currency or "USD").upper())


def refresh_fx_rates(session: Session, timeout: float = 20.0) -> int:
    """Pull fresh USD-base rates. Returns the number of currencies stored."""
    import httpx

    from ..db.models import FxRate

    symbols = ",".join(k for k in FALLBACK_RATES if k != "USD")
    fetched: dict[str, float] = {}
    for url in (
        f"https://api.frankfurter.app/latest?from=USD&to={symbols}",
        f"https://open.er-api.com/v6/latest/USD",
    ):
        try:
            r = httpx.get(url, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            fetched = data.get("rates") or data.get("conversion_rates") or {}
            if fetched:
                break
        except Exception:
            continue

    if not fetched:
        return 0

    existing = {r.currency: r for r in session.scalars(select(FxRate)).all()}
    now = dt.datetime.now(dt.timezone.utc)
    count = 0
    for ccy, rate in fetched.items():
        ccy = ccy.upper()
        if ccy not in FALLBACK_RATES:
            continue
        row = existing.get(ccy)
        if row is None:
            row = FxRate(currency=ccy, per_usd=float(rate), updated_at=now)
            session.add(row)
            # Track it: `existing` is a pre-loop snapshot, so without this the USD
            # backfill below cannot tell that this iteration already inserted USD,
            # and the second INSERT trips the unique constraint. Providers differ on
            # whether the base currency appears in their own rate table, so this only
            # ever fired against one of the two -- invisible without live network.
            existing[ccy] = row
        else:
            row.per_usd, row.updated_at = float(rate), now
        count += 1

    if "USD" not in existing:
        session.add(FxRate(currency="USD", per_usd=1.0, updated_at=now))
        count += 1
    return count
