"""Reference data: the 11 sites, the browse taxonomy, and the forwarding agents.

Idempotent -- safe to re-run on every boot. Existing rows are updated in place so
editing this file and restarting propagates changes without a migration.
"""

from __future__ import annotations

from slugify import slugify
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Category, ShippingAgent, Site

SITES = [
    dict(
        key="aliexpress",
        name="AliExpress",
        base_url="https://www.aliexpress.com",
        home_currency="USD",
        default_language="en",
        is_wholesale=False,
        notes="Retail, ships worldwide. Many items have US/local warehouses.",
    ),
    dict(
        key="alibaba",
        name="Alibaba.com",
        base_url="https://www.alibaba.com",
        home_currency="USD",
        default_language="en",
        is_wholesale=True,
        notes="B2B wholesale. MOQ and tiered pricing are the norm.",
    ),
    dict(
        key="1688",
        name="1688",
        base_url="https://www.1688.com",
        home_currency="CNY",
        default_language="zh",
        is_wholesale=True,
        needs_agent=True,
        notes="Domestic-China wholesale. Cheapest prices, but no international "
        "checkout and no English - requires a forwarding agent.",
    ),
    dict(
        key="taobao",
        name="Taobao",
        base_url="https://www.taobao.com",
        home_currency="CNY",
        default_language="zh",
        needs_agent=True,
        notes="Domestic-China consumer marketplace. Requires a forwarding agent.",
    ),
    dict(
        key="tmall",
        name="Tmall",
        base_url="https://www.tmall.com",
        home_currency="CNY",
        default_language="zh",
        needs_agent=True,
        notes="Taobao's brand-authorized storefront. Requires a forwarding agent.",
    ),
    dict(
        key="dhgate",
        name="DHgate",
        base_url="https://www.dhgate.com",
        home_currency="USD",
        default_language="en",
        is_wholesale=True,
        notes="Small-lot wholesale, ships to US directly.",
    ),
    dict(
        key="chinavasion",
        name="Chinavasion",
        base_url="https://www.chinavasion.com",
        home_currency="USD",
        default_language="en",
        is_wholesale=True,
        notes="Dropship-oriented wholesaler, single-unit orders accepted.",
    ),
    dict(
        key="globalsources",
        name="Global Sources",
        base_url="https://www.globalsources.com",
        home_currency="USD",
        default_language="en",
        is_wholesale=True,
        notes="B2B directory. Many listings quote on request rather than list a price.",
    ),
    dict(
        key="madeinchina",
        name="Made-in-China",
        base_url="https://www.made-in-china.com",
        home_currency="USD",
        default_language="en",
        is_wholesale=True,
        notes="B2B manufacturer directory with tiered FOB pricing.",
    ),
    dict(
        key="gearbest",
        name="GearBest",
        base_url="https://www.gearbest.ma",
        home_currency="USD",
        default_language="en",
        notes="Regional GearBest storefront (.ma). Catalog is much reduced from its peak.",
    ),
    dict(
        key="ebay",
        name="eBay",
        base_url="https://www.ebay.com",
        home_currency="USD",
        default_language="en",
        is_baseline=True,
        notes="US retail baseline. Used to answer whether importing is actually "
        "cheaper than buying domestically, not as a bulk sourcing option.",
    ),
    dict(
        key="lcsc",
        name="LCSC",
        base_url="https://www.lcsc.com",
        home_currency="USD",
        default_language="en",
        is_wholesale=True,
        notes="Component distributor. Publishes real manufacturer part numbers, so "
        "cross-site matching here is exact rather than fuzzy.",
    ),
    dict(
        key="octopart",
        name="Octopart",
        base_url="https://octopart.com",
        home_currency="USD",
        default_language="en",
        notes="Aggregates many component distributors behind one API. Needs a free "
        "Nexar key; without one the adapter is skipped.",
    ),
    dict(
        key="tomtop",
        name="TOMTOP",
        base_url="https://www.tomtop.com",
        home_currency="USD",
        default_language="en",
        notes="China-based retail storefront, ships to the US. Catalog overlaps "
        "Banggood, which gives the matcher more chances to find the same product.",
    ),
    dict(
        key="geekbuying",
        name="Geekbuying",
        base_url="https://www.geekbuying.com",
        home_currency="USD",
        default_language="en",
        notes="China-based retail storefront, ships to the US. Strong on e-mobility "
        "and mini PCs.",
    ),
    dict(
        key="banggood",
        name="Banggood",
        base_url="https://www.banggood.com",
        home_currency="USD",
        default_language="en",
        notes="Retail, ships to US. Frequent coupon pricing.",
    ),
]


# (slug, name, icon, [subcategories])
TAXONOMY = [
    ("electronics", "Consumer Electronics", "cpu", [
        "Earbuds & Headphones", "Speakers & Audio", "Smart Watches", "Phone Accessories",
        "Cameras & Optics", "Drones", "Projectors", "E-Readers & Tablets",
    ]),
    ("computers", "Computers & Networking", "monitor", [
        "Mini PCs & Barebones", "Laptop Parts", "Storage & SSD", "USB Hubs & Docks",
        "Keyboards & Mice", "Monitors", "Routers & Networking", "Cables & Adapters",
    ]),
    ("components", "Electronic Components", "chip", [
        "Development Boards", "Sensors & Modules", "ICs & Semiconductors",
        "Passive Components", "Connectors & Terminals", "PCB & Prototyping",
        "Batteries & Power Cells", "Displays & Panels",
    ]),
    ("lighting", "Lighting", "bulb", [
        "LED Strips", "Bulbs & Lamps", "Flashlights", "Grow Lights",
        "Stage & Party Lighting", "Automotive Lighting", "Solar Lighting",
    ]),
    ("tools", "Tools & Industrial", "wrench", [
        "Power Tools", "Hand Tools", "Soldering & Rework", "Measuring & Test Equipment",
        "3D Printers & Filament", "CNC & Laser", "Welding", "Workshop Storage",
    ]),
    ("home", "Home & Garden", "home", [
        "Kitchen & Dining", "Small Appliances", "Bedding & Textiles", "Storage & Organization",
        "Garden & Outdoor", "Cleaning", "Furniture", "Decor",
    ]),
    ("auto", "Automotive", "car", [
        "Dash Cams", "Car Electronics & Audio", "Diagnostic Tools", "Exterior Accessories",
        "Interior Accessories", "Motorcycle Parts", "EV Charging", "Tires & Wheels",
    ]),
    ("apparel", "Apparel & Accessories", "shirt", [
        "Men's Clothing", "Women's Clothing", "Shoes", "Bags & Luggage",
        "Watches", "Jewelry", "Hats & Caps", "Textiles & Fabric",
    ]),
    ("beauty", "Beauty & Personal Care", "sparkle", [
        "Skincare", "Hair Tools", "Cosmetics", "Nail Supplies",
        "Grooming Devices", "Fragrance", "Salon Equipment",
    ]),
    ("sports", "Sports & Outdoors", "bike", [
        "Cycling", "Camping & Hiking", "Fitness Equipment", "Fishing",
        "Water Sports", "Hunting & Tactical", "Team Sports",
    ]),
    ("toys", "Toys, Hobbies & Games", "gamepad", [
        "RC Vehicles", "Model Kits", "Board & Card Games", "Educational Toys",
        "Action Figures & Collectibles", "Gaming Accessories", "Party Supplies",
    ]),
    ("packaging", "Packaging & Office", "box", [
        "Shipping Supplies", "Retail Packaging", "Labels & Printing",
        "Stationery", "Office Electronics", "Custom Branded Packaging",
    ]),
    ("security", "Security & Surveillance", "shield", [
        "IP Cameras", "NVR & DVR Systems", "Access Control", "Alarms & Sensors",
        "Locks & Safes", "Video Doorbells",
    ]),
    ("health", "Health & Medical", "cross", [
        "Monitoring Devices", "Mobility Aids", "PPE & Disposables",
        "Massage & Therapy", "Lab & Dental Supplies",
    ]),
]


AGENTS = [
    dict(
        key="superbuy",
        name="Superbuy",
        home_url="https://www.superbuy.com/en/",
        url_template="https://www.superbuy.com/en/page/buy/?url={url}{ref}",
        supported_site_keys=["taobao", "tmall", "1688"],
        service_fee_note="Service fee typically 0-8% of item price; warehouse storage free ~90 days.",
        notes="Large operator, US-friendly, offers consolidated shipping and QC photos.",
        sort_order=10,
    ),
    dict(
        key="wegobuy",
        name="Wegobuy",
        home_url="https://www.wegobuy.com/en/page/index",
        url_template="https://www.wegobuy.com/en/page/buy/?from=search-input&url={url}{ref}",
        supported_site_keys=["taobao", "tmall", "1688"],
        service_fee_note="Tiered service fee, commonly ~5-8%.",
        sort_order=20,
    ),
    dict(
        key="cssbuy",
        name="CSSBuy",
        home_url="https://www.cssbuy.com/",
        url_template="https://www.cssbuy.com/item-micro-{url}{ref}",
        supported_site_keys=["taobao", "tmall", "1688"],
        service_fee_note="Flat per-item service fee, no percentage commission.",
        notes="Link template expects an item id; the app substitutes it automatically.",
        sort_order=30,
    ),
    dict(
        key="sugargoo",
        name="Sugargoo",
        home_url="https://www.sugargoo.com/",
        url_template="https://www.sugargoo.com/index/item/index.html?tp=taobao&searchlink={url}{ref}",
        supported_site_keys=["taobao", "tmall", "1688"],
        service_fee_note="Low/zero commission on most items; pays via consolidated shipping.",
        sort_order=40,
    ),
    dict(
        key="hagobuy",
        name="Hagobuy",
        home_url="https://www.hagobuy.com/",
        url_template="https://www.hagobuy.com/item/details?url={url}{ref}",
        supported_site_keys=["taobao", "tmall", "1688"],
        service_fee_note="No service fee on item price; revenue from shipping.",
        sort_order=50,
    ),
    dict(
        key="direct",
        name="Order direct (no agent needed)",
        home_url="",
        url_template="{url}",
        supported_site_keys=[
            "aliexpress", "alibaba", "dhgate", "chinavasion",
            "globalsources", "madeinchina", "gearbest", "banggood",
        ],
        service_fee_note="Site ships to the US itself.",
        sort_order=1,
    ),
]


def seed_reference_data(session: Session) -> None:
    _seed_sites(session)
    _seed_categories(session)
    _seed_agents(session)
    session.flush()


def _seed_sites(session: Session) -> None:
    existing = {s.key: s for s in session.scalars(select(Site)).all()}
    for spec in SITES:
        row = existing.get(spec["key"])
        if row is None:
            session.add(Site(**spec))
        else:
            for k, v in spec.items():
                setattr(row, k, v)


def _seed_categories(session: Session) -> None:
    existing = {(c.parent_id, c.slug): c for c in session.scalars(select(Category)).all()}

    for order, (slug, name, icon, children) in enumerate(TAXONOMY):
        top = existing.get((None, slug))
        if top is None:
            top = Category(
                slug=slug, name=name, icon=icon, parent_id=None,
                path=slug, level=0, sort_order=order,
            )
            session.add(top)
            session.flush()
            existing[(None, slug)] = top
        else:
            top.name, top.icon, top.sort_order = name, icon, order
            top.path, top.level = slug, 0

        for child_order, child_name in enumerate(children):
            child_slug = slugify(child_name)
            child = existing.get((top.id, child_slug))
            if child is None:
                child = Category(
                    slug=child_slug, name=child_name, parent_id=top.id,
                    path=f"{slug}/{child_slug}", level=1, sort_order=child_order,
                )
                session.add(child)
                session.flush()
                existing[(top.id, child_slug)] = child
            else:
                child.name = child_name
                child.path = f"{slug}/{child_slug}"
                child.level, child.sort_order = 1, child_order


def _seed_agents(session: Session) -> None:
    existing = {a.key: a for a in session.scalars(select(ShippingAgent)).all()}
    for spec in AGENTS:
        row = existing.get(spec["key"])
        if row is None:
            session.add(ShippingAgent(**spec))
        else:
            for k, v in spec.items():
                setattr(row, k, v)
