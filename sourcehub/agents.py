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
from urllib.parse import quote, urlencode

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


BUILDERS = {
    "superbuy": _superbuy,
    "wegobuy": _wegobuy,
    "cssbuy": _cssbuy,
    "sugargoo": _sugargoo,
    "hagobuy": _hagobuy,
}


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
