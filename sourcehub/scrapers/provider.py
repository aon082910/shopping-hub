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

Three things worth knowing before you wire one up:

* **Most forwarding agents do item lookup only.** They resolve a URL or item id you
  already have; they do not offer keyword search over the catalog. That enriches a
  known product but cannot discover new ones, so an agent endpoint alone will not
  populate the catalog. Providers with ``search:`` defined can.
* **Be polite.** Agent sites are small businesses giving this away as a side effect
  of their checkout flow. The per-host rate limiter applies here as everywhere, and
  the defaults are deliberately slow.
* **Some agents gate real results behind a login.** Logged out, an agent's own
  "buy this link" tool can return a teaser rather than what a signed-in customer
  would actually see. A preset can set ``via_agent_login: <agent key>`` to route
  its calls through a real browser bound to that agent's own persisted session
  (``python -m sourcehub.cli agent-login --agent <key>``, once) instead of the
  plain anonymous ``Fetcher`` -- GET only, and it degrades to the same anonymous
  access when no login has been done yet, rather than failing.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import urlencode

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

        # Scoped to search/detail only: a `resolve` step (turning a pasted URL
        # into whatever opaque id the agent's real lookup wants) is typically a
        # public, anonymous call even on an agent that gates the lookup itself --
        # confirmed live on USFans, whose short-link parser works logged out.
        # Routing it through a browser session too would just be slower for no
        # reason.
        agent_key = self.preset.get("via_agent_login") if section in ("search", "detail") else None
        if agent_key:
            # Some agents show real pricing/results only to a signed-in account;
            # a plain HTTP client has no session at all and can never reach that.
            # Routes through a real browser bound to the profile `agent-login`
            # set up, so the fetch actually carries that login's cookies.
            # Confirmed live (USFans' keyword search): an agent's own endpoint
            # can just as well be a POST with a JSON body, not only GET.
            from ..agents import agent_browser_session

            if method == "GET":
                full_url = url + ("?" + urlencode(params) if params else "")
                with agent_browser_session(agent_key) as session:
                    return session.fetch_json(full_url, referer=self.base_url, headers=headers)
            with agent_browser_session(agent_key) as session:
                return session.fetch_json(
                    url, referer=self.base_url, headers=headers,
                    method=method, body=_expand(spec.get("body"), ctx),
                )

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

    def detail(self, item_id: str, url: str = "", *, skip_resolve: bool = False) -> Optional[RawOffer]:
        """``skip_resolve``: the caller already has this preset's own native id
        (confirmed live: a USFans search result's ``goodsId`` works directly
        against ``detail``, no ``resolve`` needed) -- resolve exists to turn an
        *external* source-site URL into that native id, which is meaningless to
        do to an id that's already native.
        """
        resolve_spec = self.preset.get("resolve") if not skip_resolve else None
        if resolve_spec:
            # Some agents (USFans confirmed live) don't accept the source
            # site's own item id at their detail endpoint at all -- only an
            # opaque token their own "paste a link" tool issues for it, valid
            # for that URL specifically. This step gets one, anonymously
            # (see the via_agent_login scoping note in call()), then the real
            # detail call below uses it in place of item_id.
            resolved = self.call("resolve", {"id": item_id, "url": url})
            new_id = dig(resolved, (resolve_spec.get("map") or {}).get("id"))
            if not new_id:
                log.info("[%s/%s] resolve step returned no usable id for %s",
                         self.preset_name, self.site_key, url or item_id)
                return None
            item_id = str(new_id)

        payload = self.call("detail", {"id": item_id, "url": url})
        mapping = self.preset.get("map", {})
        node = dig(payload, mapping.get("detail_path")) or payload
        if isinstance(node, list):
            node = node[0] if node else None
        if not node:
            return None
        # `url` here is still the real URL this was called with -- resolve only
        # ever reassigns `item_id`, never `url`. Whenever this *preset* uses a
        # resolve step at all (regardless of whether it ran on this particular
        # call -- a skip_resolve call has just as untrustworthy a detailUrl),
        # trust the caller's URL over the API's own returned url field
        # outright: confirmed live that the API's field is not merely
        # sometimes empty but can be populated with a URL built from an
        # opaque, non-numeric token in place of the source site's real id --
        # syntactically a valid-looking URL, semantically pointing nowhere.
        offer = self.to_offer(
            node, detail=True, fallback_url=url,
            prefer_fallback_url=bool(self.preset.get("resolve")),
        )
        if offer:
            offer.detail_fetched = True
        return offer

    def to_offer(
        self, item: dict, detail: bool = False, fallback_url: str = "",
        prefer_fallback_url: bool = False,
    ) -> Optional[RawOffer]:
        """Map one provider record onto RawOffer using the preset's field paths.

        ``fallback_url``: the real URL this lookup was actually called with, if
        the caller has one. Matters specifically for a preset with a ``resolve``
        step (USFans): the id it maps back (``goodsId``) can be an opaque
        per-request token, not the source site's real item id.

        ``prefer_fallback_url``: use ``fallback_url`` outright rather than only
        when the API's own url field is empty. Confirmed live that USFans'
        ``detailUrl`` is not merely sometimes null -- it can be *populated*
        with a URL built from that same opaque id substituted for the real
        one, which is syntactically a fine-looking URL and semantically wrong,
        so "is it empty" isn't a reliable enough test to fall back on for a
        resolve-based preset. Callers pass this whenever a resolve step ran.
        """
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
        if not price:
            # Some vendors only give a formatted string ("¥18.50" / "18.50 CNY").
            price, _, parsed_ccy = parse_price(str(dig(item, f.get("price_text"), "")), currency)
            currency = parsed_ccy or currency
        # A multi-SKU listing routinely reports 0 (or nothing at all) at the top
        # level -- the real price lives per-SKU. Zero is not a price: it would
        # win every cheapest-price comparison in the catalog, so treat it the
        # same as "not disclosed" rather than as a genuinely free item.
        if not price:
            price = None

        # Providers hand back protocol-relative ("//item.taobao.com/...") and
        # occasionally relative URLs. Normalize to absolute: this value is stored,
        # linked, and URL-encoded into forwarding-agent deep links, all of which
        # break on a bare "//" prefix.
        url = fallback_url if (prefer_fallback_url and fallback_url) else ""
        if not url:
            url = _https(str(dig(item, f.get("url"), "") or ""))
        if not url and fallback_url:
            url = fallback_url
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

        variants_map = f.get("variants") or {}
        for node in as_list(dig(item, variants_map.get("list"))):
            if not isinstance(node, dict):
                continue
            sku = str(dig(node, variants_map.get("sku")) or "")
            if not sku:
                continue
            v_price = as_float(dig(node, variants_map.get("price")))
            v_stock = as_int(dig(node, variants_map.get("stock"))) if variants_map.get("stock") else None
            v_image = _https(str(dig(node, variants_map.get("image")) or "")) or None
            attrs = self._variant_attrs(item, node, variants_map)
            offer.add_variant(
                sku=sku,
                name=", ".join(f"{k}: {v}" for k, v in attrs.items()) or sku,
                price=v_price,
                currency=offer.currency,
                attrs=attrs,
                stock=v_stock,
                in_stock=(v_stock > 0) if v_stock is not None else True,
                image_url=v_image,
            )
        if offer.variants:
            # A multi-SKU listing's own top-level price is routinely 0 or absent
            # (confirmed live, USFans/Taobao) -- the real range lives per-SKU.
            priced = [v.price for v in offer.variants if v.price]
            if priced:
                if not offer.price_min:
                    offer.price_min = min(priced)
                if not offer.price_max:
                    offer.price_max = max(priced)

        # These three sites never ship internationally, whatever the provider says.
        offer.fees_note = (
            "Domestic-China listing sourced via an API provider. International "
            "shipping, consolidation and any service fee are charged by your "
            "forwarding agent, not by the marketplace."
        )
        return offer

    @staticmethod
    def _variant_attrs(item: dict, sku_node: dict, variants_map: dict) -> dict[str, str]:
        """Human-readable {Color: Black, Size: M} for one SKU.

        Confirmed live (Taobao via USFans, and this shape is standard across the
        whole Taobao/Tmall/1688 family, not USFans-specific): a SKU never carries
        its own option names -- it references them as opaque "propId:valueId"
        pairs (``valueIds``) into a *separate*, top-level list of property
        definitions. Without cross-referencing that list, a variant would be
        addressable (the id is real) but unreadable (no way to show a shopper
        which one is "black" and which is "M").
        """
        ref_field = variants_map.get("value_ref")
        props_path = variants_map.get("properties_list")
        if not ref_field or not props_path:
            return {}

        lookup: dict[str, tuple[str, str]] = {}
        for group in as_list(dig(item, props_path)):
            if not isinstance(group, dict):
                continue
            prop_name = str(
                group.get(variants_map.get("prop_name_en_key", "propNameEn"))
                or group.get(variants_map.get("prop_name_key", "propName"), "")
                or ""
            )
            for value in group.get(variants_map.get("prop_values_key", "valuesList"), []) or []:
                if not isinstance(value, dict):
                    continue
                vid = str(value.get(variants_map.get("value_id_key", "valueId"), ""))
                vname = str(
                    value.get(variants_map.get("value_name_en_key", "valueNameEn"))
                    or value.get(variants_map.get("value_name_key", "valueName"), "")
                    or ""
                )
                if vid and vname:
                    lookup[vid] = (prop_name, vname)

        attrs: dict[str, str] = {}
        for ref in dig(sku_node, ref_field) or []:
            # Each ref is "propId:valueId" -- only the valueId half is a key
            # into the lookup above; propId is redundant with it there.
            vid = str(ref).split(":")[-1]
            if vid in lookup:
                pname, vname = lookup[vid]
                if pname:
                    attrs[pname] = vname
        return attrs


def _https(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    return url if url.startswith("http") else ""


# ------------------------------------------------------------------- probing


def probe(
    preset_name: str, site_key: str, keyword: str, fetcher,
    *, item_url: str | None = None,
) -> dict:
    """Call a provider and report what the mapping actually extracted.

    Purpose-built for the moment you sign up somewhere new: it shows the raw JSON
    keys next to the mapped result, so a wrong ``items_path`` (or, for a
    detail-only preset, a wrong ``detail_path``/``map.item.*``) is obvious rather
    than showing up as a silently empty crawl.

    Detail-only presets (``agent_lookup``, ``usfans``: most forwarding agents do
    item lookup, not keyword search) have no ``search`` section to probe at all
    -- pass ``item_url`` and this tests ``detail`` instead. Without one, this
    fails with a clear message rather than the confusing "no 'search' section"
    ``ProviderError`` a plain keyword probe used to raise on them.
    """
    client = ProviderClient(preset_name, site_key, fetcher)

    if not client.can_search:
        if not item_url:
            raise ProviderError(
                f"provider {preset_name!r} has no search section (most forwarding "
                f"agents do item lookup only) -- pass --url with a real item link "
                f"from {site_key} to probe its detail call instead"
            )
        return _probe_detail(client, preset_name, site_key, item_url)

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
            report["mapped"] = _offer_summary(offer)
    return report


def _probe_detail(client: "ProviderClient", preset_name: str, site_key: str, item_url: str) -> dict:
    # Diagnostic only: a real adapter extracts this with a site-specific regex.
    # Good enough to drive the detail call -- for a preset with its own
    # `resolve` step (USFans), this raw id is discarded anyway in favour of
    # whatever `resolve` returns.
    m = re.search(r"(\d{6,})", item_url)
    item_id = m.group(1) if m else item_url

    resolve_report = None
    if client.preset.get("resolve"):
        resolved = client.call("resolve", {"id": item_id, "url": item_url})
        resolve_map = (client.preset.get("resolve") or {}).get("map") or {}
        resolved_id = dig(resolved, resolve_map.get("id"))
        resolve_report = {
            "top_level_keys": sorted(resolved.keys()) if isinstance(resolved, dict) else type(resolved).__name__,
            "resolved_id": resolved_id,
        }
        if resolved_id:
            item_id = str(resolved_id)

    # One detail call, not two: mapping the same payload the human sees below
    # avoids a second (for via_agent_login, a second real browser launch) round
    # trip just to reproduce what the first one already returned.
    raw_payload = client.call("detail", {"id": item_id, "url": item_url})
    mapping = client.preset.get("map", {})
    node = dig(raw_payload, mapping.get("detail_path")) or raw_payload
    if isinstance(node, list):
        node = node[0] if node else None
    offer = client.to_offer(
        node, detail=True, fallback_url=item_url, prefer_fallback_url=bool(client.preset.get("resolve")),
    ) if node else None

    return {
        "preset": preset_name,
        "site": site_key,
        "mode": "detail",
        "item_url": item_url,
        "resolve": resolve_report,
        # Chinese API envelopes overwhelmingly carry the real status here even on
        # HTTP 200 -- {"code":401,"msg":"Please log in to continue"} being exactly
        # the case this preset exists for. Surfaced directly rather than making
        # someone infer "still not logged in" from node_keys equalling
        # top_level_keys, which just means "the descent found nothing" and could
        # mean several different things on its own.
        "status_fields": _status_fields(raw_payload),
        "top_level_keys": sorted(raw_payload.keys()) if isinstance(raw_payload, dict) else type(raw_payload).__name__,
        "detail_path": mapping.get("detail_path"),
        "node_keys": sorted(node.keys()) if isinstance(node, dict) else type(node).__name__ if node else None,
        # The *values* map.item.* actually pulled out, before to_offer()'s own
        # fallback logic runs on top of them -- keys/status alone don't
        # distinguish "field is null" from "field is populated with something
        # unusable", and those need different fixes.
        "raw_mapped_fields": _raw_mapped_fields(node, mapping.get("item", {})) if isinstance(node, dict) else None,
        "mapped": _offer_summary(offer) if offer else None,
    }


def _status_fields(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    return {
        k: payload[k] for k in ("code", "msg", "message", "error", "status", "success")
        if k in payload
    }


def _raw_mapped_fields(node: dict, item_map: dict) -> dict:
    """The literal dig() result for each map.item.* entry, truncated for
    display -- shows exactly what a field resolved to (null vs. a populated but
    wrong value vs. genuinely absent) without needing another round trip."""
    out = {}
    for field, path in (item_map or {}).items():
        if not isinstance(path, (str, list)):
            continue  # e.g. the nested `specs` sub-mapping, not a plain field
        value = dig(node, path)
        out[field] = value[:200] if isinstance(value, str) else value
    return out


def _offer_summary(offer: RawOffer) -> dict:
    return {
        "id": offer.site_product_id,
        "title": offer.title[:80],
        "url": offer.url[:100],
        "price": offer.price_min,
        "price_max": offer.price_max,
        "currency": offer.currency,
        "moq": offer.moq,
        "images": len(offer.image_urls),
        "specs": len(offer.specs),
        "tiers": len(offer.tiers),
        "variants": [
            {"sku": v.sku, "name": v.name, "price": v.price, "stock": v.stock,
             "in_stock": v.in_stock, "attrs": v.attrs}
            for v in offer.variants
        ],
    }


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
