"""build_agent_links() and its USFans-native-URL special case.

USFans never exposes a source site's real Taobao/Tmall/1688 item id anywhere
in its own API (confirmed live, twice -- see providers.yaml's usfans preset
comment). That means an offer discovered through USFans' own search has, as
its `url`, USFans' own product page rather than a taobao.com/tmall.com/1688.com
URL -- the only link that's actually real. Wrapping that into any forwarding
agent's "paste a link" tool (including USFans' own) would double-wrap an
already-agent-hosted URL into nonsense, so build_agent_links() must detect
this and return a single direct link instead of the usual per-agent list.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_test_agentlinks_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 'test.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")

from sqlalchemy import select  # noqa: E402

from sourcehub.agents import build_agent_links  # noqa: E402
from sourcehub.db.models import Offer, Site  # noqa: E402
from sourcehub.db.session import init_db, session_scope  # noqa: E402

FAILS: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(f"{label}: got {got!r}, want {want!r}")


def check_true(label: str, got) -> None:
    check(label, bool(got), True)


def _make_offer(session, site_key: str, url: str, product_id: str) -> Offer:
    site = session.scalar(select(Site).where(Site.key == site_key))
    offer = Offer(
        site_id=site.id,
        site_product_id=product_id,
        url=url,
        title_raw="Test Item",
        currency="CNY",
        price_min=10.0,
        price_usd=1.5,
    )
    session.add(offer)
    session.flush()
    return offer


def run() -> int:
    init_db()

    with session_scope() as session:
        print("\na real taobao URL still gets the normal multi-agent list")
        real_offer = _make_offer(
            session, "taobao", "https://item.taobao.com/item.htm?id=1075605616467", "real-1"
        )
        real_links = build_agent_links(session, real_offer)
        check_true("more than one agent offered", len(real_links) > 1)
        check_true("usfans is one of them", any(l.key == "usfans" for l in real_links))
        usfans_link = next(l for l in real_links if l.key == "usfans")
        check_true("usfans link wraps the real url",
                   "item.taobao.com" in usfans_link.url and "usfans.com" in usfans_link.url)

        print("\na USFans-native product URL collapses to a single direct link")
        native_url = "https://usfans.com/product/2/foM24kFAqYlA62Uf5iEslcy0qTGZeTY8tQtuuaek8"
        native_offer = _make_offer(session, "taobao", native_url, "native-1")
        native_links = build_agent_links(session, native_offer)
        check("exactly one link", len(native_links), 1)
        check("it is the usfans agent", native_links[0].key, "usfans")
        check("linked straight to the native URL, not wrapped", native_links[0].url, native_url)

        print("\nthe bare-domain vs www. mismatch doesn't defeat the match")
        # Real captured USFans URLs have no 'www.' -- the seeded ShippingAgent's
        # home_url does. Confirms _bare_host() normalizes both sides.
        check_true("still collapsed to one link despite the www. mismatch",
                   len(native_links) == 1)

    print("\n" + "=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("agent links OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
