"""Query expansion.

Search fails in a specific, avoidable way here: the catalog is multilingual and
machine-translated, so one product legitimately appears as "earbuds", "earphones",
"headset" and "TWS" depending on which site and which translation produced the
title. A user typing one of those got a fraction of the catalog and no hint that the
rest existed.

Expansion is **OR within a concept, AND across concepts**. Searching
"wireless earbuds" becomes (wireless OR bluetooth) AND (earbud OR earphone OR
headphone OR tws) -- which is what the user meant, rather than a bag of loosely
related words that dilutes ranking.

Groups are plain data. Add to them freely; a term may appear in several groups.
"""

from __future__ import annotations

# Each group is a set of interchangeable terms. Order is irrelevant.
SYNONYM_GROUPS: list[list[str]] = [
    ["earbud", "earbuds", "earphone", "earphones", "headphone", "headphones",
     "headset", "tws", "airpod", "airpods"],
    ["wireless", "bluetooth", "bt"],
    ["charger", "charging", "adapter", "psu", "power supply"],
    ["powerbank", "power bank", "battery pack", "portable charger"],
    ["hub", "dock", "docking station", "splitter"],
    ["laptop", "notebook"],
    ["phone", "smartphone", "mobile", "cellphone"],
    ["torch", "flashlight"],
    ["led", "l.e.d"],
    ["drone", "quadcopter", "uav", "fpv"],
    ["camera", "cam", "webcam"],
    ["dashcam", "dash cam", "car dvr", "driving recorder"],
    ["watch", "smartwatch", "wristwatch"],
    ["screwdriver", "driver bit", "bit set"],
    ["drill", "driver", "impact driver"],
    ["case", "cover", "shell", "housing"],
    ["cable", "cord", "lead", "wire"],
    ["ssd", "solid state drive"],
    ["hdd", "hard drive", "hard disk"],
    ["usb-c", "usbc", "type-c", "typec", "type c"],
    ["sneaker", "sneakers", "trainers", "running shoes"],
    ["backpack", "rucksack", "knapsack"],
    ["3d printer", "3dprinter", "fdm printer"],
    ["filament", "pla", "petg", "abs filament"],
    ["soldering", "solder", "solder iron"],
    ["multimeter", "dmm", "volt meter", "voltmeter"],
    ["cctv", "surveillance", "security camera", "ip camera"],
    ["projector", "beamer"],
    ["keyboard", "keeb", "mechanical keyboard"],
    ["mouse", "mice"],
    ["speaker", "speakers", "soundbar"],
    ["microphone", "mic"],
]


def _build_index() -> dict[str, set[str]]:
    index: dict[str, set[str]] = {}
    for group in SYNONYM_GROUPS:
        members = {t.lower().strip() for t in group if t.strip()}
        for term in members:
            index.setdefault(term, set()).update(members)
    return index


SYNONYM_INDEX: dict[str, set[str]] = _build_index()

# Longest first so "power bank" is matched before "bank".
MULTI_WORD = sorted(
    (t for t in SYNONYM_INDEX if " " in t), key=lambda t: -len(t)
)


def expand_terms(query: str) -> list[list[str]]:
    """Split a query into concepts, each expanded to its synonyms.

    Returns a list of alternatives-groups: AND across the outer list, OR within.
    Unknown words become single-item groups, so an unrecognised term still has to
    match -- expansion must never make a query looser than the user wrote it.
    """
    text = " ".join((query or "").lower().split())
    if not text:
        return []

    concepts: list[list[str]] = []
    # Consume multi-word synonyms first, replacing them with a placeholder so their
    # constituent words are not also matched as separate concepts.
    for phrase in MULTI_WORD:
        while phrase in text:
            concepts.append(sorted(SYNONYM_INDEX[phrase]))
            text = text.replace(phrase, " ", 1)

    for word in text.split():
        word = word.strip(",.;:!?\"'()")
        if not word:
            continue
        group = SYNONYM_INDEX.get(word)
        concepts.append(sorted(group) if group else [word])

    return concepts
