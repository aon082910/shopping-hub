"""Image download, dedupe, thumbnailing and perceptual hashing.

The perceptual hash is not decoration -- it is the strongest cross-site matching
signal available. Chinese marketplaces overwhelmingly reuse the *same supplier
photography* across every reseller, so two listings whose primary images are within
a small Hamming distance are almost always the same physical product even when the
titles share no words (one being Chinese and one being English keyword soup).

Images are stored content-addressed by SHA-256, so the hundreds of resellers sharing
one photo cost one file on disk.
"""

from __future__ import annotations

import hashlib
import io
import logging
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.models import Image
from ..util.http import Fetcher

log = logging.getLogger(__name__)

THUMB_SIZE = (400, 400)
MIN_DIMENSION = 80  # below this it's a sprite/icon, not product photography
MAX_BYTES = 12 * 1024 * 1024


class ImageStore:
    def __init__(self, session: Session, fetcher: Fetcher | None = None):
        self.session = session
        self.root = get_settings().media_path
        self._fetcher = fetcher
        self._owns_fetcher = fetcher is None

    @property
    def fetcher(self) -> Fetcher:
        if self._fetcher is None:
            self._fetcher = Fetcher(delay=0.3, retries=2, timeout=30)
        return self._fetcher

    def close(self) -> None:
        if self._owns_fetcher and self._fetcher is not None:
            self._fetcher.close()
            self._fetcher = None

    # ------------------------------------------------------------------ public

    def ingest_many(
        self,
        urls: Iterable[str],
        *,
        offer_id: int | None = None,
        canonical_id: int | None = None,
        referer: str | None = None,
        limit: int = 8,
    ) -> list[Image]:
        out: list[Image] = []
        seen: set[str] = set()
        for pos, url in enumerate(urls):
            if len(out) >= limit:
                break
            url = _upgrade_url(url)
            if not url or url in seen:
                continue
            seen.add(url)
            img = self.ingest(url, offer_id=offer_id, canonical_id=canonical_id,
                             referer=referer, position=len(out))
            if img:
                out.append(img)
        return out

    def ingest(
        self,
        url: str,
        *,
        offer_id: int | None = None,
        canonical_id: int | None = None,
        referer: str | None = None,
        position: int = 0,
    ) -> Optional[Image]:
        # Same src_url already attached to this row? Nothing to do.
        existing = self.session.scalar(
            select(Image).where(
                Image.src_url == url,
                Image.offer_id == offer_id,
                Image.canonical_id == canonical_id,
            )
        )
        if existing:
            return existing

        try:
            data = self.fetcher.download(url, referer=referer)
        except Exception as e:
            log.debug("image download failed %s: %s", url, e)
            return None

        if not data or len(data) > MAX_BYTES:
            return None

        sha = hashlib.sha256(data).hexdigest()

        # Content already on disk from another listing -- reuse the file and its phash.
        twin = self.session.scalar(select(Image).where(Image.sha256 == sha).limit(1))
        if twin and twin.local_path and (self.root / twin.local_path).exists():
            row = Image(
                src_url=url, local_path=twin.local_path, thumb_path=twin.thumb_path,
                sha256=sha, phash=twin.phash, width=twin.width, height=twin.height,
                offer_id=offer_id, canonical_id=canonical_id, position=position,
            )
            self.session.add(row)
            return row

        try:
            from PIL import Image as PILImage

            pil = PILImage.open(io.BytesIO(data))
            pil.load()
        except Exception as e:
            log.debug("undecodable image %s: %s", url, e)
            return None

        if min(pil.size) < MIN_DIMENSION:
            return None

        phash = compute_phash(pil)
        rel_dir = Path(sha[:2]) / sha[2:4]
        (self.root / rel_dir).mkdir(parents=True, exist_ok=True)

        ext = (pil.format or "JPEG").lower()
        ext = "jpg" if ext in ("jpeg", "mpo") else ext
        rel_path = rel_dir / f"{sha}.{ext}"
        thumb_rel = rel_dir / f"{sha}_thumb.jpg"

        try:
            (self.root / rel_path).write_bytes(data)
            thumb = pil.convert("RGB")
            thumb.thumbnail(THUMB_SIZE)
            thumb.save(self.root / thumb_rel, "JPEG", quality=82, optimize=True)
        except Exception as e:
            log.debug("image write failed %s: %s", url, e)
            return None

        row = Image(
            src_url=url,
            local_path=str(rel_path).replace("\\", "/"),
            thumb_path=str(thumb_rel).replace("\\", "/"),
            sha256=sha,
            phash=phash,
            width=pil.width,
            height=pil.height,
            offer_id=offer_id,
            canonical_id=canonical_id,
            position=position,
        )
        self.session.add(row)
        return row


def compute_phash(pil_image) -> Optional[str]:
    try:
        import imagehash

        return str(imagehash.phash(pil_image.convert("RGB"), hash_size=8))
    except Exception as e:
        log.debug("phash failed: %s", e)
        return None


def hamming(a: str | None, b: str | None) -> Optional[int]:
    """Hamming distance between two hex phash strings. None if incomparable."""
    if not a or not b or len(a) != len(b):
        return None
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except ValueError:
        return None


def phash_similarity(a: str | None, b: str | None, max_distance: int = 10) -> float:
    """1.0 = identical image, 0.0 = beyond the match threshold."""
    d = hamming(a, b)
    if d is None:
        return 0.0
    if d > max_distance:
        return 0.0
    return 1.0 - (d / (max_distance + 1))


def _upgrade_url(url: str | None) -> Optional[str]:
    """Strip marketplace thumbnail suffixes to fetch the full-resolution original.

    Alibaba CDNs append transform suffixes like ``_220x220.jpg`` or ``_.webp``;
    removing them yields a much better image at no extra cost.
    """
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("http"):
        return None
    for suffix in ("_220x220", "_300x300", "_640x640", "_50x50", "_100x100", "_960x960"):
        url = url.replace(suffix, "")
    if url.endswith(".jpg_.webp"):
        url = url[: -len("_.webp")]
    if url.endswith("_.webp"):
        url = url[: -len("_.webp")]
    return url.split("?")[0] if "alicdn" in url else url
