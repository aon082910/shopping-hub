"""Translation to English, with a persistent content-addressed cache.

Design points that matter in practice:

* **Everything is cached by SHA-256 of (provider, source text).** Re-crawling the
  same 50k listings daily would otherwise be the single largest cost in the system;
  with the cache, a repeat crawl translates only genuinely new strings.
* **Batching.** Product titles are short. Sending them one per API call is 40x more
  expensive in overhead than sending 40 in one call, so the Claude provider batches
  and asks for a JSON array back.
* **Nothing is destroyed.** Raw text is always retained on the offer; the English
  version is written to a parallel ``*_en`` column.
* **Graceful degradation.** With no provider configured the pipeline still runs --
  ``*_en`` simply falls back to the raw string, and English-language sites are
  unaffected either way.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db.models import Translation
from ..util.text import clean, detect_lang, hash_key, needs_translation

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You translate e-commerce product data from Chinese (and other \
languages) into natural English for a product comparison catalog.

Rules:
- Preserve every model number, part number, dimension, capacity, voltage and unit \
exactly as written. Never convert units.
- Translate product titles into concise, natural English of the kind a US retailer \
would use. Drop untranslatable SEO padding ("爆款", "厂家直销") rather than rendering \
it literally.
- Specification keys and values must stay terse. "电池容量" -> "Battery Capacity", \
not "The capacity of the battery".
- If a string is already English, return it unchanged.
- Return ONLY a JSON array of strings, same length and order as the input array. \
No commentary, no markdown fence."""


class Translator:
    """Provider-agnostic translator with DB-backed caching.

    Usage:
        tr = Translator(session)
        tr.translate_many(["无线蓝牙耳机", "电池容量"])
    """

    def __init__(self, session: Session, provider: str | None = None):
        self.session = session
        s = get_settings()
        self.provider = (provider or s.translate_provider or "none").lower()
        self.settings = s
        self._client = None
        self._mem: dict[str, str] = {}

    # ------------------------------------------------------------------ public

    def translate(self, text: str | None, *, src_lang: str | None = None) -> str | None:
        if not text:
            return text
        out = self.translate_many([text], src_lang=src_lang)
        return out[0] if out else text

    def translate_many(
        self, texts: Sequence[str | None], *, src_lang: str | None = None
    ) -> list[str | None]:
        """Translate a batch, preserving order. Passthrough for English input."""
        results: list[str | None] = list(texts)
        pending: dict[str, list[int]] = {}

        for i, raw in enumerate(texts):
            if not raw:
                continue
            text = clean(raw)
            if not text or not needs_translation(text):
                results[i] = text
                continue
            key = self._cache_key(text)
            if key in self._mem:
                results[i] = self._mem[key]
                continue
            cached = self._cache_get(key)
            if cached is not None:
                self._mem[key] = cached
                results[i] = cached
                continue
            pending.setdefault(text, []).append(i)

        if not pending or self.provider == "none":
            for text, idxs in pending.items():
                for i in idxs:
                    results[i] = text  # graceful fallback: keep the original
            return results

        unique = list(pending)
        batch_size = 40
        for start in range(0, len(unique), batch_size):
            chunk = unique[start : start + batch_size]
            try:
                translated = self._call_provider(chunk, src_lang)
            except Exception as e:
                log.warning("translation batch failed (%s): %s", self.provider, e)
                translated = chunk  # fall back to source text
            for src, dst in zip(chunk, translated):
                dst = clean(dst) or src
                key = self._cache_key(src)
                self._mem[key] = dst
                self._cache_put(key, src, dst)
                for i in pending[src]:
                    results[i] = dst

        return results

    def translate_offer(self, offer, spec_rows: Iterable = (),
                        variant_rows: Iterable = ()) -> None:
        """Fill in every ``*_en`` field on an Offer, its specs and its variants.

        One batch for the whole listing: variant names are short ("黑色 128GB") and
        sending them individually would multiply the per-call overhead by the SKU
        count, which on a Taobao listing can be dozens.
        """
        specs = list(spec_rows)
        variants = list(variant_rows)
        payload: list[str | None] = [
            offer.title_raw,
            offer.description_raw,
            offer.shipping_note_raw,
            offer.fees_note_raw,
        ]
        payload += [s.key_raw for s in specs]
        payload += [s.value_raw for s in specs]
        payload += [v.name_raw for v in variants]

        out = self.translate_many(payload)

        offer.title_en = out[0] or offer.title_raw
        offer.description_en = out[1]
        offer.shipping_note_en = out[2]
        offer.fees_note_en = out[3]
        offer.source_lang = detect_lang(offer.title_raw or "")

        n = len(specs)
        for idx, spec in enumerate(specs):
            spec.key_en = out[4 + idx]
            spec.value_en = out[4 + n + idx]
        base = 4 + 2 * n
        for idx, variant in enumerate(variants):
            variant.name_en = out[base + idx]

    # ------------------------------------------------------------------ cache

    def _cache_key(self, text: str) -> str:
        return hash_key(self.provider, text)

    def _cache_get(self, key: str) -> str | None:
        row = self.session.scalar(select(Translation).where(Translation.key_hash == key))
        return row.dst_text if row else None

    def _cache_put(self, key: str, src: str, dst: str) -> None:
        if self.session.scalar(select(Translation.id).where(Translation.key_hash == key)):
            return
        self.session.add(
            Translation(
                key_hash=key,
                src_lang=detect_lang(src),
                src_text=src[:20000],
                dst_text=dst[:20000],
                provider=self.provider,
            )
        )

    # --------------------------------------------------------------- providers

    def _call_provider(self, texts: list[str], src_lang: str | None) -> list[str]:
        if self.provider == "claude":
            return self._claude(texts)
        if self.provider == "deepl":
            return self._deepl(texts, src_lang)
        if self.provider == "google_free":
            return self._google_free(texts, src_lang)
        return texts

    def _claude(self, texts: list[str]) -> list[str]:
        if self._client is None:
            import anthropic

            if not self.settings.anthropic_api_key:
                raise RuntimeError("TRANSLATE_PROVIDER=claude but ANTHROPIC_API_KEY is unset")
            self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key)

        msg = self._client.messages.create(
            model=self.settings.anthropic_model,
            max_tokens=8000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(texts, ensure_ascii=False)}],
        )
        body = "".join(block.text for block in msg.content if block.type == "text")
        return _parse_json_array(body, len(texts), texts)

    def _deepl(self, texts: list[str], src_lang: str | None) -> list[str]:
        import httpx

        if not self.settings.deepl_api_key:
            raise RuntimeError("TRANSLATE_PROVIDER=deepl but DEEPL_API_KEY is unset")
        host = (
            "https://api-free.deepl.com"
            if self.settings.deepl_api_key.endswith(":fx")
            else "https://api.deepl.com"
        )
        resp = httpx.post(
            f"{host}/v2/translate",
            headers={"Authorization": f"DeepL-Auth-Key {self.settings.deepl_api_key}"},
            data=[("text", t) for t in texts] + [("target_lang", "EN-US")],
            timeout=60,
        )
        resp.raise_for_status()
        return [t["text"] for t in resp.json()["translations"]]

    def _google_free(self, texts: list[str], src_lang: str | None) -> list[str]:
        from deep_translator import GoogleTranslator

        tr = GoogleTranslator(source=src_lang or "auto", target="en")
        out = []
        for t in texts:
            try:
                out.append(tr.translate(t[:4900]) or t)
            except Exception as e:
                log.debug("google_free failed on one string: %s", e)
                out.append(t)
        return out


def _parse_json_array(body: str, expected: int, fallback: list[str]) -> list[str]:
    """Models occasionally wrap JSON in prose or a fence; recover the array."""
    body = body.strip()
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body)
    try:
        data = json.loads(body)
    except Exception:
        m = re.search(r"\[.*\]", body, re.S)
        if not m:
            return fallback
        try:
            data = json.loads(m.group(0))
        except Exception:
            return fallback

    if not isinstance(data, list):
        return fallback
    data = [str(x) if x is not None else "" for x in data]
    if len(data) != expected:
        log.warning("translator returned %s items, expected %s; padding", len(data), expected)
        data = (data + fallback[len(data) :])[:expected]
    return data
