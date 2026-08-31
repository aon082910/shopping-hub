"""Taobao, Tmall and 1688 -- the Alibaba domestic-China properties.

**Read this before you file a bug.** These three are categorically different from
the other eight sites:

1. Search results require a logged-in session. Anonymous requests get bounced to
   ``login.taobao.com`` or a slider captcha. There is no header trick around it.
2. They are Chinese-language only, and prices are CNY.
3. They do not ship internationally. Every offer here is annotated ``needs_agent``
   and the item page surfaces forwarding-agent deep links instead of a buy button.

Crawling splits into two jobs with different costs, and they are configured
independently (``driver:`` in ``config.yaml`` is shorthand for both):

  ============  ==================  ==================
  driver        search (discovery)  detail (enrich)
  ============  ==================  ==================
  ``browser``   browser             browser
  ``provider``  provider            provider
  ``hybrid``    browser             provider
  ============  ==================  ==================

``browser`` is Playwright against a persistent profile you logged into once via
``python -m sourcehub.cli browser-login --site taobao``. Free and it works, but
slow, and every page is a chance to get challenged.

``provider`` is an HTTP JSON API -- no login, no captcha, no browser. Endpoints and
field mappings are data-driven (``providers.yaml``, see ``scrapers/provider.py``).
Set CN_PROVIDER_PRESET/CN_PROVIDER_KEY and verify with ``provider-probe``.

**``hybrid`` is usually the right answer.** Forwarding agents expose item *lookup*
but not keyword search, so they cannot discover products -- yet detail fetching is
where nearly all the traffic goes: one gated page per listing page during search
versus one per *product* during enrichment. Hybrid keeps discovery on the browser
and moves the expensive bulk onto the API, which cuts gated page loads by roughly
the number of products per results page (~40x on Taobao).

You can also set ``search_driver:`` / ``detail_driver:`` explicitly. Either way, a
driver that asks for the provider is downgraded to the browser when the provider
cannot do that job, so a lookup-only agent configured as ``driver: provider``
resolves to hybrid automatically instead of crawling nothing.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterator, Optional
from urllib.parse import quote_plus

from selectolax.parser import HTMLParser

from ..config import get_settings
from ..util.browser import BrowserSession, BrowserUnavailable
from ..util.http import BlockedError
from ..util.money import parse_moq, parse_price, parse_tiers
from ..util.text import clean
from .base import RawOffer, RawTier, SiteAdapter

log = logging.getLogger(__name__)

_UNSET = object()

# `driver:` is shorthand for a (search_driver, detail_driver) pair. Either half can
# be overridden individually in config.yaml.
DRIVER_PRESETS: dict[str, tuple[str, str]] = {
    "browser": ("browser", "browser"),
    "provider": ("provider", "provider"),
    # Discovery over the browser (agents can't search), enrichment over the API.
    "hybrid": ("browser", "provider"),
}


class _AlibabaCNBase(SiteAdapter):
    """Shared browser/provider plumbing for taobao, tmall and 1688."""

    home_currency = "CNY"
    needs_agent = True
    login_url = ""
    search_template = ""
    item_url_template = ""
    result_selector = ""

    def __init__(self, config=None):
        super().__init__(config)
        self._browser: Optional[BrowserSession] = None
        self._provider: Any = _UNSET
        self._drivers: Optional[tuple[str, str]] = None

    # ------------------------------------------------------------- driver mux

    @property
    def provider(self):
        """Lazily built provider client, or None if it can't be configured.

        The failure is cached too -- otherwise every offer in a crawl re-attempts
        the same broken configuration and re-logs the same error.
        """
        if self._provider is not _UNSET:
            return self._provider
        from .provider import ProviderClient, ProviderError

        s = get_settings()
        self._provider = None
        if not (s.cn_provider_key or s.cn_provider_base_url):
            return None
        preset = self.site_cfg.get("provider_preset") or s.cn_provider_preset
        try:
            self._provider = ProviderClient(
                preset, self.key, self.fetcher, base_url=s.cn_provider_base_url
            )
        except ProviderError as e:
            log.error("[%s] provider not usable: %s", self.key, e)
        return self._provider

    def _resolve_drivers(self) -> tuple[str, str]:
        """Decide how to do discovery and how to do enrichment, independently.

        ``driver:`` is shorthand for a (search, detail) pair; ``search_driver:`` and
        ``detail_driver:`` override either half. Anything asking for the provider is
        then *downgraded to browser* if the provider can't actually do that job --
        which is the common case, since forwarding-agent endpoints do item lookup
        but not keyword search. That downgrade is what silently turns a misconfigured
        ``driver: provider`` into a working hybrid instead of an empty crawl.
        """
        cfg = self.site_cfg
        base = str(cfg.get("driver", "browser")).lower()
        if base not in DRIVER_PRESETS:
            log.warning("[%s] unknown driver %r; using 'browser'. Valid: %s",
                        self.key, base, ", ".join(sorted(DRIVER_PRESETS)))
            base = "browser"
        search, detail = DRIVER_PRESETS[base]
        search = str(cfg.get("search_driver", search)).lower()
        detail = str(cfg.get("detail_driver", detail)).lower()

        if search == "provider":
            client = self.provider
            if client is None:
                log.warning(
                    "[%s] search_driver=provider but no provider is configured "
                    "(set CN_PROVIDER_KEY); falling back to the browser for search.",
                    self.key,
                )
                search = "browser"
            elif not client.can_search:
                log.warning(
                    "[%s] provider preset %r has no `search:` section -- it can only "
                    "look up items you already have, which is normal for forwarding "
                    "agents. Using the browser for discovery instead (hybrid mode).",
                    self.key, client.preset_name,
                )
                search = "browser"

        if detail == "provider":
            client = self.provider
            if client is None:
                log.warning(
                    "[%s] detail_driver=provider but no provider is configured; "
                    "using the browser for detail.", self.key,
                )
                detail = "browser"
            elif not client.can_detail:
                log.warning(
                    "[%s] provider preset %r has no `detail:` section; using the "
                    "browser for detail.", self.key, client.preset_name,
                )
                detail = "browser"

        if (search, detail) != DRIVER_PRESETS.get(base):
            log.info("[%s] drivers resolved: search=%s detail=%s (from %r)",
                     self.key, search, detail, base)
        return search, detail

    @property
    def drivers(self) -> tuple[str, str]:
        if self._drivers is None:
            self._drivers = self._resolve_drivers()
        return self._drivers

    @property
    def search_driver(self) -> str:
        return self.drivers[0]

    @property
    def detail_driver(self) -> str:
        return self.drivers[1]

    @property
    def browser(self) -> BrowserSession:
        if self._browser is None:
            self._browser = BrowserSession().start()
        return self._browser

    def close(self) -> None:
        super().close()
        if self._browser is not None:
            self._browser.close()
            self._browser = None

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        pages = max_pages or self.max_pages
        if self.search_driver == "provider":
            yield from self._search_provider(keyword, pages)
        else:
            yield from self._search_browser(keyword, pages)

    # ------------------------------------------------------------ browser path

    def _search_browser(self, keyword: str, pages: int) -> Iterator[RawOffer]:
        try:
            browser = self.browser
        except BrowserUnavailable as e:
            log.error("[%s] %s", self.key, e)
            return

        for page in range(1, pages + 1):
            url = self.search_template.format(kw=quote_plus(keyword), page=page)
            try:
                html = browser.get_html(url, wait_selector=self.result_selector, wait_ms=2000)
            except BlockedError as e:
                log.error(
                    "[%s] blocked on page %s (%s).\n"
                    "  The saved browser session is expired or challenged. Re-run:\n"
                    "    python -m sourcehub.cli browser-login --site %s",
                    self.key, page, e, self.key,
                )
                return
            except Exception as e:
                log.warning("[%s] page %s failed: %s", self.key, page, e)
                return

            found = 0
            for offer in self.parse_results(html):
                found += 1
                yield offer
            if found == 0:
                log.info("[%s] page %s empty for %r; stopping", self.key, page, keyword)
                return

    # ----------------------------------------------------------- provider path

    def _search_provider(self, keyword: str, pages: int) -> Iterator[RawOffer]:
        client = self.provider
        if client is None or not client.can_search:
            return  # already reported and downgraded in _resolve_drivers

        for page in range(1, pages + 1):
            found = 0
            try:
                for offer in client.search(keyword, page):
                    found += 1
                    yield offer
            except Exception as e:
                log.warning("[%s] provider search failed on page %s: %s", self.key, page, e)
                return
            if not found:
                return

    # ------------------------------------------------------------ subclass API

    def parse_results(self, html: str) -> Iterator[RawOffer]:
        raise NotImplementedError

    def fetch_detail(self, offer: RawOffer) -> RawOffer:
        """Enrich one offer, honouring the resolved detail driver.

        In hybrid mode this is where the win is: discovery costs one gated browser
        page per *listing page*, while enrichment -- normally one gated page per
        *product*, which is the bulk of the traffic and where blocks happen -- goes
        over the API instead.
        """
        if self.detail_driver == "provider":
            offer = self._detail_provider(offer)
            if offer.detail_fetched:
                return offer
            if not self.site_cfg.get("detail_fallback", True):
                return offer
            log.info("[%s] provider had nothing for %s; falling back to the browser",
                     self.key, offer.site_product_id)
        return self._detail_browser(offer)

    def _detail_browser(self, offer: RawOffer) -> RawOffer:
        try:
            html = self.browser.get_html(offer.url, wait_ms=2500)
        except (BlockedError, BrowserUnavailable) as e:
            log.warning("[%s] detail blocked for %s: %s", self.key, offer.url, e)
            return offer
        except Exception as e:
            log.warning("[%s] detail failed for %s: %s", self.key, offer.url, e)
            return offer
        self.parse_detail(html, offer)
        offer.detail_fetched = True
        return offer

    def parse_detail(self, html: str, offer: RawOffer) -> None:
        tree = HTMLParser(html)
        for row in tree.css(
            "#J_AttrUL li, .attributes-list li, .obj-content tr, "
            ".od-pc-attribute-item, .offer-attr-item"
        ):
            txt = clean(row.text(separator=" "))
            if ":" in txt or "：" in txt:
                k, _, v = re.split(r"[:：]", txt, maxsplit=1)[0], ":", re.split(r"[:：]", txt, maxsplit=1)[-1]
                offer.add_spec(k, v)
        for img in tree.css("#J_UlThumb img, .tb-thumb img, .detail-gallery-img, .od-gallery-img img")[:12]:
            u = self.first_attr(img)
            if u:
                offer.image_urls.append(_https(re.sub(r"_\d+x\d+\.jpg.*$", "", u)))
        crumbs = [clean(a.text()) for a in tree.css(".breadcrumb a, .crumb a, .od-pc-breadcrumb a")]
        crumbs = [c for c in crumbs if c]
        if crumbs:
            offer.category_path = " > ".join(crumbs[:4])

    def _detail_provider(self, offer: RawOffer) -> RawOffer:
        client = self.provider
        if client is None or not client.can_detail:
            return offer
        try:
            enriched = client.detail(offer.site_product_id, offer.url)
        except Exception as e:
            log.warning("[%s] provider detail failed for %s: %s",
                        self.key, offer.site_product_id, e)
            return offer
        if enriched is None:
            return offer

        # Merge onto the search-stage offer rather than replacing it -- the listing
        # page often has fields (sales rank, location) the detail call omits.
        for field in ("description", "category_path", "brand", "model", "seller_url",
                      "shipping_from", "rating", "review_count"):
            value = getattr(enriched, field, None)
            if value and not getattr(offer, field, None):
                setattr(offer, field, value)
        if enriched.specs:
            offer.specs = enriched.specs
        if enriched.tiers:
            offer.tiers = enriched.tiers
            offer.moq = min(t.min_qty for t in enriched.tiers)
            offer.price_min = min(t.price for t in enriched.tiers)
            offer.price_max = max(t.price for t in enriched.tiers)
        for u in enriched.image_urls:
            if u not in offer.image_urls:
                offer.image_urls.append(u)
        if enriched.price_min is not None and not offer.tiers:
            offer.price_min = enriched.price_min
            offer.currency = enriched.currency
        offer.fees_note = offer.fees_note or enriched.fees_note
        offer.detail_fetched = True
        return offer


# ------------------------------------------------------------------------ 1688


class Alibaba1688Adapter(_AlibabaCNBase):
    key = "1688"
    name = "1688"
    base_url = "https://www.1688.com"
    login_url = "https://login.1688.com/member/signin.htm"
    search_template = "https://s.1688.com/selloffer/offer_search.htm?keywords={kw}&beginPage={page}"
    item_url_template = "https://detail.1688.com/offer/{id}.html"
    result_selector = ".sm-offer-item, .offer-list-row-offer, [data-h5-type='offerCard']"

    def parse_results(self, html: str) -> Iterator[RawOffer]:
        tree = HTMLParser(html)
        cards = self.select_cards(
            tree, self.result_selector.split(",") + ["[class*='offer-item']"]
        )
        for card in cards:
            try:
                link = card.css_first("a[href*='detail.1688.com'], a[href*='/offer/']")
                href = (link.attributes.get("href") if link else "") or ""
                m = re.search(r"/offer/(\d+)\.html", href)
                if not m:
                    continue
                pid = m.group(1)

                title = clean(
                    self.text_of(card, ".title, .offer-title, [class*='title']")
                    or (link.attributes.get("title") if link else "")
                )
                if not title:
                    continue

                price_text = self.text_of(card, ".price, [class*='price']")
                pmin, pmax, ccy = parse_price(price_text, "CNY")

                moq_text = self.text_of(card, ".sale, [class*='quantity'], [class*='sold']")
                moq, unit = parse_moq(moq_text or "")

                offer = RawOffer(
                    site_key=self.key,
                    site_product_id=pid,
                    url=self.item_url_template.format(id=pid),
                    title=title,
                    currency=ccy or "CNY",
                    price_min=pmin,
                    price_max=pmax,
                    moq=moq,
                    moq_unit=unit,
                    seller_name=clean(self.text_of(card, ".company, [class*='company']")) or None,
                    shipping_from=clean(self.text_of(card, ".location, [class*='area']")) or None,
                    orders_count=self.parse_int(self.text_of(card, "[class*='sold'], .sale-quantity")),
                    raw={"source": "browser"},
                )
                u = self.first_attr(card.css_first("img"))
                if u:
                    offer.image_urls.append(_https(u))
                yield offer
            except Exception as e:
                log.debug("[1688] bad card: %s", e)

    def parse_detail(self, html: str, offer: RawOffer) -> None:
        super().parse_detail(html, offer)
        tree = HTMLParser(html)
        ladder = tree.css_first(".price-list, .od-pc-offer-price, [class*='price-range']")
        if ladder:
            for tier in parse_tiers(ladder.text(separator=" "), "CNY"):
                offer.tiers.append(RawTier(**tier))
        if offer.tiers:
            offer.moq = min(t.min_qty for t in offer.tiers)
            offer.price_min = min(t.price for t in offer.tiers)
            offer.price_max = max(t.price for t in offer.tiers)
        # 1688 quotes domestic freight only; international is the agent's problem.
        offer.fees_note = (
            "Domestic-China listing. International shipping, consolidation and any "
            "service fee are charged by your forwarding agent, not by 1688."
        )


# ---------------------------------------------------------------------- taobao


class TaobaoAdapter(_AlibabaCNBase):
    key = "taobao"
    name = "Taobao"
    base_url = "https://www.taobao.com"
    login_url = "https://login.taobao.com/member/login.jhtml"
    search_template = "https://s.taobao.com/search?q={kw}&s={page_offset}"
    item_url_template = "https://item.taobao.com/item.htm?id={id}"
    result_selector = "[class*='Card--doubleCard'], .item.J_MouserOnverReq, #mainsrp-itemlist .items .item"

    def _page_url(self, keyword: str, page: int) -> str:
        # Taobao paginates by result offset, 44 per page, not by page number.
        return f"https://s.taobao.com/search?q={quote_plus(keyword)}&s={(page - 1) * 44}"

    def search_page_url(self, keyword: str, page: int = 1) -> str:
        return self._page_url(keyword, page)

    def _search_browser(self, keyword: str, pages: int) -> Iterator[RawOffer]:
        try:
            browser = self.browser
        except BrowserUnavailable as e:
            log.error("[%s] %s", self.key, e)
            return
        for page in range(1, pages + 1):
            try:
                html = browser.get_html(
                    self._page_url(keyword, page),
                    wait_selector=self.result_selector,
                    wait_ms=2000,
                )
            except BlockedError as e:
                log.error("[%s] blocked (%s). Re-run: python -m sourcehub.cli "
                          "browser-login --site %s", self.key, e, self.key)
                return
            except Exception as e:
                log.warning("[%s] page %s failed: %s", self.key, page, e)
                return
            found = 0
            for offer in self.parse_results(html):
                found += 1
                yield offer
            if not found:
                return

    def parse_results(self, html: str) -> Iterator[RawOffer]:
        # Taobao also embeds the result set as JSON; prefer it when present.
        for blob in self.json_blobs(html, "g_page_config", "window.g_page_config"):
            auctions = (blob.get("mods", {}).get("itemlist", {})
                        .get("data", {}).get("auctions", [])) if isinstance(blob, dict) else []
            for a in auctions:
                offer = self._offer_from_auction(a)
                if offer:
                    yield offer
            if auctions:
                return

        tree = HTMLParser(html)
        for card in self.select_cards(tree, self.result_selector):
            try:
                link = card.css_first("a[href*='item.htm'], a[href*='id=']")
                href = (link.attributes.get("href") if link else "") or ""
                m = re.search(r"[?&]id=(\d+)", href)
                if not m:
                    continue
                pid = m.group(1)
                title = clean(
                    self.text_of(card, "[class*='title'], .title")
                    or (link.attributes.get("title") if link else "")
                )
                if not title:
                    continue
                pmin, pmax, ccy = parse_price(
                    self.text_of(card, "[class*='price'], .price"), "CNY"
                )
                offer = RawOffer(
                    site_key=self.key,
                    site_product_id=pid,
                    url=self.item_url_template.format(id=pid),
                    title=title,
                    currency=ccy or "CNY",
                    price_min=pmin,
                    price_max=pmax,
                    seller_name=clean(self.text_of(card, "[class*='shopName'], .shopname")) or None,
                    shipping_from=clean(self.text_of(card, "[class*='procity'], .location")) or None,
                    orders_count=self.parse_int(
                        self.text_of(card, "[class*='realSales'], .deal-cnt")
                    ),
                    raw={"source": "browser"},
                )
                u = self.first_attr(card.css_first("img"))
                if u:
                    offer.image_urls.append(_https(u))
                yield offer
            except Exception as e:
                log.debug("[taobao] bad card: %s", e)

    def _offer_from_auction(self, a: dict) -> Optional[RawOffer]:
        pid = str(a.get("nid") or a.get("item_id") or "")
        title = clean(re.sub(r"<[^>]+>", "", str(a.get("raw_title") or a.get("title") or "")))
        if not pid or not title:
            return None
        offer = RawOffer(
            site_key=self.key,
            site_product_id=pid,
            url=self.item_url_template.format(id=pid),
            title=title,
            currency="CNY",
            price_min=_as_float(a.get("view_price")),
            seller_name=clean(str(a.get("nick") or "")) or None,
            shipping_from=clean(str(a.get("item_loc") or "")) or None,
            orders_count=self.parse_int(str(a.get("view_sales") or "")),
            raw={"source": "g_page_config"},
        )
        if a.get("pic_url"):
            offer.image_urls.append(_https(str(a["pic_url"])))
        return offer


# ----------------------------------------------------------------------- tmall


class TmallAdapter(TaobaoAdapter):
    key = "tmall"
    name = "Tmall"
    base_url = "https://www.tmall.com"
    login_url = "https://login.taobao.com/member/login.jhtml"
    item_url_template = "https://detail.tmall.com/item.htm?id={id}"

    def _page_url(self, keyword: str, page: int) -> str:
        # tmall=true restricts Taobao search to Tmall (brand-authorized) sellers.
        return (
            f"https://list.tmall.com/search_product.htm?q={quote_plus(keyword)}"
            f"&s={(page - 1) * 60}"
        )

    result_selector = ".product, .product-iWrap, [class*='Card--doubleCard']"

    def parse_results(self, html: str) -> Iterator[RawOffer]:
        tree = HTMLParser(html)
        cards = self.select_cards(tree, self.result_selector)
        if not cards:
            yield from super().parse_results(html)
            return
        for card in cards:
            try:
                link = card.css_first("a[href*='detail.tmall.com'], a[href*='id=']")
                href = (link.attributes.get("href") if link else "") or ""
                m = re.search(r"[?&]id=(\d+)", href)
                if not m:
                    continue
                pid = m.group(1)
                title = clean(
                    self.text_of(card, ".productTitle, [class*='title']")
                    or (link.attributes.get("title") if link else "")
                )
                if not title:
                    continue
                pmin, pmax, ccy = parse_price(
                    self.text_of(card, ".productPrice, [class*='price']"), "CNY"
                )
                offer = RawOffer(
                    site_key=self.key,
                    site_product_id=pid,
                    url=self.item_url_template.format(id=pid),
                    title=title,
                    currency=ccy or "CNY",
                    price_min=pmin,
                    price_max=pmax,
                    seller_name=clean(self.text_of(card, ".productShop, [class*='shop']")) or None,
                    orders_count=self.parse_int(self.text_of(card, ".productStatus, [class*='sales']")),
                    is_verified_supplier=True,  # Tmall sellers are brand-authorized
                    raw={"source": "browser"},
                )
                u = self.first_attr(card.css_first("img"))
                if u:
                    offer.image_urls.append(_https(u))
                yield offer
            except Exception as e:
                log.debug("[tmall] bad card: %s", e)


# ------------------------------------------------------------------- utilities


def _https(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    return url


def _as_float(v) -> Optional[float]:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None
