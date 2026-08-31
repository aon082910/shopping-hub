"""Adapter contract.

Every site implements :class:`SiteAdapter`. The rest of the pipeline never knows
which site a record came from -- it only sees :class:`RawOffer`, which is deliberately
"as listed": raw strings, native currency, no translation. Normalization, FX and
translation all happen downstream in ``pipeline/ingest.py`` so that adding a site
means writing one file and nothing else.

Two required methods:
    search(keyword, max_pages) -> yields RawOffer  (listing pages, cheap, shallow)
    fetch_detail(offer)        -> RawOffer          (product page, expensive, deep)

``search`` should never raise for a single bad card -- log and skip. ``fetch_detail``
may raise; the ingest loop catches and marks the offer stale.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from selectolax.parser import HTMLParser

from ..config import CrawlConfig, load_crawl_config
from ..util.http import Fetcher, absolute_url

log = logging.getLogger(__name__)


@dataclass
class RawSpec:
    key: str
    value: str
    position: int = 0


@dataclass
class RawTier:
    min_qty: int
    max_qty: Optional[int]
    price: float
    currency: str = "USD"


@dataclass
class RawVariant:
    """One purchasable SKU inside a listing."""

    sku: str
    name: str
    price: Optional[float] = None
    currency: Optional[str] = None
    attrs: dict[str, str] = field(default_factory=dict)
    stock: Optional[int] = None
    in_stock: bool = True
    image_url: Optional[str] = None


@dataclass
class RawOffer:
    """One listing, exactly as the site presents it."""

    site_key: str
    site_product_id: str
    url: str
    title: str

    currency: str = "USD"
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    tiers: list[RawTier] = field(default_factory=list)

    moq: int = 1
    moq_unit: str = "piece"
    lead_time_days: Optional[int] = None

    shipping_cost: Optional[float] = None
    shipping_currency: Optional[str] = None
    shipping_free: bool = False
    shipping_from: Optional[str] = None
    shipping_note: Optional[str] = None
    fees_note: Optional[str] = None

    brand: Optional[str] = None
    model: Optional[str] = None
    mpn: Optional[str] = None
    gtin: Optional[str] = None

    seller_name: Optional[str] = None
    seller_url: Optional[str] = None
    seller_years: Optional[int] = None
    is_verified_supplier: bool = False
    rating: Optional[float] = None
    review_count: Optional[int] = None
    orders_count: Optional[int] = None

    description: Optional[str] = None
    variants: list[RawVariant] = field(default_factory=list)
    specs: list[RawSpec] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    category_path: Optional[str] = None
    in_stock: bool = True
    detail_fetched: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def add_variant(self, sku: str, name: str, price: Optional[float] = None,
                    **kw) -> None:
        sku, name = (sku or "").strip(), (name or "").strip()
        if sku or name:
            self.variants.append(
                RawVariant(sku=sku or name, name=name or sku, price=price, **kw)
            )

    def add_spec(self, key: str, value: str) -> None:
        key, value = (key or "").strip(), (value or "").strip()
        if key and value and len(key) < 200:
            self.specs.append(RawSpec(key, value[:1000], len(self.specs)))


class SiteAdapter(ABC):
    """Base class. Subclasses set ``key`` and implement ``search``/``fetch_detail``."""

    key: str = ""
    name: str = ""
    base_url: str = ""
    home_currency: str = "USD"
    needs_agent: bool = False

    def __init__(self, config: CrawlConfig | None = None):
        self.config = config or load_crawl_config()
        self.site_cfg = self.config.site(self.key)
        self._fetcher: Optional[Fetcher] = None
        self._browser: Any = None

    # -- plumbing -----------------------------------------------------------

    @property
    def fetcher(self) -> Fetcher:
        if self._fetcher is None:
            self._fetcher = Fetcher(
                delay=float(self.site_cfg.get("delay_seconds", 2.5)),
                timeout=float(self.site_cfg.get("request_timeout", 45)),
                retries=int(self.site_cfg.get("retries", 3)),
                headers=self.extra_headers(),
            )
        return self._fetcher

    def extra_headers(self) -> dict[str, str]:
        return {}

    # Several storefronts (Banggood prices, Chinavasion results, DHgate cards) render
    # their listings client-side: the HTML you get over plain HTTP contains the card
    # skeleton with empty price nodes, or no cards at all. `render: browser` in
    # config.yaml runs the page in Chromium so the XHRs complete before parsing.
    # Slower and heavier, so it stays opt-in per site.
    result_selector: str = ""

    @property
    def render_mode(self) -> str:
        return str(self.site_cfg.get("render", "http")).lower()

    def render_mode_for(self, phase: str) -> str:
        """Rendering can differ per phase.

        AliExpress is the case that forces this: its *search* page ships a complete
        JSON blob over plain HTTP (fast, 59 items), while its *product* page is a
        77KB JS shell with no attributes in the HTML at all. Rendering both in a
        browser would throw away a working fast path; rendering neither loses every
        spec. `render_search:` / `render_detail:` override `render:` per phase.
        """
        return str(
            self.site_cfg.get(f"render_{phase}", self.site_cfg.get("render", "http"))
        ).lower()

    @property
    def browser(self):
        from ..util.browser import BrowserSession

        if self._browser is None:
            self._browser = BrowserSession().start()
        return self._browser

    def fetch_html(
        self,
        url: str,
        *,
        referer: str | None = None,
        wait_selector: str | None = None,
        phase: str = "search",
    ) -> str:
        """HTML for a page, rendered in a browser when that phase needs it."""
        if self.render_mode_for(phase) == "browser":
            return self.browser.get_html(
                url, wait_selector=wait_selector or self.result_selector or None
            )
        return self.fetcher.get(url, referer=referer or (self.base_url + "/")).text

    def close(self) -> None:
        if self._fetcher is not None:
            self._fetcher.close()
            self._fetcher = None
        if getattr(self, "_browser", None) is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None

    def __enter__(self) -> "SiteAdapter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def max_pages(self) -> int:
        return int(self.site_cfg.get("max_pages_per_keyword", 5))

    # Adapters declare their listing URL as either ``SEARCH`` or ``search_template``
    # (historical drift), both using {kw}/{page}. This is the one accessor that
    # resolves either, so fixture capture and diagnostics don't need to know which.
    SEARCH: str = ""
    search_template: str = ""

    def search_page_url(self, keyword: str, page: int = 1) -> str:
        """URL of one listing page. Override when pagination isn't a page number."""
        from urllib.parse import quote_plus

        template = self.search_template or self.SEARCH
        if not template:
            raise NotImplementedError(
                f"{type(self).__name__} defines neither search_template nor SEARCH; "
                "override search_page_url()"
            )
        return template.format(kw=quote_plus(keyword), page=page)

    # -- contract -----------------------------------------------------------

    @abstractmethod
    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        """Yield shallow offers from listing pages."""

    def fetch_detail(self, offer: RawOffer) -> RawOffer:
        """Enrich an offer from its product page. Default: no-op."""
        return offer

    def category_seeds(self) -> list[tuple[str, str]]:
        """Optional [(category_path, url)] pairs for category-driven crawling."""
        return []

    # Values marketplaces put in schema.org fields that are not product identity.
    PLACEHOLDER_BRANDS = {
        "none", "n/a", "na", "null", "generic", "no brand", "nobrand", "oem",
        "unbranded", "other", "others", "brand", "no", "-", "unknown",
    }

    def plausible_brand(self, value: Optional[str]) -> Optional[str]:
        """Drop brand values that are not a product brand.

        Marketplaces routinely put *their own name* in schema.org ``brand`` -- DHgate
        emits ``{"@type":"Brand","name":"dhgate"}`` on every listing. Accepting that
        would give tens of thousands of unrelated products one shared brand, and
        brand+MPN matching (weight 0.92) would then merge them into nonsense.
        """
        if not value:
            return None
        v = value.strip()
        low = v.lower()
        if low in self.PLACEHOLDER_BRANDS or len(v) < 2:
            return None
        # The site naming itself, in any of its spellings.
        site_words = {self.key.lower(), self.name.lower(),
                      self.name.lower().replace(".com", "").replace(" ", "")}
        domain = self.base_url.split("//")[-1].replace("www.", "").split(".")[0].lower()
        site_words.add(domain)
        if low.replace(" ", "") in site_words:
            return None
        return v

    def plausible_mpn(self, value: Optional[str], site_product_id: str = "") -> Optional[str]:
        """Drop identifiers that are site bookkeeping rather than a part number.

        A manufacturer part number is useful because it is the *same string on every
        site*. A purely numeric value almost never is -- it is a category id (DHgate
        emits ``mpn: "104"``) or the site's own product id, neither of which carries
        across sites, and both of which can collide by coincidence.
        """
        if not value:
            return None
        v = value.strip()
        if len(v) < 3 or v.lower() in self.PLACEHOLDER_BRANDS:
            return None
        if site_product_id and v == str(site_product_id):
            return None
        if v.isdigit():
            return None
        return v

    # ------------------------------------------------------- structured data

    @staticmethod
    def json_ld_nodes(tree) -> list[dict]:
        """Every schema.org node on the page, flattened out of @graph wrappers."""
        import json as _json

        out: list[dict] = []

        def walk(node):
            if isinstance(node, dict):
                if "@graph" in node:
                    for child in node["@graph"]:
                        walk(child)
                out.append(node)
            elif isinstance(node, list):
                for child in node:
                    walk(child)

        for script in tree.css('script[type="application/ld+json"]'):
            try:
                walk(_json.loads(script.text()))
            except Exception:
                continue
        return out

    @classmethod
    def json_ld_product(cls, tree) -> Optional[dict]:
        """The Product node, if the page publishes one."""
        for node in cls.json_ld_nodes(tree):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(str(x).lower() in ("product", "individualproduct") for x in types):
                return node
        return None

    def apply_json_ld(self, offer: "RawOffer", tree) -> bool:
        """Fill an offer from schema.org markup. Returns True if anything was found.

        Worth preferring over CSS selectors wherever it exists: it is the one part of
        a product page a site maintains deliberately (for Google), so it survives the
        redesigns that break every hand-written selector. It is also the only place
        most storefronts publish brand/SKU/MPN/GTIN at all -- without it the matcher
        loses its two strongest signals and falls back to fuzzy title comparison.

        Existing values win: search-stage data is not overwritten by a thinner
        detail-stage node.
        """
        from ..util.text import clean, find_gtin, normalize_gtin

        node = self.json_ld_product(tree)
        if not node:
            return False

        def text(value) -> Optional[str]:
            if value is None:
                return None
            if isinstance(value, dict):
                value = value.get("name") or value.get("value") or value.get("@id")
            if isinstance(value, list):
                value = value[0] if value else None
            s = clean(str(value)) if value is not None else ""
            return s or None

        offer.brand = offer.brand or self.plausible_brand(text(node.get("brand")))
        offer.mpn = offer.mpn or self.plausible_mpn(
            text(node.get("mpn")) or text(node.get("sku")), offer.site_product_id
        )
        offer.model = offer.model or self.plausible_mpn(
            text(node.get("model")), offer.site_product_id
        ) or offer.mpn

        for key in ("gtin14", "gtin13", "gtin12", "gtin8", "gtin"):
            if offer.gtin:
                break
            offer.gtin = normalize_gtin(text(node.get(key)) or "")
        if not offer.gtin:
            offer.gtin = find_gtin(text(node.get("description")) or "")

        if not offer.description:
            offer.description = (text(node.get("description")) or "")[:8000] or None

        images = node.get("image")
        for url in (images if isinstance(images, list) else [images]):
            url = text(url)
            if url and url.startswith(("http", "//")):
                url = "https:" + url if url.startswith("//") else url
                if url not in offer.image_urls:
                    offer.image_urls.append(url)

        # schema.org PropertyValue pairs are a real spec table when present.
        for prop in node.get("additionalProperty") or []:
            if isinstance(prop, dict):
                offer.add_spec(text(prop.get("name")) or "", text(prop.get("value")) or "")

        offers_node = node.get("offers") or {}
        if isinstance(offers_node, list):
            offers_node = offers_node[0] if offers_node else {}
        if isinstance(offers_node, dict):
            price = offers_node.get("price") or offers_node.get("lowPrice")
            try:
                price = float(price) if price is not None else None
            except (TypeError, ValueError):
                price = None
            # Zero is never a real price; leaving it would win every comparison.
            if price and price > 0 and not offer.price_min:
                offer.price_min = price
                offer.currency = offers_node.get("priceCurrency") or offer.currency
            high = offers_node.get("highPrice")
            try:
                high = float(high) if high is not None else None
            except (TypeError, ValueError):
                high = None
            if high and offer.price_min and high > offer.price_min:
                offer.price_max = high
            avail = str(offers_node.get("availability", "")).lower()
            if avail:
                offer.in_stock = "outofstock" not in avail.replace(" ", "")

        rating = node.get("aggregateRating")
        if isinstance(rating, dict):
            try:
                offer.rating = offer.rating or float(rating.get("ratingValue"))
            except (TypeError, ValueError):
                pass
            try:
                offer.review_count = offer.review_count or int(
                    float(rating.get("reviewCount") or rating.get("ratingCount"))
                )
            except (TypeError, ValueError):
                pass

        return True

    # -- shared helpers -----------------------------------------------------

    def html(self, url: str, *, referer: str | None = None) -> HTMLParser:
        return HTMLParser(self.fetcher.get(url, referer=referer).text)

    def abs_url(self, href: str | None) -> Optional[str]:
        return absolute_url(self.base_url, href)

    @staticmethod
    def dedupe(offers):
        """Yield offers once per product id.

        Results pages routinely show the same listing twice -- eBay places items in
        both a sponsored and an organic slot. Ingest would collapse the duplicates
        anyway, but only after paying for a second detail fetch on each, so they are
        dropped at the source.
        """
        seen = set()
        for offer in offers:
            key = str(offer.site_product_id)
            if key in seen:
                continue
            seen.add(key)
            yield offer

    @staticmethod
    def select_cards(tree, selectors):
        """Return listing cards, trying each selector until one matches.

        Deliberately NOT a comma-joined ``css()`` call. selectolax yields an element
        once per matching *branch* of a selector group, so a card matching both
        halves of ``li.product-item, .item.product`` comes back TWICE -- silently
        doubling every listing on the page, wasting a detail fetch on each duplicate
        and inflating the crawl stats. Trying the selectors in order also expresses
        what these adapters actually want: a fallback chain across markup versions.
        """
        if isinstance(selectors, str):
            selectors = [s.strip() for s in selectors.split(",") if s.strip()]
        for selector in selectors:
            found = tree.css(selector)
            if found:
                return found
        return []

    @staticmethod
    def text_of(node, selector: str, default: str = "") -> str:
        if node is None:
            return default
        found = node.css_first(selector)
        return found.text(strip=True) if found else default

    @staticmethod
    def attr_of(node, selector: str, attr: str) -> Optional[str]:
        if node is None:
            return None
        found = node.css_first(selector)
        if not found:
            return None
        return found.attributes.get(attr)

    @staticmethod
    def first_attr(node, attrs: tuple[str, ...] = ("src", "data-src", "data-lazy-src",
                                                   "data-original", "data-ks-lazyload")):
        """Image URL from whichever lazy-load attribute this site happens to use."""
        if node is None:
            return None
        for a in attrs:
            v = node.attributes.get(a)
            if v and not v.startswith("data:"):
                return v
        srcset = node.attributes.get("srcset")
        if srcset:
            return srcset.split(",")[0].strip().split(" ")[0]
        return None

    @staticmethod
    def parse_int(text: str | None) -> Optional[int]:
        if not text:
            return None
        m = re.search(r"[\d,.]+\s*[KkMm万]?", text.replace(" ", ""))
        if not m:
            return None
        token = m.group(0)
        mult = 1.0
        if token[-1] in "Kk":
            mult, token = 1_000.0, token[:-1]
        elif token[-1] in "Mm":
            mult, token = 1_000_000.0, token[:-1]
        elif token[-1] == "万":
            mult, token = 10_000.0, token[:-1]
        try:
            return int(float(token.replace(",", "")) * mult)
        except ValueError:
            return None

    @staticmethod
    def parse_float(text: str | None) -> Optional[float]:
        if not text:
            return None
        m = re.search(r"\d+(?:\.\d+)?", text.replace(",", ""))
        return float(m.group(0)) if m else None

    @staticmethod
    def json_blobs(html_text: str, *var_names: str) -> Iterator[Any]:
        """Yield parsed JSON assigned to any of ``var_names`` in inline <script>.

        These marketplaces render server-side into globals like
        ``window.runParams = {...}`` -- reading that is far more robust than
        scraping the DOM, which changes weekly.

        Two things make this harder than it sounds, both observed live on
        AliExpress:

        * the identifier appears dozens of times in minified code as a plain
          property *access*, so only sites where an assignment follows are accepted;
        * the value is a JavaScript object literal, not JSON. AliExpress emits
          ``_init_data_= { data: {"hierarchy": ...} }`` with the outer key unquoted,
          which ``json.loads`` rejects outright. When strict parsing fails we retry
          from the first ``{"`` inside, where the genuinely-JSON part begins.
        """
        import json

        for name in var_names:
            # Require an assignment (`name = {` / `name: {`). A bare occurrence is a
            # minified reference, not data.
            pattern = re.escape(name) + r"\s*[:=]\s*(?=[{\[])"
            for m in re.finditer(pattern, html_text):
                start = m.end()
                candidate = _balance_json(html_text, start)
                if not candidate:
                    continue
                try:
                    yield json.loads(candidate)
                    continue
                except Exception:
                    pass
                inner = html_text.find('{"', start)
                if inner == -1 or inner > start + 4000:
                    continue
                nested = _balance_json(html_text, inner)
                if not nested:
                    continue
                try:
                    yield json.loads(nested)
                except Exception:
                    continue


def _balance_json(text: str, start: int) -> Optional[str]:
    """Extract a complete JSON object/array starting at ``start`` by brace counting.

    Regex alone cannot match nested braces; the greedy/lazy variants both fail on
    real payloads, so we count depth while respecting string literals.
    """
    if start >= len(text) or text[start] not in "{[":
        return None
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(start, min(len(text), start + 4_000_000)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None
