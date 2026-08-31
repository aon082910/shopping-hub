"""Capture and replay real listing HTML, so selector rot is caught by a test.

Why this exists: the largest ongoing maintenance cost in this project is these
sites changing their markup. `selftest` catches that against the *live* site, but
it needs network, it is slow, and it cannot tell you whether a change *you* made
broke parsing.

So: capture the live HTML once into tests/fixtures/<site>/, and replay it offline
in tests/test_adapters.py. What that buys, precisely:

  * refactors that break parsing fail a test immediately, offline, in CI
  * when a site does change, you re-capture and the diff shows exactly what moved
  * a fixture records what the markup looked like on a specific date

What it does NOT buy: proof that your selectors match the site *today*. A fixture
is a snapshot; only `selftest` against the live site answers that. The two are
complementary, and saying so beats implying more than is true.

Capture:  python -m sourcehub.cli selftest --site dhgate --save-fixture
Replay:   python tests/test_adapters.py
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Optional

from .config import ROOT
from .scrapers.base import SiteAdapter

log = logging.getLogger(__name__)

FIXTURE_ROOT = ROOT / "tests" / "fixtures"

SEARCH_HTML = "search.html"
DETAIL_HTML = "detail.html"
MANIFEST = "manifest.json"


def fixture_dir(site_key: str) -> Path:
    return FIXTURE_ROOT / site_key


def has_fixture(site_key: str) -> bool:
    return (fixture_dir(site_key) / SEARCH_HTML).exists()


def load_manifest(site_key: str) -> dict:
    path = fixture_dir(site_key) / MANIFEST
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_html(site_key: str, name: str) -> Optional[str]:
    path = fixture_dir(site_key) / name
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def list_fixtures() -> list[dict]:
    out: list[dict] = []
    if not FIXTURE_ROOT.exists():
        return out
    for d in sorted(FIXTURE_ROOT.iterdir()):
        if not d.is_dir() or not (d / SEARCH_HTML).exists():
            continue
        manifest = load_manifest(d.name)
        out.append({
            "site": d.name,
            "captured_at": manifest.get("captured_at", "?"),
            "keyword": manifest.get("keyword", "?"),
            "synthetic": bool(manifest.get("synthetic")),
            "has_detail": (d / DETAIL_HTML).exists(),
            "offers_parsed": manifest.get("offers_parsed"),
            "search_bytes": (d / SEARCH_HTML).stat().st_size,
        })
    return out


def capture(adapter: SiteAdapter, keyword: str, with_detail: bool = True) -> dict:
    """Fetch one listing page (and one product page) and write them to disk.

    Nothing is saved unless the search page actually came back -- a half-written
    fixture that silently parses to zero offers is worse than no fixture at all.
    """
    site_key = adapter.key
    url = adapter.search_page_url(keyword, 1)

    search_html = _get_html(adapter, url)
    if not search_html or len(search_html) < 500:
        got = len(search_html or "")
        raise RuntimeError(f"search page for {site_key} came back with only {got} bytes")

    target = fixture_dir(site_key)
    target.mkdir(parents=True, exist_ok=True)
    (target / SEARCH_HTML).write_text(search_html, encoding="utf-8")

    manifest = {
        "site": site_key,
        "keyword": keyword,
        "search_url": url,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "search_bytes": len(search_html),
        "synthetic": False,
    }

    # Parse it back straight away: a fixture the adapter cannot read is not worth
    # keeping, and finding that out now beats finding it out in CI.
    offers = list(adapter.search(keyword, max_pages=1))
    manifest["offers_parsed"] = len(offers)

    if with_detail and offers:
        try:
            detail_html = _get_html(adapter, offers[0].url)
            if detail_html and len(detail_html) > 500:
                (target / DETAIL_HTML).write_text(detail_html, encoding="utf-8")
                manifest["detail_url"] = offers[0].url
                manifest["detail_product_id"] = offers[0].site_product_id
                manifest["detail_bytes"] = len(detail_html)
        except Exception as e:
            log.warning("[%s] detail capture failed (%s); keeping the listing page only",
                        site_key, e)

    (target / MANIFEST).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest


def _get_html(adapter: SiteAdapter, url: str) -> str:
    """Fetch through whichever transport this adapter actually uses.

    Must match what ``search()`` does, or the fixture captures markup the adapter
    would never see -- a browser-rendered site saved as raw HTTP replays as an empty
    page and looks like selector rot.
    """
    drivers = getattr(adapter, "drivers", None)
    if drivers is not None and drivers[0] == "browser":
        return adapter.browser.get_html(
            url, wait_selector=getattr(adapter, "result_selector", None)
        )
    # fetch_html honours `render: browser` for the ordinary HTTP adapters.
    return adapter.fetch_html(url)


class FixtureFetcher:
    """Stands in for util.http.Fetcher, serving saved HTML instead of the network.

    Listing and product pages are told apart by URL: whatever matches the recorded
    detail URL in the manifest gets the product page, everything else the listing.
    """

    def __init__(self, search_html, detail_html=None, detail_url=None):
        self.search_html = search_html
        self.detail_html = detail_html
        self.detail_url = detail_url
        self.requests: list[str] = []

    def _is_detail(self, url: str) -> bool:
        if not self.detail_html or not self.detail_url:
            return False
        return url.split("?")[0] == self.detail_url.split("?")[0]

    def _body(self, url: str) -> str:
        return self.detail_html if self._is_detail(url) else self.search_html

    def get(self, url, params=None, headers=None, referer=None, expect_json=False):
        self.requests.append(url)
        return _FixtureResponse(url, self._body(url))

    def post(self, url, data=None, json_body=None, headers=None, referer=None):
        self.requests.append(url)
        return _FixtureResponse(url, self._body(url))

    def download(self, url, referer=None):
        raise RuntimeError("fixture replay must not hit the network for images")

    def close(self) -> None:
        pass


class _FixtureResponse:
    def __init__(self, url: str, text: str):
        self.url, self.text, self.status = url, text, 200
        self.headers: dict[str, str] = {}
        self.content = text.encode("utf-8", "replace")

    def json(self):
        return json.loads(self.text)


class FixtureBrowser:
    """Stands in for a Playwright session, for the browser-driven adapters."""

    def __init__(self, search_html, detail_html=None, detail_url=None):
        self._f = FixtureFetcher(search_html, detail_html, detail_url)
        self.requests = self._f.requests

    def get_html(self, url, **kwargs) -> str:
        self.requests.append(url)
        return self._f._body(url)

    def close(self) -> None:
        pass


def bind_fixture(adapter: SiteAdapter, site_key: Optional[str] = None) -> Optional[dict]:
    """Point an adapter at its saved fixture instead of the network.

    Returns the manifest, or None when that site has no fixture yet.
    """
    site_key = site_key or adapter.key
    search_html = load_html(site_key, SEARCH_HTML)
    if search_html is None:
        return None
    manifest = load_manifest(site_key)
    detail_html = load_html(site_key, DETAIL_HTML)
    detail_url = manifest.get("detail_url")

    adapter._fetcher = FixtureFetcher(search_html, detail_html, detail_url)
    if hasattr(adapter, "_browser"):
        adapter._browser = FixtureBrowser(search_html, detail_html, detail_url)
    return manifest
