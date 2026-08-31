"""Reverse image search: find products from a photo.

This reuses the perceptual hashes the matcher already computes for every ingested
image, so it costs no extra storage and no extra crawling.

Two-tier lookup, because one tier alone is wrong in a different way each:

  1. **LSH bands** -- the 64-bit hash is split into four 16-bit bands, each with its
     own index. Two hashes differing by <=3 bits must agree exactly on at least one
     band (pigeonhole), so an indexed lookup finds re-encoded copies of the same
     photo instantly, however large the catalog. This is the dominant case: these
     marketplaces reuse one supplier photograph across every reseller.

  2. **Bounded scan** -- a crop, a rescale or a watermark pushes the distance past
     what banding can guarantee. If the bands come back thin we scan up to
     ``scan_limit`` hashes in Python. That is O(n), hence the cap: it is a
     best-effort widening, not a promise, and the result says which tier answered.

Distance is reported, not hidden. A caller that needs certainty can require
distance 0-2 ("same file"); 3-10 is "same photo, re-encoded or lightly edited";
beyond that it is a visual resemblance and should be labelled as such.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db.models import CanonicalProduct, Image, Offer
from .images import compute_phash, hamming

log = logging.getLogger(__name__)

# Distance bands, used for the confidence label shown to a user.
EXACT_MAX = 2
SAME_PHOTO_MAX = 10
SIMILAR_MAX = 18


@dataclass
class ImageHit:
    product: CanonicalProduct
    distance: int
    tier: str          # "band" | "scan"
    matched_image_id: int

    @property
    def confidence(self) -> str:
        if self.distance <= EXACT_MAX:
            return "identical image"
        if self.distance <= SAME_PHOTO_MAX:
            return "same photo"
        return "visually similar"

    @property
    def score(self) -> float:
        """1.0 at distance 0, decaying to 0.0 at the similarity cutoff."""
        return max(0.0, 1.0 - self.distance / (SIMILAR_MAX + 1))


class UndecodableImage(ValueError):
    pass


def phash_of_bytes(data: bytes) -> str:
    """Perceptual hash of an uploaded file. Raises UndecodableImage on junk input."""
    try:
        from PIL import Image as PILImage

        img = PILImage.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise UndecodableImage(f"could not decode image ({e})") from e

    ph = compute_phash(img)
    if not ph:
        raise UndecodableImage("could not hash image")
    return ph


def _band_candidates(session: Session, phash: str, cap: int = 2000) -> dict[int, str]:
    """Image id -> phash for rows sharing at least one 16-bit band."""
    clauses = [
        func.substr(Image.phash, i * 4 + 1, 4) == phash[i * 4:(i + 1) * 4]
        for i in range(4)
    ]
    rows = session.execute(
        select(Image.id, Image.phash)
        .where(Image.phash.is_not(None), or_(*clauses))
        .limit(cap)
    ).all()
    return {int(i): h for i, h in rows if h}


def _scan_candidates(session: Session, limit: int) -> dict[int, str]:
    rows = session.execute(
        select(Image.id, Image.phash)
        .where(Image.phash.is_not(None))
        .order_by(Image.id.desc())
        .limit(limit)
    ).all()
    return {int(i): h for i, h in rows if h}


def _products_for_images(session: Session, image_ids: list[int]) -> dict[int, int]:
    """Image id -> canonical product id, following either link on the image row."""
    if not image_ids:
        return {}
    rows = session.execute(
        select(Image.id, Image.canonical_id, Offer.canonical_id)
        .join(Offer, Offer.id == Image.offer_id, isouter=True)
        .where(Image.id.in_(image_ids))
    ).all()
    out: dict[int, int] = {}
    for image_id, direct, via_offer in rows:
        product_id = direct or via_offer
        if product_id:
            out[int(image_id)] = int(product_id)
    return out


def find_by_phash(
    session: Session,
    phash: str,
    *,
    limit: int = 30,
    max_distance: int = SIMILAR_MAX,
    scan_limit: int = 50_000,
    allow_scan: bool = True,
) -> list[ImageHit]:
    """Products whose imagery is within ``max_distance`` of ``phash``, best first."""
    candidates = _band_candidates(session, phash)
    tier = "band"

    # Thin band result means either a genuinely empty catalog or an edited image
    # that broke every band. Widen once, with a hard cap so this stays bounded.
    if allow_scan and len(candidates) < limit * 4:
        scanned = _scan_candidates(session, scan_limit)
        if len(scanned) > len(candidates):
            tier = "scan"
            scanned.update(candidates)
            candidates = scanned

    scored: list[tuple[int, int]] = []
    for image_id, other in candidates.items():
        d = hamming(phash, other)
        if d is not None and d <= max_distance:
            scored.append((image_id, d))
    if not scored:
        return []

    scored.sort(key=lambda t: t[1])
    product_by_image = _products_for_images(session, [i for i, _ in scored])

    # Best distance per product: one product owns many images, and returning the
    # same product several times is noise rather than a result.
    best: dict[int, tuple[int, int]] = {}
    for image_id, d in scored:
        product_id = product_by_image.get(image_id)
        if product_id is None:
            continue
        if product_id not in best or d < best[product_id][1]:
            best[product_id] = (image_id, d)

    ordered = sorted(best.items(), key=lambda kv: kv[1][1])[:limit]
    products = {
        p.id: p
        for p in session.scalars(
            select(CanonicalProduct).where(
                CanonicalProduct.id.in_([pid for pid, _ in ordered]),
                CanonicalProduct.is_active.is_(True),
            )
        ).all()
    }
    return [
        ImageHit(products[pid], d, tier, image_id)
        for pid, (image_id, d) in ordered
        if pid in products
    ]


def find_by_bytes(session: Session, data: bytes, **kwargs) -> list[ImageHit]:
    return find_by_phash(session, phash_of_bytes(data), **kwargs)


def find_by_url(session: Session, url: str, **kwargs) -> list[ImageHit]:
    from ..util.http import Fetcher

    fetcher = Fetcher(delay=0.2, retries=2, timeout=30)
    try:
        return find_by_bytes(session, fetcher.download(url), **kwargs)
    finally:
        fetcher.close()
