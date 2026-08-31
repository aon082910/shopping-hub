"""Parsing unit tests. No network, no database.

These cover the regex-heavy code that silently produces garbage when it's wrong --
a price parsed as 1234.56 instead of 1.23456 corrupts every comparison downstream.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sourcehub.util.money import FxConverter, parse_moq, parse_price, parse_tiers
from sourcehub.util.text import (
    codes_conflict,
    detect_lang,
    extract_model_codes,
    find_gtin,
    normalize_gtin,
    normalize_spec_key,
    normalize_title,
)

FAILS: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILS.append(f"{label}\n      got  {got!r}\n      want {want!r}")
        print(f"  FAIL  {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok    {label}")


def test_prices() -> None:
    print("\nprices")
    check("plain usd", parse_price("US $12.34"), (12.34, None, "USD"))
    check("bare dollar", parse_price("$5.99"), (5.99, None, "USD"))
    check("range", parse_price("$1.20 - $3.40"), (1.2, 3.4, "USD"))
    check("cny symbol", parse_price("￥12.50"), (12.5, None, "CNY"))
    check("cny range", parse_price("¥10.00-¥15.00"), (10.0, 15.0, "CNY"))
    check("thousands", parse_price("$1,234.56"), (1234.56, None, "USD"))
    check("european", parse_price("12,34 €"), (12.34, None, "EUR"))
    check("trailing code", parse_price("129.00 MAD"), (129.0, None, "MAD"))
    check("with unit", parse_price("US $2.10/Piece"), (2.1, None, "USD"))
    check("no price", parse_price("Negotiable"), (None, None, "USD"))
    check("empty", parse_price(None), (None, None, "USD"))
    # A bare number with no currency marker anywhere must not be read as a price.
    check("bare number", parse_price("500 pieces"), (None, None, "USD"))

    # Regression, found against live pages: the symbol table key is "US$" but the
    # lookup lower-cased *with* the space, so "US $" -- the commonest prefix on
    # AliExpress and DHgate -- never resolved a currency. It fell through to the site
    # default, which silently mislabels dollar prices on a non-USD site, and left
    # `currency` unset so the second half of a range was discarded.
    check("US-space-dollar range", parse_price("US $2.10 - 4.50"), (2.10, 4.50, "USD"))
    check("with unit suffix", parse_price("US $2.26 - 3.14/Piece"), (2.26, 3.14, "USD"))
    check("no-space variant", parse_price("US$3.99"), (3.99, None, "USD"))
    check("hk dollar", parse_price("HK$ 45.00"), (45.0, None, "HKD"))
    check("aud", parse_price("A$ 12.00"), (12.0, None, "AUD"))
    # A dollar price on a MAD-default site must stay USD, not become dirhams.
    check("explicit symbol beats site default",
          parse_price("US $5.00", "MAD"), (5.0, None, "USD"))


def test_moq() -> None:
    print("\nMOQ")
    check("explicit", parse_moq("Min. Order: 500 Pieces"), (500, "piece"))
    check("moq label", parse_moq("MOQ: 100 sets"), (100, "set"))
    check("thousands", parse_moq("Min. Order: 1,000 pieces"), (1000, "piece"))
    check("implicit", parse_moq("5 pcs / lot"), (5, "piece"))
    check("chinese", parse_moq("起批 2 件"), (2, "piece"))
    check("none", parse_moq("Free shipping"), (1, "piece"))
    check("default", parse_moq(None), (1, "piece"))


def test_tiers() -> None:
    print("\nprice ladder")
    tiers = parse_tiers("1 - 99 Pieces $2.30  100 - 999 Pieces $2.10  >=1000 Pieces $1.90")
    check("tier count", len(tiers), 3)
    if len(tiers) == 3:
        check("tier 1", (tiers[0]["min_qty"], tiers[0]["max_qty"], tiers[0]["price"]),
              (1, 99, 2.3))
        check("tier 2", (tiers[1]["min_qty"], tiers[1]["max_qty"], tiers[1]["price"]),
              (100, 999, 2.1))
        check("tier 3 price", tiers[2]["price"], 1.9)


def test_gtin() -> None:
    print("\nGTIN")
    # 0012345678905 is a valid UPC-A check digit; 0012345678900 is not.
    check("valid upc", normalize_gtin("012345678905"), "00012345678905")
    check("bad checksum", normalize_gtin("012345678900"), None)
    check("wrong length", normalize_gtin("12345"), None)
    check("non numeric", normalize_gtin("ABC-DEF"), None)
    check("found in text", find_gtin("Widget UPC 012345678905 blue"), "00012345678905")


def test_text() -> None:
    print("\ntext normalization")
    check(
        "strips marketing",
        normalize_title("2024 New Hot Sale Free Shipping Wireless Bluetooth Earbuds"),
        "wireless bluetooth earbuds",
    )
    check("lang zh", detect_lang("无线蓝牙耳机"), "zh")
    check("lang en", detect_lang("Wireless Earbuds"), "en")
    check("lang ja", detect_lang("ワイヤレスイヤホン"), "ja")
    check("spec alias", normalize_spec_key("Brand Name"), "brand")
    check("spec alias 2", normalize_spec_key("Model Number:"), "model")
    check("spec colon", normalize_spec_key("Battery Capacity"), "battery")


def test_model_codes() -> None:
    print("\nmodel codes")
    # Regression: separators must be stripped, or the same part written two ways
    # reads as a conflict and suppresses a correct match.
    check("space form", extract_model_codes("Hub PD 100W"), ["PD100W"])
    check("no-space form", extract_model_codes("Hub PD100W"), ["PD100W"])
    check("dashed form", extract_model_codes("ESP32-WROOM-32 board"), ["ESP32WROOM32"])
    check("single digit ignored", extract_model_codes("8 in 1 Type C"), [])

    check("identical codes agree", codes_conflict({"PD100W"}, {"PD100W"}), False)
    check("abbreviation agrees", codes_conflict({"PD100W"}, {"PD100"}), False)
    check("genuinely different conflict", codes_conflict({"XT60"}, {"XT90"}), True)
    check("one side missing is no conflict", codes_conflict(set(), {"XT90"}), False)


def test_select_cards() -> None:
    print()
    print("card selection")
    from selectolax.parser import HTMLParser

    from sourcehub.scrapers.base import SiteAdapter

    # Regression: selectolax yields an element once per matching *branch* of a
    # selector group, so a card matching two branches came back twice and every
    # listing on the page was silently duplicated.
    html = ('<ol>'
            '<li class="item product product-item"><a>A</a></li>'
            '<li class="item product product-item"><a>B</a></li>'
            '</ol>')
    tree = HTMLParser(html)
    check("comma css() still double-counts",
          len(tree.css("li.product-item, .item.product")), 4)
    check("select_cards does not",
          len(SiteAdapter.select_cards(tree, ["li.product-item", ".item.product"])), 2)
    check("accepts a comma string too",
          len(SiteAdapter.select_cards(tree, "li.product-item, .item.product")), 2)
    check("falls through to the next selector",
          len(SiteAdapter.select_cards(tree, [".nope", ".item.product"])), 2)
    check("empty when nothing matches",
          SiteAdapter.select_cards(tree, [".nope", ".also-nope"]), [])


def test_fx() -> None:
    print("\nFX")
    fx = FxConverter({"CNY": 7.0, "USD": 1.0})
    check("cny to usd", fx.to_usd(70.0, "CNY"), 10.0)
    check("usd passthrough", fx.to_usd(10.0, "USD"), 10.0)
    check("unknown currency", fx.to_usd(10.0, "XYZ"), None)
    check("none amount", fx.to_usd(None, "USD"), None)


def main() -> int:
    for fn in (test_prices, test_moq, test_tiers, test_gtin, test_text,
               test_model_codes, test_select_cards, test_fx):
        fn()
    print("\n" + "=" * 60)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("all parsing tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
