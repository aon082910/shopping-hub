"""Data-driven provider driver: get Taobao/Tmall/1688 data over an API, no login.

Every practical no-login route to these three sites -- a commercial data API like
OTAPI, an unblocker service, a RapidAPI scraper, or a forwarding agent's own
endpoint -- has the same shape: **call an HTTP endpoint, get JSON back, normalize
it**. Only the URL, auth style and field names differ.

So none of that is hardcoded. Endpoints and field mappings live in
``providers.yaml`` and are resolved at runtime, which means adapting to whichever
vendor you sign up for is a YAML edit, not a code change. Use

    python -m sourcehub.cli provider-probe --preset otapi --keyword "usb hub"

to dump a real response and see exactly what the mapping extracted from it.

Two things worth knowing before you wire one up:

* **Most forwarding agents do item lookup only.** They resolve a URL or item id you
  already have; they do not offer keyword search over the catalog. That enriches a
  known product but cannot discover new ones, so an agent endpoint alone will not
  populate the catalog. Providers with ``search:`` defined can.
* **Be polite.** Agent sites are small businesses giving this away as a side effect
  of their checkout flow. The per-host rate limiter applies here as everywhere, and
  the defaults are deliberately slow.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Iterator, Optional

import yaml

from ..config import ROOT, config_path, get_settings
from ..util.money import parse_price
from ..util.text import clean
from .base import RawOffer, RawTier

log = logging.getLogger(__name__)

_PRESETS_CACHE: dict | None = None


class ProviderError(RuntimeError):
    pass


# --------------------------------------------------------------- path resolution


_INDEX_RE = re.compile(r"^(.*?)\[(\d*)\]$")


def dig(obj: Any, path: str | list[str] | None, default: Any = None) -> Any:
    """Resolve a dotted path against nested JSON.

    Supports ``a.b.c``, explicit indices ``a.b[0].c``, and a fan-out ``a.b[].c``
    which collects ``c`` from every element of ``b``. ``path`` may be a list of
    candidates, in which case the first one that resolves to something non-empty
    wins -- vendors rename fields between versions and this absorbs that.
    """
    if path is None:
        return default
    if isinstance(path, list):
        for candidate in path:
            value = dig(obj, candidate, None)
            if value not in (None, "", [], {}):
                return value
        return default

    return _dig_segments(obj, str(path).split("."), default)


def _dig_segments(cur: Any, segments: list[str], default: Any) -> Any:
    for i, segment in enumerate(segments):
        if cur is None:
            return default
        m = _INDEX_RE.match(segment)
        if m:
            key, idx = m.group(1), m.group(2)
            if key:
                cur = cur.get(key) if isinstance(cur, dict) else None
            if cur is None:
                return default
            if not isinstance(cur, list):
                cur = [cur]
            if idx == "":
                # Fan-out: resolve the remaining segments against every element.
                tail = segments[i + 1 :]
                if not tail:
                    return cur
                out: list = []
                for element in cur:
                    value = _dig_segments(element, tail, None)
                    if value is not None:
                        out.extend(value if isinstance(value, list) else [value])
                return out or default
            try:
                cur = cur[int(idx)]
            except (ValueError, IndexError):
                return default
        elif isinstance(cur, dict):
            cur = cur.get(segment)
        elif isinstance(cur, list) and segment.isdigit():
            try:
                cur = cur[int(segment)]
            except IndexError:
                return default
        else:
            return default
    return default if cur is None else cur


def as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"\d+(?:[.,]\d+)?", str(value).replace(",", "."))
    return float(m.group(0)) if m else None


def as_int(value: Any) -> Optional[int]:
    f = as_float(value)
    return int(f) if f is not None else None


# ------------------------------------------------------------------- presets


def load_presets(path: str | os.PathLike[str] | None = None) -> dict:
    global _PRESETS_CACHE
    if _PRESETS_CACHE is not None and path is None:
        return _PRESETS_CACHE
    p = Path(path) if path else config_path("providers.yaml")
    if not p.exists():
        data: dict = {"providers": {}}
    else:
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {"providers": {}}
    if path is None:
        _PRESETS_CACHE = data
    return data


def get_preset(name: str) -> dict:
    presets = load_presets().get("providers", {})
    if name not in presets:
        raise ProviderError(
            f"unknown provider preset {name!r}. Available: "
            f"{', '.join(sorted(presets)) or '(none - providers.yaml missing)'}"
        )
    return presets[name]


def _expand(value: Any, ctx: dict[str, Any]) -> Any:
    """Substitute {keyword}/{page}/{id}/{provider} and ${ENV_VAR} placeholders."""
    if isinstance(value, str):
        out = value
        for k, v in ctx.items():
            out = out.replace("{" + k + "}", str(v if v is not None else ""))
        for m in re.finditer(r"\$\{(\w+)\}", out):
            out = out.replace(m.group(0), os.environ.get(m.group(1), ""))
        return out
    if isinstance(value, dict):
        return {k: _expand(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, ctx) for v in value]
    return value


# -------------------------------------------------------------------- client


class ProviderClient:
    """Calls a configured provider and yields normalized :class:`RawOffer` records."""

    def __init__(self, preset_name: str, site_key: str, fetcher, *, base_url: str = ""):
        self.preset_name = preset_name
        self.preset = get_preset(preset_name)
        self.site_key = site_key
        self.fetcher = fetcher
        self.base_url = (base_url or self.preset.get("base_url", "")).rstrip("/")
        if not self.base_url:
            raise ProviderError(f"provider {preset_name!r} has no base_url")

        sites = self.preset.get("sites", {})
        if site_key not in sites:
            raise ProviderError(
                f"provider {preset_name!r} does not declare support for {site_key!r} "
                f"(declares: {', '.join(sorted(sites)) or 'none'})"
            )
        self.site_cfg = sites[site_key] or {}

    # -- capability flags --------------------------------------------------

    @property
    def can_search(self) -> bool:
        return bool(self.preset.get("search"))

    @property
    def can_detail(self) -> bool:
        return bool(self.preset.get("detail"))

    # -- request plumbing --------------------------------------------------

    def _auth(self, params: dict, headers: dict) -> None:
        auth = self.preset.get("auth") or {}
        mode = auth.get("mode", "none")
        value = auth.get("value") or os.environ.get(
            auth.get("value_env", "CN_PROVIDER_KEY"), ""
        )
        if mode == "none":
            return
        if not value:
            log.warning(
                "provider %s expects an API key in $%s but it is unset",
                self.preset_name, auth.get("value_env", "CN_PROVIDER_KEY"),
            )
        if mode == "query":
            params[auth.get("param", "key")] = value
        elif mode == "bearer":
            headers["Authorization"] = f"Bearer {value}"
        elif mode == "header":
            headers[auth.get("param", "X-API-Key")] = value
            for k, v in (auth.get("extra_headers") or {}).items():
                headers[k] = _expand(v, {})

    def call(self, section: str, ctx: dict[str, Any]) -> Any:
        spec = self.preset.get(section)
        if not spec:
            raise ProviderError(f"provider {self.preset_name!r} has no {section!r} section")

        ctx = {**ctx, "provider": self.site_cfg.get("provider_code", self.site_key),
               "site": self.site_key}
        path = _expand(spec.get("path", ""), ctx)
        params = {k: v for k, v in (_expand(spec.get("params", {}), ctx)).items()
                  if v not in (None, "")}
        headers = dict(_expand(spec.get("headers", {}), ctx))
        self._auth(params, headers)

        url = self.base_url + path
        method = str(spec.get("method", "GET")).upper()
        if method == "POST":
            resp = self.fetcher.post(url, json_body=_expand(spec.get("body"), ctx),
                                     headers=headers)
        else:
            resp = self.fetcher.get(url, params=params, headers=headers, expect_json=True)
        return resp.json()

    # -- normalization -----------------------------------------------------

    def search(self, keyword: str, page: int = 1) -> Iterator[RawOffer]:
        payload = self.call("search", {"keyword": keyword, "page": page})
        mapping = self.preset.get("map", {})
        items = as_list(dig(payload, mapping.get("items_path")))
        if not items:
            log.info("[%s/%s] provider returned no items for %r "
                     "(check map.items_path with `provider-probe`)",
                     self.preset_name, self.site_key, keyword)
        for item in items:
            offer = self.to_offer(item)
            if offer:
                yield offer

    def detail(self, item_id: str, url: str = "") -> Optional[RawOffer]:
        payload = self.call("detail", {"id": item_id, "url": url})
        mapping = self.preset.get("map", {})
        node = dig(payload, mapping.get("detail_path")) or payload
        if isinstance(node, list):
            node = node[0] if node else None
        if not node:
            return None
        offer = self.to_offer(node, detail=True)
        if offer:
            offer.detail_fetched = True
        return offer

    def to_offer(self, item: dict, detail: bool = False) -> Optional[RawOffer]:
        """Map one provider record onto RawOffer using the preset's field paths."""
        f = (self.preset.get("map", {}) or {}).get("item", {}) or {}
        if not isinstance(item, dict):
            return None

        item_id = dig(item, f.get("id"))
        title = clean(str(dig(item, f.get("title"), "") or ""))
        if item_id in (None, "") or not title:
            return None
        item_id = str(item_id)

        currency = str(dig(item, f.get("currency"), "") or "") or self.site_cfg.get(
            "currency", "CNY"
        )
        price = as_float(dig(item, f.get("price")))
        if price is None:
            # Some vendors only give a formatted string ("¥18.50" / "18.50 CNY").
            price, _, parsed_ccy = parse_price(str(dig(item, f.get("price_text"), "")), currency)
            currency = parsed_ccy or currency

        # Providers hand back protocol-relative ("//item.taobao.com/...") and
        # occasionally relative URLs. Normalize to absolute: this value is stored,
        # linked, and URL-encoded into forwarding-agent deep links, all of which
        # break on a bare "//" prefix.
        url = _https(str(dig(item, f.get("url"), "") or ""))
        if not url:
            template = self.site_cfg.get("item_url_template", "")
            url = template.replace("{id}", item_id) if template else ""
        if not url:
            return None

        offer = RawOffer(
            site_key=self.site_key,
            site_product_id=item_id,
            url=url,
            title=title,
            currency=(currency or "CNY").upper(),
            price_min=price,
            price_max=as_float(dig(item, f.get("price_max"))),
            moq=max(1, as_int(dig(item, f.get("moq"))) or 1),
            seller_name=clean(str(dig(item, f.get("seller"), "") or "")) or None,
            seller_url=str(dig(item, f.get("seller_url"), "") or "") or None,
            shipping_from=clean(str(dig(item, f.get("location"), "") or "")) or None,
            orders_count=as_int(dig(item, f.get("sales"))),
            rating=as_float(dig(item, f.get("rating"))),
            review_count=as_int(dig(item, f.get("reviews"))),
            description=clean(str(dig(item, f.get("description"), "") or "")) or None,
            category_path=clean(str(dig(item, f.get("category"), "") or "")) or None,
            raw={"source": f"provider:{self.preset_name}"},
        )

        for u in as_list(dig(item, f.get("images"))) or as_list(dig(item, f.get("image"))):
            u = _https(str(u.get("url") if isinstance(u, dict) else u))
            if u:
                offer.image_urls.append(u)

        specs = f.get("specs") or {}
        for node in as_list(dig(item, specs.get("list"))):
            if isinstance(node, dict):
                offer.add_spec(
                    str(dig(node, specs.get("key"), "") or ""),
                    str(dig(node, specs.get("value"), "") or ""),
                )

        tiers = f.get("tiers") or {}
        for node in as_list(dig(item, tiers.get("list"))):
            if not isinstance(node, dict):
                continue
            lo = as_int(dig(node, tiers.get("min_qty")))
            tier_price = as_float(dig(node, tiers.get("price")))
            if lo is None or tier_price is None:
                continue
            offer.tiers.append(
                RawTier(lo, as_int(dig(node, tiers.get("max_qty"))), tier_price, offer.currency)
            )
        if offer.tiers:
            offer.moq = min(t.min_qty for t in offer.tiers)
            offer.price_min = min(t.price for t in offer.tiers)
            offer.price_max = max(t.price for t in offer.tiers)

        # These three sites never ship internationally, whatever the provider says.
        offer.fees_note = (
            "Domestic-China listing sourced via an API provider. International "
            "shipping, consolidation and any service fee are charged by your "
            "forwarding agent, not by the marketplace."
        )
        return offer


def _https(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    return url if url.startswith("http") else ""


# ------------------------------------------------------------------- probing


def probe(preset_name: str, site_key: str, keyword: str, fetcher) -> dict:
    """Call a provider and report what the mapping actually extracted.

    Purpose-built for the moment you sign up somewhere new: it shows the raw JSON
    keys next to the mapped result, so a wrong ``items_path`` is obvious rather
    than showing up as a silently empty crawl.
    """
    client = ProviderClient(preset_name, site_key, fetcher)
    payload = client.call("search", {"keyword": keyword, "page": 1})

    mapping = client.preset.get("map", {})
    items = as_list(dig(payload, mapping.get("items_path")))

    report = {
        "preset": preset_name,
        "site": site_key,
        "top_level_keys": sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__,
        "items_path": mapping.get("items_path"),
        "items_found": len(items),
        "first_item_keys": sorted(items[0].keys()) if items and isinstance(items[0], dict) else [],
        "mapped": None,
        "candidate_item_paths": _candidate_array_paths(payload),
    }
    if items:
        offer = client.to_offer(items[0])
        if offer:
            report["mapped"] = {
                "id": offer.site_product_id,
                "title": offer.title[:80],
                "url": offer.url[:100],
                "price": offer.price_min,
                "currency": offer.currency,
                "moq": offer.moq,
                "images": len(offer.image_urls),
                "specs": len(offer.specs),
                "tiers": len(offer.tiers),
            }
    return report


def _candidate_array_paths(obj: Any, prefix: str = "", depth: int = 0) -> list[str]:
    """Find paths pointing at arrays of objects -- likely candidates for items_path."""
    out: list[str] = []
    if depth > 6 or not isinstance(obj, dict):
        return out
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, list) and value and isinstance(value[0], dict):
            out.append(f"{path}  ({len(value)} objects)")
        elif isinstance(value, dict):
            out.extend(_candidate_array_paths(value, path, depth + 1))
    return out[:15]
