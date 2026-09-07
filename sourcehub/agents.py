"""Forwarding-agent deep links for the domestic-China sites.

Why this exists: 1688, Taobao and Tmall will not ship to the US and will not take a
foreign card. The standard workaround is a **forwarding agent** ("daigou"): the agent
buys the item on your behalf into their Chinese warehouse, you consolidate several
purchases into one parcel, and they ship it internationally. Their fee structures
differ enough to matter, so the item page shows several side by side rather than
picking one for you.

Each agent accepts a product URL or item id on a "buy this link" page, and the exact
shape differs per agent and per source site -- hence the per-agent builders below
rather than one template string.

Affiliate/referral codes are read from ``.env`` (``AGENT_REF_*``) and appended only
when present, so out of the box these are clean, un-tagged links.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, urlencode, urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db.models import Offer, ShippingAgent, Site

CN_SITES = {"1688", "taobao", "tmall"}


@dataclass
class AgentLink:
    key: str
    name: str
    url: str
    fee_note: Optional[str]
    home_url: str
    consolidation: bool = True
    is_direct: bool = False


def _bare_host(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def item_id_from_url(url: str, site_key: str) -> Optional[str]:
    """Extract the numeric item id an agent's URL format needs."""
    if not url:
        return None
    if site_key == "1688":
        m = re.search(r"/offer/(\d+)\.html", url)
    else:
        m = re.search(r"[?&]id=(\d+)", url) or re.search(r"/(\d{9,})\.htm", url)
    return m.group(1) if m else None


# --------------------------------------------------------------- per-agent URLs


def _superbuy(url: str, site_key: str, ref: str) -> str:
    params = {"url": url}
    if ref:
        params["partnercode"] = ref
    return "https://www.superbuy.com/en/page/buy/?" + urlencode(params)


def _wegobuy(url: str, site_key: str, ref: str) -> str:
    params = {"from": "search-input", "url": url}
    if ref:
        params["partnercode"] = ref
    return "https://www.wegobuy.com/en/page/buy/?" + urlencode(params)


def _cssbuy(url: str, site_key: str, ref: str) -> str:
    """CSSBuy addresses items by id, with a per-site prefix."""
    item_id = item_id_from_url(url, site_key)
    if not item_id:
        return "https://www.cssbuy.com/?" + urlencode({"url": url})
    prefix = {"1688": "item-micro-", "tmall": "item-tmall-", "taobao": "item-"}[site_key]
    suffix = f"?promotionCode={ref}" if ref else ""
    return f"https://www.cssbuy.com/{prefix}{item_id}{suffix}"


def _sugargoo(url: str, site_key: str, ref: str) -> str:
    platform = {"1688": "1688", "tmall": "tmall", "taobao": "taobao"}[site_key]
    params = {"tp": platform, "searchlink": url}
    if ref:
        params["memberId"] = ref
    return "https://www.sugargoo.com/index/item/index.html?" + urlencode(params)


def _hagobuy(url: str, site_key: str, ref: str) -> str:
    params = {"url": url}
    if ref:
        params["affcode"] = ref
    return "https://www.hagobuy.com/item/details?" + urlencode(params)


def _usfans(url: str, site_key: str, ref: str) -> str:
    # USFans' item resolution is an async POST from its own JS (confirmed live:
    # POST /api/goods/short-link/parser), not a GET query param a plain link can
    # trigger -- unlike the other agents here, no URL format was found that lands
    # a human straight on the resolved item. Home with the link pre-filled is the
    # honest fallback: they still have to click Search themselves.
    params = {"url": url}
    if ref:
        params["ref"] = ref
    return "https://www.usfans.com/?" + urlencode(params)


def _cnfans(url: str, site_key: str, ref: str) -> str:
    # Confirmed live: pasting a real source URL into CNFans' own search box
    # lands directly on ".../product?id=<real id>&platform=<TAOBAO|ALI_1688>
    # &productUrl=<the real url>&productPwd=" -- no resolve step, no
    # obfuscated id, unlike USFans. taobao and tmall share platform=TAOBAO.
    item_id = item_id_from_url(url, site_key) or ""
    platform = "ALI_1688" if site_key == "1688" else "TAOBAO"
    params = {"id": item_id, "platform": platform, "productUrl": url, "productPwd": ""}
    if ref:
        params["ref"] = ref
    return "https://cnfans.com/product?" + urlencode(params)


def _greetbuy(url: str, site_key: str, ref: str) -> str:
    # No URL format was found live that lands a human straight on the
    # resolved item (its own search box's "paste a link" flow didn't trigger
    # under automation, and re-running it as a plain query param just did a
    # literal keyword search instead) -- home with the link pre-filled is
    # the same honest fallback _usfans() uses for the same reason.
    params = {"url": url}
    if ref:
        params["ref"] = ref
    return "https://www.greetbuy.com/?" + urlencode(params)


def _buckydrop(url: str, site_key: str, ref: str) -> str:
    # Same honest fallback as _greetbuy() -- BuckyDrop's own search works
    # (confirmed live, see scrapers/buckydrop.py), but no direct "paste a
    # link and land on the resolved item" URL format was confirmed.
    params = {"url": url}
    if ref:
        params["ref"] = ref
    return "https://www.buckydrop.com/en/sourcing?" + urlencode(params)


def _parcelup(url: str, site_key: str, ref: str) -> str:
    # Same honest fallback -- ParcelUp's own /shop/ sits behind a genuine
    # Cloudflare Turnstile challenge (confirmed live), so there was no page
    # to even test a direct-link format against.
    params = {"url": url}
    if ref:
        params["ref"] = ref
    return "https://parcelup.com/shop/?" + urlencode(params)


BUILDERS = {
    "superbuy": _superbuy,
    "wegobuy": _wegobuy,
    "cssbuy": _cssbuy,
    "sugargoo": _sugargoo,
    "hagobuy": _hagobuy,
    "usfans": _usfans,
    "cnfans": _cnfans,
    "greetbuy": _greetbuy,
    "buckydrop": _buckydrop,
    "parcelup": _parcelup,
}


def agent_browser_session(agent_key: str):
    """A :class:`BrowserSession` bound to one forwarding agent's own persisted
    login profile, set up once with ``python -m sourcehub.cli agent-login
    --agent <key>``.

    Why this needs to exist at all: some agents show real pricing, full search
    results, or deal listings only to a signed-in account -- logged out, their
    "buy this link" tool gives a teaser, not what a human buying through them
    would actually see. The plain :class:`~..util.http.Fetcher` used everywhere
    else in this project has no session at all, so it can never reach that.

    Without a prior ``agent-login``, this is simply a fresh, logged-out browser
    profile -- it degrades to the same anonymous access the HTTP path already
    has, not an error.
    """
    from .config import get_settings
    from .util.browser import BrowserSession

    return BrowserSession(profile_dir=str(get_settings().agent_profile_path(agent_key)))


# ------------------------------------------------------------------- public API


def build_agent_links(session: Session, offer: Offer) -> list[AgentLink]:
    """Ordering options for one offer.

    For US-shipping sites this is a single "order direct" link. For the three
    domestic-China sites it is the list of enabled forwarding agents.
    """
    site = session.get(Site, offer.site_id)
    if site is None:
        return []

    settings = get_settings()

    if not site.needs_agent:
        return [
            AgentLink(
                key="direct",
                name=f"Order direct on {site.name}",
                url=offer.url,
                fee_note="Ships to the US directly - no agent needed.",
                home_url=site.base_url,
                consolidation=False,
                is_direct=True,
            )
        ]

    agents = session.scalars(
        select(ShippingAgent)
        .where(ShippingAgent.enabled.is_(True))
        .order_by(ShippingAgent.sort_order)
    ).all()

    # Some agents (USFans confirmed live) never expose the source site's real
    # item id anywhere in their own API -- deliberately, so a buyer can't cut
    # them out and go straight to Taobao. When that's the case, offer.url is
    # already that agent's own product page (see providers.yaml's usfans
    # item_url_template) rather than a taobao/tmall/1688 URL -- the only
    # dereferenceable link available at all. Wrapping an already-agent-hosted
    # URL into ANY agent's "paste a link" tool, including that same agent's
    # own, would just double-wrap nonsense, and no other agent has a real
    # source URL to act on either. Link to it directly instead.
    offer_host = _bare_host(offer.url)
    if offer_host:
        for agent in agents:
            if agent.key == "direct" or site.key not in (agent.supported_site_keys or []):
                continue
            if offer_host == _bare_host(agent.home_url):
                return [AgentLink(
                    key=agent.key, name=agent.name, url=offer.url,
                    fee_note=agent.service_fee_note, home_url=agent.home_url,
                    consolidation=agent.consolidation,
                )]

    links: list[AgentLink] = []
    for agent in agents:
        if agent.key == "direct" or site.key not in (agent.supported_site_keys or []):
            continue
        builder = BUILDERS.get(agent.key)
        if builder is None:
            # Unknown agent added via the DB: fall back to its stored template.
            url = (agent.url_template or "{url}").replace("{url}", quote(offer.url, safe="")) \
                                                  .replace("{ref}", "")
        else:
            url = builder(offer.url, site.key, settings.agent_ref(agent.key))
        links.append(
            AgentLink(
                key=agent.key,
                name=agent.name,
                url=url,
                fee_note=agent.service_fee_note,
                home_url=agent.home_url,
                consolidation=agent.consolidation,
            )
        )
    return links


def agent_notice(session: Session, offer: Offer) -> Optional[str]:
    """Warning text for the item page when an offer cannot be ordered directly."""
    site = session.get(Site, offer.site_id)
    if site is None or not site.needs_agent:
        return None
    return (
        f"{site.name} sells domestically within China only - it will not ship to the US "
        f"and does not accept most foreign cards. Use a forwarding agent below: the agent "
        f"buys the item into their warehouse for you, then ships it on. Expect the agent's "
        f"service fee, domestic China shipping (often a few dollars), and international "
        f"freight on top of the listed price."
    )


def estimate_agent_total(
    unit_price_usd: float | None,
    qty: int = 1,
    *,
    service_fee_pct: float = 0.06,
    domestic_shipping_usd: float = 1.50,
    intl_shipping_usd: float | None = None,
    weight_kg: float | None = None,
    dims_cm: tuple | None = None,
    category_path: str | None = None,
) -> Optional[dict]:
    """A rough all-in estimate for an agent order.

    International freight is estimated from weight and volume rather than assumed
    flat -- couriers bill the greater of actual and volumetric weight, so a light
    bulky item costs far more than a small heavy one and a single constant is wrong
    for both. Where the listing publishes no weight, a per-category guess is used
    and flagged as guessed.

    Still an estimate, deliberately: real freight depends on the courier, the lane
    and the agent's contract rates, none of which a listing page can tell you.
    """
    if unit_price_usd is None:
        return None

    from .pipeline.freight import load_freight_table

    goods = unit_price_usd * max(1, qty)
    service = goods * service_fee_pct

    freight = None
    if intl_shipping_usd is None:
        freight = load_freight_table().estimate(
            qty=qty, weight_kg=weight_kg, dims_cm=dims_cm, category_path=category_path
        )
        intl_shipping_usd = freight["usd"]

    total = goods + service + domestic_shipping_usd + intl_shipping_usd
    return {
        "goods": round(goods, 2),
        "service_fee": round(service, 2),
        "domestic_shipping": round(domestic_shipping_usd, 2),
        "international_shipping": round(intl_shipping_usd, 2),
        "total": round(total, 2),
        "freight": freight,
        "is_estimate": True,
    }
