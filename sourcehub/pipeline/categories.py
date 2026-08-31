"""Mapping each site's own taxonomy onto our browse tree.

Sites disagree about categories entirely -- 1688 says "数码配件 > 蓝牙耳机",
Alibaba says "Consumer Electronics > Earphone & Headphone", Banggood says
"Electronics > Audio". We resolve a product's category by scoring the English
category breadcrumb *and* the product title against a keyword table per leaf
category, then caching the decision per (site, raw_path) in ``site_category_map``
so the same breadcrumb is never re-resolved.

The keyword table is deliberately plain data -- extend it and re-run
``python -m sourcehub.cli recategorize`` to reclassify the catalog.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import Category, CanonicalProduct, Offer, SiteCategoryMap
from ..util.text import clean, normalize_title

log = logging.getLogger(__name__)

# leaf-category slug -> discriminating keywords
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "earbuds-headphones": ["earbud", "earphone", "headphone", "headset", "tws", "airpod", "earpiece"],
    "speakers-audio": ["speaker", "soundbar", "subwoofer", "amplifier", "microphone", "audio"],
    "smart-watches": ["smartwatch", "smart watch", "fitness tracker", "band", "wearable"],
    "phone-accessories": ["phone case", "screen protector", "phone holder", "charger cable", "otg"],
    "cameras-optics": ["camera", "lens", "gopro", "telescope", "binocular", "microscope"],
    "drones": ["drone", "quadcopter", "fpv", "uav"],
    "projectors": ["projector", "beamer"],
    "e-readers-tablets": ["tablet", "e-reader", "ipad", "kindle"],

    "mini-pcs-barebones": ["mini pc", "barebone", "nuc", "thin client"],
    "laptop-parts": ["laptop battery", "laptop screen", "hinge", "keyboard replacement"],
    "storage-ssd": ["ssd", "hard drive", "hdd", "nvme", "microsd", "usb flash", "memory card"],
    "usb-hubs-docks": ["usb hub", "docking station", "dock", "type-c hub", "kvm"],
    "keyboards-mice": ["keyboard", "mouse", "mousepad", "trackball", "keycap"],
    "monitors": ["monitor", "display panel", "lcd screen"],
    "routers-networking": ["router", "switch", "access point", "network card", "poe", "modem"],
    "cables-adapters": ["cable", "adapter", "converter", "hdmi", "extension cord"],

    "development-boards": ["arduino", "raspberry pi", "esp32", "esp8266", "stm32", "dev board", "microcontroller"],
    "sensors-modules": ["sensor", "module", "gyroscope", "accelerometer", "thermistor"],
    "ics-semiconductors": ["ic chip", "mosfet", "transistor", "voltage regulator", "diode"],
    "passive-components": ["resistor", "capacitor", "inductor", "crystal oscillator"],
    "connectors-terminals": ["connector", "terminal block", "jst", "xt60", "header pin"],
    "pcb-prototyping": ["pcb", "breadboard", "perfboard", "prototype board"],
    "batteries-power-cells": ["18650", "lipo", "battery cell", "lifepo4", "battery pack", "power bank"],
    "displays-panels": ["oled", "tft", "lcd module", "e-ink", "display module"],

    "led-strips": ["led strip", "light strip", "ws2812", "cob strip", "neon strip"],
    "bulbs-lamps": ["led bulb", "lamp", "downlight", "ceiling light", "e27"],
    "flashlights": ["flashlight", "torch", "headlamp", "lantern"],
    "grow-lights": ["grow light", "plant light", "hydroponic light"],
    "stage-party-lighting": ["stage light", "disco", "laser light", "par light", "moving head"],
    "automotive-lighting": ["headlight", "led bar", "fog light", "tail light"],
    "solar-lighting": ["solar light", "solar lamp", "solar garden"],

    "power-tools": ["drill", "grinder", "impact driver", "circular saw", "power tool"],
    "hand-tools": ["screwdriver", "wrench", "plier", "hand tool", "hammer", "socket set"],
    "soldering-rework": ["soldering", "solder iron", "hot air rework", "flux", "desolder"],
    "measuring-test-equipment": ["multimeter", "oscilloscope", "caliper", "laser level", "thermal camera"],
    "3d-printers-filament": ["3d printer", "filament", "pla", "petg", "resin printer", "nozzle"],
    "cnc-laser": ["cnc", "laser engraver", "spindle", "router bit", "engraving machine"],
    "welding": ["welder", "welding", "mig", "tig", "plasma cutter"],
    "workshop-storage": ["tool box", "tool chest", "workbench", "storage case"],

    "kitchen-dining": ["kitchen", "cookware", "cutlery", "tableware", "utensil"],
    "small-appliances": ["blender", "air fryer", "kettle", "coffee maker", "vacuum cleaner"],
    "bedding-textiles": ["bedding", "duvet", "pillow", "bed sheet", "blanket"],
    "storage-organization": ["organizer", "storage box", "shelf", "rack", "hanger"],
    "garden-outdoor": ["garden", "irrigation", "planter", "lawn", "greenhouse"],
    "cleaning": ["mop", "cleaning", "detergent", "brush"],
    "furniture": ["chair", "desk", "table", "sofa", "cabinet"],
    "decor": ["wall art", "decor", "vase", "picture frame", "curtain"],

    "dash-cams": ["dash cam", "dashcam", "driving recorder", "car dvr"],
    "car-electronics-audio": ["car stereo", "car audio", "head unit", "carplay", "car amplifier"],
    "diagnostic-tools": ["obd2", "obd", "diagnostic scanner", "code reader"],
    "exterior-accessories": ["car cover", "spoiler", "roof rack", "mud flap"],
    "interior-accessories": ["seat cover", "steering wheel cover", "car mat", "phone mount car"],
    "motorcycle-parts": ["motorcycle", "scooter", "helmet", "moto"],
    "ev-charging": ["ev charger", "charging cable type 2", "wallbox", "charging station"],
    "tires-wheels": ["tire", "tyre", "wheel hub", "rim"],

    "men-s-clothing": ["men shirt", "mens jacket", "men hoodie", "men pants"],
    "women-s-clothing": ["women dress", "womens", "blouse", "skirt", "ladies"],
    "shoes": ["shoes", "sneaker", "boots", "sandal", "footwear"],
    "bags-luggage": ["backpack", "handbag", "luggage", "suitcase", "tote"],
    "watches": ["wristwatch", "quartz watch", "mechanical watch", "watch strap"],
    "jewelry": ["necklace", "bracelet", "earring", "ring", "pendant"],
    "hats-caps": ["hat", "cap", "beanie", "snapback"],
    "textiles-fabric": ["fabric", "textile", "yarn", "cloth roll"],

    "skincare": ["serum", "moisturizer", "face mask", "skincare", "sunscreen"],
    "hair-tools": ["hair dryer", "straightener", "curler", "clipper", "trimmer hair"],
    "cosmetics": ["lipstick", "eyeshadow", "foundation makeup", "mascara"],
    "nail-supplies": ["nail polish", "nail art", "gel nail", "uv lamp nail"],
    "grooming-devices": ["shaver", "epilator", "razor", "grooming"],
    "fragrance": ["perfume", "cologne", "fragrance", "essential oil"],
    "salon-equipment": ["salon chair", "beauty bed", "facial machine"],

    "cycling": ["bicycle", "bike", "cycling", "e-bike", "bike light"],
    "camping-hiking": ["tent", "sleeping bag", "camping", "hiking", "backpacking"],
    "fitness-equipment": ["dumbbell", "resistance band", "yoga mat", "treadmill", "gym"],
    "fishing": ["fishing rod", "fishing reel", "lure", "fishing line"],
    "water-sports": ["kayak", "paddle board", "swimming", "diving", "snorkel"],
    "hunting-tactical": ["tactical", "hunting", "scope", "holster", "airsoft"],
    "team-sports": ["soccer", "basketball", "football", "volleyball"],

    "rc-vehicles": ["rc car", "rc truck", "rc boat", "remote control car", "servo"],
    "model-kits": ["model kit", "scale model", "gundam", "diecast"],
    "board-card-games": ["board game", "playing cards", "puzzle", "chess"],
    "educational-toys": ["educational toy", "stem toy", "building blocks", "montessori"],
    "action-figures-collectibles": ["action figure", "collectible", "figurine", "anime figure"],
    "gaming-accessories": ["gamepad", "controller", "joystick", "gaming chair", "console"],
    "party-supplies": ["balloon", "party decoration", "banner", "confetti"],

    "shipping-supplies": ["bubble mailer", "poly mailer", "packing tape", "shipping box"],
    "retail-packaging": ["gift box", "packaging box", "pouch", "jar bottle"],
    "labels-printing": ["label printer", "thermal label", "sticker roll", "barcode"],
    "stationery": ["notebook", "pen", "pencil", "stationery", "planner"],
    "office-electronics": ["printer", "scanner", "shredder", "laminator"],
    "custom-branded-packaging": ["custom logo", "branded packaging", "custom printed"],

    "ip-cameras": ["ip camera", "wifi camera", "security camera", "ptz camera"],
    "nvr-dvr-systems": ["nvr", "dvr", "surveillance kit", "cctv system"],
    "access-control": ["access control", "rfid reader", "fingerprint lock", "turnstile"],
    "alarms-sensors": ["alarm", "motion sensor", "smoke detector", "door sensor"],
    "locks-safes": ["padlock", "safe box", "door lock", "smart lock"],
    "video-doorbells": ["doorbell", "video doorbell", "intercom"],

    "monitoring-devices": ["blood pressure", "oximeter", "thermometer", "glucose", "ecg"],
    "mobility-aids": ["wheelchair", "walker", "crutch", "mobility scooter"],
    "ppe-disposables": ["face mask", "nitrile glove", "protective suit", "ppe"],
    "massage-therapy": ["massage gun", "massager", "tens unit", "acupuncture"],
    "lab-dental-supplies": ["lab equipment", "dental", "petri", "centrifuge", "pipette"],
}

# Coarse fallbacks when nothing leaf-level hits.
TOP_LEVEL_HINTS = {
    "electronics": ["electronic", "digital", "consumer electronics", "数码"],
    "computers": ["computer", "pc", "laptop", "网络"],
    "components": ["component", "electronic parts", "元器件"],
    "lighting": ["light", "lamp", "led", "照明"],
    "tools": ["tool", "machine", "industrial", "工具"],
    "home": ["home", "household", "kitchen", "家居"],
    "auto": ["car", "auto", "vehicle", "汽车"],
    "apparel": ["apparel", "clothing", "fashion", "服装"],
    "beauty": ["beauty", "cosmetic", "personal care", "美妆"],
    "sports": ["sport", "outdoor", "运动"],
    "toys": ["toy", "game", "hobby", "玩具"],
    "packaging": ["packaging", "office", "stationery", "包装"],
    "security": ["security", "surveillance", "安防"],
    "health": ["health", "medical", "医疗"],
}


class Categorizer:
    def __init__(self, session: Session):
        self.session = session
        self._by_slug: dict[str, Category] = {
            c.slug: c for c in session.scalars(select(Category)).all()
        }
        self._cache: dict[tuple[int, str], Optional[int]] = {}

    def resolve(
        self, *, site_id: int, raw_path: str | None, title: str | None
    ) -> Optional[Category]:
        """Best category for a listing, using its breadcrumb then its title."""
        raw_path = clean(raw_path or "")
        cache_key = (site_id, raw_path)

        if raw_path and cache_key in self._cache:
            cid = self._cache[cache_key]
            return self.session.get(Category, cid) if cid else None

        if raw_path:
            mapped = self.session.scalar(
                select(SiteCategoryMap).where(
                    SiteCategoryMap.site_id == site_id,
                    SiteCategoryMap.raw_path == raw_path,
                )
            )
            if mapped is not None:
                mapped.hits += 1
                if mapped.category_id:
                    self._cache[cache_key] = mapped.category_id
                    return self.session.get(Category, mapped.category_id)

        haystack = normalize_title(f"{raw_path} {raw_path} {title or ''}")
        category, confidence = self._score(haystack)

        if raw_path:
            self.session.add(
                SiteCategoryMap(
                    site_id=site_id,
                    raw_path=raw_path[:512],
                    raw_path_en=raw_path[:512],
                    category_id=category.id if category else None,
                    confidence=confidence,
                    hits=1,
                )
            )
            self._cache[cache_key] = category.id if category else None

        return category

    def _score(self, haystack: str) -> tuple[Optional[Category], float]:
        if not haystack:
            return None, 0.0

        best_slug, best_score = None, 0.0
        for slug, keywords in CATEGORY_KEYWORDS.items():
            if slug not in self._by_slug:
                continue
            score = 0.0
            for kw in keywords:
                if kw in haystack:
                    # Longer phrases are far more specific than single words.
                    score += 1.0 + 0.35 * kw.count(" ")
            if score > best_score:
                best_slug, best_score = slug, score

        if best_slug and best_score >= 1.0:
            return self._by_slug[best_slug], min(1.0, best_score / 3.0)

        for slug, hints in TOP_LEVEL_HINTS.items():
            if slug in self._by_slug and any(h in haystack for h in hints):
                return self._by_slug[slug], 0.35

        return None, 0.0


def recount_categories(session: Session) -> None:
    """Refresh the product counters shown in the browse sidebar.

    A parent's count includes its whole subtree, which is what a user browsing
    "Consumer Electronics" expects to see.
    """
    counts = dict(
        session.execute(
            select(CanonicalProduct.category_id, func.count(CanonicalProduct.id))
            .where(CanonicalProduct.is_active.is_(True))
            .group_by(CanonicalProduct.category_id)
        ).all()
    )

    categories = session.scalars(select(Category)).all()
    by_id = {c.id: c for c in categories}

    for c in categories:
        c.product_count = int(counts.get(c.id, 0) or 0)

    for c in categories:
        if c.parent_id and c.product_count:
            parent = by_id.get(c.parent_id)
            while parent is not None:
                parent.product_count += c.product_count
                parent = by_id.get(parent.parent_id) if parent.parent_id else None


def recategorize_all(session: Session, batch: int = 500) -> int:
    """Re-run classification over every product. Use after editing the keyword table."""
    cat = Categorizer(session)
    changed = 0
    offset = 0
    while True:
        products = session.scalars(
            select(CanonicalProduct).order_by(CanonicalProduct.id).limit(batch).offset(offset)
        ).all()
        if not products:
            break
        offset += batch
        for product in products:
            offer = session.scalar(
                select(Offer).where(Offer.canonical_id == product.id).limit(1)
            )
            resolved = cat.resolve(
                site_id=offer.site_id if offer else 0,
                raw_path=offer.raw_category_path if offer else None,
                title=product.title_en,
            )
            if resolved and resolved.id != product.category_id:
                product.category_id = resolved.id
                changed += 1
        session.flush()
    recount_categories(session)
    return changed
