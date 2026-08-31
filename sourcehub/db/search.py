"""Full-text search over the canonical catalog.

SQLite gets a real FTS5 index (BM25-ranked). Anything else falls back to LIKE so
the app still works if you point SOURCEHUB_DB_URL at Postgres.

The index is maintained explicitly at ingest time rather than by triggers -- we want
to index the *English* title plus the raw title plus brand/model/specs as one blob,
which is a computed document, not a straight column mirror.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..util.text import segment_cjk

FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS product_fts USING fts5(
    title_en,
    title_raw,
    brand,
    model,
    identifiers,      -- gtin, mpn, site product ids
    specs,
    category_path,
    product_id UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
"""


def is_sqlite(bind: Any) -> bool:
    return bind.dialect.name == "sqlite"


# Expression indexes backing the LSH band lookup in pipeline/matching.py. Without
# these, every candidate-generation call table-scans images.
PHASH_BAND_DDL = [
    f"CREATE INDEX IF NOT EXISTS ix_img_band{i} "
    f"ON images (substr(phash, {i * 4 + 1}, 4))"
    for i in range(4)
]


def ensure_fts(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        conn.execute(text(FTS_DDL))
        for ddl in PHASH_BAND_DDL:
            conn.execute(text(ddl))


def index_product(session: Session, product) -> None:
    """Insert/refresh one canonical product in the FTS index."""
    if not is_sqlite(session.get_bind()):
        return

    spec_blob = " ".join(
        f"{k} {v}" for k, v in (product.specs or {}).items() if v is not None
    )[:8000]
    ident = " ".join(
        filter(None, [product.gtin, product.mpn, product.model, product.brand])
    )
    # CJK is segmented per character so FTS5 can tokenize it -- see segment_cjk.
    raw_titles = segment_cjk(
        " ".join(filter(None, [o.title_raw for o in product.offers]))
    )[:8000]
    cat_path = product.category.path if product.category else ""

    session.execute(
        text("DELETE FROM product_fts WHERE product_id = :pid"), {"pid": product.id}
    )
    session.execute(
        text(
            "INSERT INTO product_fts "
            "(title_en, title_raw, brand, model, identifiers, specs, category_path, product_id) "
            "VALUES (:t, :tr, :b, :m, :i, :s, :c, :pid)"
        ),
        {
            "t": product.title_en or "",
            "tr": raw_titles,
            "b": product.brand or "",
            "m": product.model or "",
            "i": ident,
            "s": spec_blob,
            "c": cat_path,
            "pid": product.id,
        },
    )


def drop_product(session: Session, product_id: int) -> None:
    if not is_sqlite(session.get_bind()):
        return
    session.execute(
        text("DELETE FROM product_fts WHERE product_id = :pid"), {"pid": product_id}
    )


_FTS_SPECIAL = re.compile(r'[",():*^\-]')
# CJK queries are handled by per-character segmentation, not synonyms.
CJK_QUERY = re.compile(r"[㐀-鿿぀-ヿ가-힯]")


def build_match_query(q: str, expand: bool = True) -> str:
    """Turn free user input into a safe FTS5 MATCH expression.

    Each term becomes a prefix query so "earbud" hits "earbuds"; quoting each token
    neutralizes FTS5 operators a user might type by accident. CJK input is segmented
    the same way the index is, so a Chinese query matches a Chinese listing.

    With ``expand``, synonyms are ORed within a concept and ANDed across concepts --
    "wireless earbuds" becomes (wireless OR bluetooth) AND (earbud OR earphone OR
    ...). Expansion never loosens the query: an unrecognised word stays a required
    term of its own.
    """
    if expand and not CJK_QUERY.search(q or ""):
        from .synonyms import expand_terms

        concepts = expand_terms(q)
        if concepts:
            clauses = []
            for group in concepts:
                alts = []
                for term in group:
                    tokens = [t for t in _FTS_SPECIAL.sub(" ", term).split() if t]
                    if not tokens:
                        continue
                    # A multi-word synonym has to match as a phrase, not as loose
                    # words, or "power bank" would match any page mentioning a bank.
                    alts.append(
                        '"' + " ".join(tokens) + '"' if len(tokens) > 1
                        else f'"{tokens[0]}"*'
                    )
                if alts:
                    clauses.append("(" + " OR ".join(alts) + ")")
            if clauses:
                return " AND ".join(clauses)

    terms = [t for t in _FTS_SPECIAL.sub(" ", segment_cjk(q)).split() if t]
    if not terms:
        return ""
    return " AND ".join(f'"{t}"*' for t in terms)


def search_product_ids(
    session: Session, q: str, limit: int = 60, offset: int = 0
) -> list[tuple[int, float]]:
    """Return [(product_id, relevance)] best-first. Relevance is higher = better."""
    q = (q or "").strip()
    if not q:
        return []

    if is_sqlite(session.get_bind()):
        match = build_match_query(q)
        if not match:
            return []
        rows = session.execute(
            text(
                """
                SELECT product_id,
                       bm25(product_fts, 10.0, 4.0, 3.0, 3.0, 8.0, 1.0, 0.5) AS rank
                FROM product_fts
                WHERE product_fts MATCH :m
                ORDER BY rank
                LIMIT :lim OFFSET :off
                """
            ),
            {"m": match, "lim": limit, "off": offset},
        ).all()
        # bm25() returns negative numbers, lower = better. Flip for a sane API.
        out = [(int(r[0]), -float(r[1])) for r in rows]
        if out:
            return out
        # CJK and other non-space-delimited scripts tokenize poorly; fall through.

    like = f"%{q}%"
    rows = session.execute(
        text(
            """
            SELECT DISTINCT p.id
            FROM canonical_products p
            LEFT JOIN offers o ON o.canonical_id = p.id
            WHERE p.title_en LIKE :like
               OR p.brand LIKE :like
               OR p.model LIKE :like
               OR p.mpn LIKE :like
               OR o.title_raw LIKE :like
            LIMIT :lim OFFSET :off
            """
        ),
        {"like": like, "lim": limit, "off": offset},
    ).all()
    return [(int(r[0]), 0.0) for r in rows]


def suggest(session: Session, prefix: str, limit: int = 8) -> list[str]:
    """Autocomplete from indexed titles."""
    prefix = (prefix or "").strip()
    if len(prefix) < 2 or not is_sqlite(session.get_bind()):
        return []
    match = build_match_query(prefix)
    if not match:
        return []
    rows = session.execute(
        text(
            "SELECT title_en FROM product_fts WHERE product_fts MATCH :m "
            "ORDER BY bm25(product_fts) LIMIT :lim"
        ),
        {"m": match, "lim": limit},
    ).all()
    seen, out = set(), []
    for (title,) in rows:
        t = (title or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out
