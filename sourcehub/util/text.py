"""Text normalization used by both matching and translation.

The single most valuable function here is :func:`normalize_title` -- listings across
these sites are stuffed with SEO noise ("2024 New Hot Sale Free Shipping Wholesale
Dropshipping ..."), and stripping it is what makes title similarity mean anything.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
KANA_RE = re.compile(r"[぀-ヿ]")
HANGUL_RE = re.compile(r"[가-힯]")
CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")

# Marketing filler that carries no product identity.
STOPWORDS = {
    "new", "hot", "sale", "free", "shipping", "dropshipping", "dropship", "wholesale",
    "high", "quality", "best", "top", "cheap", "price", "factory", "direct", "oem",
    "odm", "custom", "customized", "professional", "portable", "universal", "multi",
    "multifunctional", "fashion", "luxury", "premium", "original", "genuine", "brand",
    "for", "with", "and", "the", "a", "an", "of", "in", "to", "pcs", "pc", "piece",
    "pieces", "set", "sets", "lot", "pack", "gift", "gifts", "2023", "2024", "2025",
    "2026", "upgrade", "upgraded", "version", "style", "hight", "super", "mini",
}

_PUNCT_RE = re.compile(r"[^\w\s一-鿿.+#-]", re.UNICODE)
_WS_RE = re.compile(r"\s+")
# Letters, optional separator, 2+ digits, then an optional alphanumeric tail. The
# tail must be long enough for multi-segment part numbers ("ESP32-WROOM-32") and must
# end on an alphanumeric so a trailing hyphen isn't absorbed.
_MODEL_RE = re.compile(r"\b([A-Z]{1,6}[-\s]?\d{2,6}(?:[A-Z0-9-]{0,12}[A-Z0-9])?)\b")
_GTIN_RE = re.compile(r"\b(\d{8}|\d{12,14})\b")


def detect_lang(text: str) -> str:
    """Cheap script-based language guess. Enough to route translation."""
    if not text:
        return "und"
    # Kana first: Japanese titles are frequently katakana-only with no kanji at all,
    # and checking CJK first would misread those as English.
    if KANA_RE.search(text):
        return "ja"
    if CJK_RE.search(text):
        return "zh"
    if HANGUL_RE.search(text):
        return "ko"
    if CYRILLIC_RE.search(text):
        return "ru"
    return "en"


def needs_translation(text: str) -> bool:
    return detect_lang(text) not in ("en", "und")


_CJK_CHAR_RE = re.compile(r"([㐀-䶿一-鿿豈-﫿぀-ヿ가-힯])")


def segment_cjk(text: str | None) -> str:
    """Space-separate every CJK character so SQLite's FTS5 can tokenize it.

    FTS5's ``unicode61`` tokenizer treats CJK as ordinary letters and splits only on
    punctuation and spaces, so "2024新款TWS蓝牙耳机" is a *single* token: neither
    "TWS" nor "蓝牙耳机" will ever match it. Separating each CJK character turns the
    string into per-character tokens, which makes both the embedded Latin substring
    and any CJK substring findable. Applied to indexed text and query text alike.
    """
    if not text:
        return ""
    return _WS_RE.sub(" ", _CJK_CHAR_RE.sub(r" \1 ", text)).strip()


def clean(text: str | None) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("　", " ")
    return _WS_RE.sub(" ", text).strip()


def normalize_title(title: str | None) -> str:
    """Lowercase, de-punctuate, drop marketing stopwords. Used for match scoring."""
    t = clean(title).lower()
    t = _PUNCT_RE.sub(" ", t)
    tokens = [w for w in t.split() if w and w not in STOPWORDS and len(w) > 1]
    return " ".join(tokens)


def title_tokens(title: str | None) -> set[str]:
    return set(normalize_title(title).split())


def extract_model_codes(text: str | None) -> list[str]:
    """Pull likely model/part numbers ('ESP32-WROOM-32', 'XT60', 'CR2032').

    Separators are stripped entirely so the same part written three ways by three
    sellers -- "PD 100W", "PD-100W", "PD100W" -- yields one canonical code. Leaving
    them in makes identical codes look like a *conflict*, which actively suppresses
    correct matches.
    """
    if not text:
        return []
    out: list[str] = []
    for m in _MODEL_RE.finditer(clean(text).upper()):
        code = re.sub(r"[-\s_]", "", m.group(1))
        if code and code not in out:
            out.append(code)
    return out


def codes_conflict(a: set[str], b: set[str]) -> bool:
    """True only when both sides state model codes and none of them line up.

    Containment counts as agreement: "PD100W" vs "PD100" is one seller abbreviating,
    not a different part.
    """
    if not a or not b:
        return False
    if a & b:
        return False
    return not any(x.startswith(y) or y.startswith(x) for x in a for y in b)


def normalize_gtin(value: str | None) -> str | None:
    """Normalize any UPC-A/EAN-8/EAN-13 to a 14-digit GTIN, checksum-validated."""
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) not in (8, 12, 13, 14):
        return None
    g14 = digits.rjust(14, "0")
    body, check = g14[:-1], int(g14[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(body))
    if (10 - total % 10) % 10 != check:
        return None
    return g14


def find_gtin(text: str | None) -> str | None:
    for m in _GTIN_RE.finditer(text or ""):
        g = normalize_gtin(m.group(1))
        if g:
            return g
    return None


def normalize_spec_key(key: str) -> str:
    """Collapse the many ways sites spell the same attribute into one key."""
    k = clean(key).lower().rstrip(":：").strip()
    k = _PUNCT_RE.sub(" ", k)
    k = _WS_RE.sub(" ", k).strip()
    return SPEC_KEY_ALIASES.get(k, k)


SPEC_KEY_ALIASES = {
    "brand name": "brand",
    "brands": "brand",
    "model number": "model",
    "model no": "model",
    "item no": "model",
    "product name": "name",
    "place of origin": "origin",
    "country of origin": "origin",
    "material quality": "material",
    "product material": "material",
    "size": "dimensions",
    "product size": "dimensions",
    "item size": "dimensions",
    "package size": "package dimensions",
    "net weight": "weight",
    "item weight": "weight",
    "gross weight": "gross weight",
    "power supply": "power",
    "rated power": "power",
    "input voltage": "voltage",
    "working voltage": "voltage",
    "battery capacity": "battery",
    "battery type": "battery type",
    "screen size": "display size",
    "display screen": "display",
    "connection": "connectivity",
    "interface type": "interface",
    "bluetooth version": "bluetooth",
    "waterproof grade": "ip rating",
    "protection level": "ip rating",
    "certification": "certifications",
    "warranty period": "warranty",
    "color": "color",
    "colour": "color",
}


def hash_key(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update((p or "").encode("utf-8", "ignore"))
        h.update(b"\x1f")
    return h.hexdigest()


def truncate(text: str | None, n: int) -> str | None:
    if text is None:
        return None
    text = clean(text)
    return text if len(text) <= n else text[: n - 1] + "…"
