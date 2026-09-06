"""The web surface for price watches.

The routes are thin, so the tests aim at the two things that are not: that the
form's meaning survives the trip into the database, and that the whole surface is
gated -- a watch stores a URL this server later POSTs to, which is a server-side
request forgery hole if anyone can create one.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_watchweb_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"
os.environ["SOURCEHUB_ADMIN_TOKEN"] = "test-token"

from fastapi.testclient import TestClient  # noqa: E402

from sourcehub.api.main import app  # noqa: E402
from sourcehub.db.models import CanonicalProduct, Offer, Site, Watch  # noqa: E402
from sourcehub.db.session import init_db, session_scope  # noqa: E402

AUTH = ("admin", "test-token")


def _seed() -> str:
    init_db()
    with session_scope() as session:
        site = session.query(Site).filter_by(key="wtest").one_or_none()
        if site is None:
            site = Site(key="wtest", name="Watch Test",
                        base_url="https://example.test", needs_agent=False)
            session.add(site)
            session.flush()
        product = session.query(CanonicalProduct).filter_by(slug="watch-widget").one_or_none()
        if product is None:
            product = CanonicalProduct(slug="watch-widget", title_en="Watch Widget",
                                       best_price_usd=20.0)
            session.add(product)
            session.flush()
            session.add(
                Offer(
                    site_id=site.id, canonical_id=product.id,
                    site_product_id="w1", url="https://example.test/w1",
                    title_raw="Watch Widget", price_usd=20.0,
                    landed_cost_usd=31.0, is_active=True, in_stock=True,
                )
            )
        return product.slug


def test_every_watch_route_is_gated():
    """Including the read-only one: the list page shows notification URLs."""
    slug = _seed()
    client = TestClient(app)

    for method, path, kwargs in (
        ("get", "/watches", {}),
        ("post", "/watches", {"data": {"slug": slug, "target": "5"}}),
        ("post", "/watches/1/delete", {}),
        ("post", "/watches/1/toggle", {}),
        ("post", "/watches/1/test", {}),
    ):
        r = getattr(client, method)(path, follow_redirects=False, **kwargs)
        assert r.status_code in (401, 403), (
            f"{method.upper()} {path} was reachable without the admin token "
            f"({r.status_code})"
        )
    print("  no watch route is reachable without the admin token")


def test_create_round_trips_the_form():
    slug = _seed()
    client = TestClient(app)

    r = client.post(
        "/watches",
        data={
            "slug": slug, "target": "12.50", "label": "Q3 build",
            "webhook": "https://ntfy.example.test/hook",
            "use_landed": "true", "direct_only": "true",
        },
        auth=AUTH, follow_redirects=False,
    )
    assert r.status_code == 303, f"expected a redirect, got {r.status_code}"

    with session_scope() as session:
        w = session.query(Watch).order_by(Watch.id.desc()).first()
        assert w.target_usd == 12.50
        assert w.label == "Q3 build"
        assert w.use_landed is True and w.direct_only is True
        assert w.notify_url == "https://ntfy.example.test/hook"
        # Seeded from what the watch would see now, so "cheapest since you
        # started watching" means something -- and, comparing landed cost, that
        # is the landed figure rather than the unit price.
        assert w.baseline_usd == 31.0, f"baseline was {w.baseline_usd}, expected landed 31.0"
    print("  checkboxes, target and webhook survive into the database")


def test_unchecked_boxes_stay_false():
    """HTML omits unchecked boxes entirely rather than sending false."""
    slug = _seed()
    client = TestClient(app)
    client.post("/watches", data={"slug": slug, "target": "3", "label": "unchecked"}, auth=AUTH,
                follow_redirects=False)

    with session_scope() as session:
        w = session.query(Watch).order_by(Watch.id.desc()).first()
        assert w.use_landed is False and w.direct_only is False and w.on_restock is False
    print("  omitted checkboxes are stored as false, not true")


def test_a_watch_needs_a_target_or_a_restock_trigger():
    """Otherwise it is a row that can never fire, and silently never does."""
    slug = _seed()
    client = TestClient(app)

    r = client.post("/watches", data={"slug": slug}, auth=AUTH, follow_redirects=False)
    assert r.status_code == 400, f"expected a rejection, got {r.status_code}"

    r = client.post("/watches", data={"slug": slug, "on_restock": "true", "label": "restock only"},
                    auth=AUTH, follow_redirects=False)
    assert r.status_code == 303, "a restock-only watch is legitimate and was refused"
    print("  a watch that could never fire is refused")


def test_webhook_scheme_is_checked():
    slug = _seed()
    client = TestClient(app)
    r = client.post(
        "/watches",
        data={"slug": slug, "target": "5", "label": "bad url",
              "webhook": "file:///etc/passwd"},
        auth=AUTH, follow_redirects=False,
    )
    assert r.status_code == 400, f"a non-HTTP notify URL was accepted ({r.status_code})"
    print("  only http(s) notification URLs are accepted")


def test_toggle_and_delete():
    slug = _seed()
    client = TestClient(app)
    client.post("/watches", data={"slug": slug, "target": "7", "label": "toggle me"}, auth=AUTH,
                follow_redirects=False)
    with session_scope() as session:
        wid = session.query(Watch).order_by(Watch.id.desc()).first().id

    client.post(f"/watches/{wid}/toggle", auth=AUTH, follow_redirects=False)
    with session_scope() as session:
        assert session.get(Watch, wid).enabled is False

    client.post(f"/watches/{wid}/toggle", auth=AUTH, follow_redirects=False)
    with session_scope() as session:
        assert session.get(Watch, wid).enabled is True

    client.post(f"/watches/{wid}/delete", auth=AUTH, follow_redirects=False)
    with session_scope() as session:
        assert session.get(Watch, wid) is None
    print("  pause, resume and delete work")


def test_list_page_renders():
    slug = _seed()
    client = TestClient(app)
    client.post("/watches", data={"slug": slug, "target": "6.25", "label": "listing"}, auth=AUTH,
                follow_redirects=False)
    r = client.get("/watches", auth=AUTH)
    assert r.status_code == 200
    assert "Watch Widget" in r.text and "6.25" in r.text
    print("  the watch list renders the product and its target")


def test_product_page_shows_the_watch_without_leaking_the_webhook():
    """The product page is public; the notification URL is not."""
    slug = _seed()
    client = TestClient(app)
    client.post(
        "/watches",
        data={"slug": slug, "target": "4.10", "label": "leak check",
              "webhook": "https://secret.example.test/abc123"},
        auth=AUTH, follow_redirects=False,
    )
    r = client.get(f"/product/{slug}")
    assert r.status_code == 200
    assert "4.10" in r.text, "the existing watch is not shown on the product page"
    assert "secret.example.test" not in r.text and "abc123" not in r.text, (
        "the notification URL leaked onto the public product page"
    )
    print("  the product page shows the watch but never its webhook")


def test_duplicate_label_is_explained_not_a_500():
    """(product, label) is unique -- two unlabelled watches on one item collide.

    That is an ordinary thing for a user to do, so it has to read as a message
    rather than as an integrity error escaping the route.
    """
    slug = _seed()
    client = TestClient(app)
    first = client.post("/watches", data={"slug": slug, "target": "9", "label": "dupe"},
                        auth=AUTH, follow_redirects=False)
    assert first.status_code == 303

    second = client.post("/watches", data={"slug": slug, "target": "8", "label": "dupe"},
                         auth=AUTH, follow_redirects=False)
    assert second.status_code == 400, f"expected a clean refusal, got {second.status_code}"
    assert "already a watch" in second.text

    # A different label on the same product is the supported way to keep both.
    third = client.post("/watches", data={"slug": slug, "target": "8", "label": "dupe 2"},
                        auth=AUTH, follow_redirects=False)
    assert third.status_code == 303, "a second watch with its own label was refused"
    print("  a colliding label is explained, and labelling resolves it")


if __name__ == "__main__":
    for fn in (
        test_every_watch_route_is_gated,
        test_create_round_trips_the_form,
        test_unchecked_boxes_stay_false,
        test_a_watch_needs_a_target_or_a_restock_trigger,
        test_webhook_scheme_is_checked,
        test_duplicate_label_is_explained_not_a_500,
        test_toggle_and_delete,
        test_list_page_renders,
        test_product_page_shows_the_watch_without_leaking_the_webhook,
    ):
        fn()
    print("watch routes OK")
