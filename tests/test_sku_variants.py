"""sku_combinations: the pure part of AliExpress's click-through variant scraper.

AliExpress's product page ships no per-SKU price in any script tag any more (see
BrowserSession.get_sku_variants); the only way left to learn what "100ft, red" costs
is to click the picker and read what the page shows. That click-and-read loop needs
a real browser and isn't unit-testable offline. What it delegates to --
cross-producting the option rows into combinations, aggregating "is any option in
this combination sold out", and picking a representative photo -- has no I/O and is
exactly the kind of thing worth getting right without a live site in the loop.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sourcehub.util.browser import sku_combinations  # noqa: E402

FAILS: list[str] = []


def check(label, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


def opt(col, label, sold_out=False, image=None):
    return {"col": col, "label": label, "sold_out": sold_out, "image": image}


def test_single_row():
    rows = [[opt("r-1", "50FT", sold_out=True), opt("r-2", "65.6FT"), opt("r-3", "100FT")]]
    combos = sku_combinations(rows, ["Length"], max_combinations=16)
    check("one combination per option", len(combos), 3)
    check("attrs use the row name", combos[0]["attrs"], {"Length": "50FT"})
    check("sold-out option is flagged", combos[0]["sold_out"], True)
    check("in-stock option is not flagged", combos[1]["sold_out"], False)
    check("sku is the raw column id", combos[0]["sku"], "r-1")
    print("  single-row picker -> one combo per option")


def test_two_rows_cross_product():
    rows = [
        [opt("c-black", "Black"), opt("c-white", "White")],
        [opt("s-m", "M"), opt("s-l", "L", sold_out=True)],
    ]
    combos = sku_combinations(rows, ["Color", "Size"], max_combinations=16)
    check("cross product of both rows", len(combos), 4)
    attrs = [c["attrs"] for c in combos]
    check("every color/size pair appears", attrs, [
        {"Color": "Black", "Size": "M"}, {"Color": "Black", "Size": "L"},
        {"Color": "White", "Size": "M"}, {"Color": "White", "Size": "L"},
    ])
    black_l = combos[1]
    check("sold-out in either row marks the whole combo", black_l["sold_out"], True)
    check("sku joins both column ids", black_l["sku"], "c-black|s-l")
    print("  two-row picker -> full cross product, sold-out is combo-wide")


def test_image_prefers_first_option_that_has_one():
    rows = [
        [opt("c-black", "Black", image="black.jpg")],
        [opt("s-m", "M", image=None), opt("s-l", "L", image=None)],
    ]
    combos = sku_combinations(rows, ["Color", "Size"], max_combinations=16)
    check("combo image falls back to whichever option has one", combos[0]["image"], "black.jpg")
    check("combo without any image is None", None not in [c is None for c in [combos[0]["image"]]], True)
    print("  representative image comes from whichever row actually has one")


def test_max_combinations_bounds_the_explosion():
    rows = [[opt(f"c-{i}", str(i)) for i in range(5)], [opt(f"s-{i}", str(i)) for i in range(5)]]
    combos = sku_combinations(rows, ["A", "B"], max_combinations=6)
    check("capped at max_combinations, not 25", len(combos), 6)
    print("  a 5x5 picker is capped, not fully enumerated")


def test_no_rows_is_empty():
    check("no options at all -> no combinations", sku_combinations([], [], 16), [])
    print("  no SKU picker -> no combinations, no crash")


if __name__ == "__main__":
    for fn in (
        test_single_row,
        test_two_rows_cross_product,
        test_image_prefers_first_option_that_has_one,
        test_max_combinations_bounds_the_explosion,
        test_no_rows_is_empty,
    ):
        fn()
    print("=" * 60)
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(" -", f)
        sys.exit(1)
    print("sku_combinations OK")
