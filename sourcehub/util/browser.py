"""Playwright driver for the sites that cannot be fetched over plain HTTP.

Taobao, Tmall and 1688 gate search results behind a logged-in session and an
anti-bot challenge. The workable approach is a **persistent browser profile**: you
log in by hand exactly once (``python -m sourcehub.cli browser-login --site taobao``,
solve the slider yourself), and every subsequent headless run reuses those cookies
from disk.

If a challenge appears mid-run, :class:`BrowserSession` raises ``BlockedError`` rather
than silently returning an empty page -- a silent zero looks identical to "no results"
and would quietly rot your catalog.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator, Optional

from ..config import get_settings
from .http import BlockedError

log = logging.getLogger(__name__)

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en','zh-CN']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = window.chrome || {runtime: {}};
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (p) => (
  p.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : origQuery(p)
);
"""

CHALLENGE_SELECTORS = [
    "#nc_1_wrapper", ".nc-container", "#nocaptcha",
    ".J_MIDDLEWARE_FRAME_WIDGET", "#login-form", ".login-blocks",
    "#baxia-dialog-content", ".baxia-dialog",
]

CHALLENGE_URL_MARKERS = ("login.taobao.com", "login.1688.com", "punish", "_____tmd_____",
                         "sec.taobao.com", "captcha")


class BrowserUnavailable(RuntimeError):
    pass


class BrowserSession:
    """Thin wrapper over a persistent Playwright context."""

    def __init__(self, *, headless: Optional[bool] = None, profile_dir: Optional[str] = None,
                 slow_mo: int = 0):
        s = get_settings()
        self.headless = s.sourcehub_headless if headless is None else headless
        self.profile_dir = profile_dir or str(s.browser_profile_path)
        self.proxy = s.sourcehub_proxy or None
        self.user_agent = s.sourcehub_user_agent
        self.slow_mo = slow_mo
        self._pw = None
        self._ctx = None

    def start(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover
            raise BrowserUnavailable(
                "playwright is not installed. Run:\n"
                "  pip install playwright\n  playwright install chromium"
            ) from e

        self._pw = sync_playwright().start()
        launch_kwargs: dict = {
            "user_data_dir": self.profile_dir,
            "headless": self.headless,
            "slow_mo": self.slow_mo,
            "viewport": {"width": 1440, "height": 900},
            "user_agent": self.user_agent,
            "locale": "zh-CN",
            "timezone_id": "Asia/Shanghai",
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
            ],
        }
        if self.proxy:
            launch_kwargs["proxy"] = {"server": self.proxy}

        try:
            self._ctx = self._pw.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:
            # The slim image ships the playwright package but no browser binary, so
            # the import above succeeds and only the launch fails. Left raw, that
            # surfaces as an opaque Playwright error that callers swallow into
            # "0 listings" -- indistinguishable from a site changing its markup.
            msg = str(e)
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                raise BrowserUnavailable(
                    "Chromium is not installed in this environment, and this site "
                    "needs it to render its listings.\n"
                    "  Slim image: use the full image (allornothing/shopping-hub:latest), "
                    "or set render: http for this site in config.yaml.\n"
                    "  Local checkout: python -m playwright install chromium"
                ) from e
            raise
        self._ctx.add_init_script(STEALTH_JS)
        # Images/fonts/media are dead weight for scraping; we fetch product images
        # separately over plain HTTP. Cuts page time roughly in half.
        self._ctx.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_(),
        )
        return self

    def close(self) -> None:
        for obj, meth in ((self._ctx, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, meth)()
                except Exception:
                    pass
        self._ctx = self._pw = None

    def __enter__(self) -> "BrowserSession":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ pages

    @contextmanager
    def page(self):
        if self._ctx is None:
            raise BrowserUnavailable("BrowserSession.start() was not called")
        pg = self._ctx.new_page()
        try:
            yield pg
        finally:
            try:
                pg.close()
            except Exception:
                pass

    def get_html(
        self,
        url: str,
        *,
        wait_selector: str | None = None,
        wait_ms: int = 2500,
        scroll: bool = True,
        timeout_ms: int = 45000,
    ) -> str:
        """Navigate and return settled HTML, raising BlockedError on a challenge."""
        with self.page() as pg:
            pg.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            self._assert_not_blocked(pg)

            if wait_selector:
                try:
                    pg.wait_for_selector(wait_selector, timeout=timeout_ms // 2)
                except Exception:
                    log.debug("wait_selector %r never appeared on %s", wait_selector, url)

            if scroll:
                # Lazy-loaded grids need real scrolling before the cards populate.
                for _ in range(6):
                    pg.mouse.wheel(0, 1400)
                    pg.wait_for_timeout(450)

            pg.wait_for_timeout(wait_ms)
            self._assert_not_blocked(pg)
            return pg.content()

    def fetch_json(self, url: str, *, referer: str | None = None) -> object:
        """Call a site's own XHR endpoint from inside the page, so cookies and
        anti-bot tokens are attached by the browser itself."""
        with self.page() as pg:
            if referer:
                pg.goto(referer, wait_until="domcontentloaded")
                self._assert_not_blocked(pg)
            return pg.evaluate(
                """async (u) => {
                    const r = await fetch(u, {credentials: 'include'});
                    const t = await r.text();
                    try { return JSON.parse(t); } catch (e) { return {__raw: t}; }
                }""",
                url,
            )

    def _assert_not_blocked(self, pg) -> None:
        url = (pg.url or "").lower()
        if any(m in url for m in CHALLENGE_URL_MARKERS):
            raise BlockedError(f"redirected to challenge/login: {pg.url}", None, pg.url)
        for sel in CHALLENGE_SELECTORS:
            try:
                if pg.locator(sel).count() > 0 and pg.locator(sel).first.is_visible():
                    raise BlockedError(f"challenge element {sel} visible", None, pg.url)
            except BlockedError:
                raise
            except Exception:
                continue


def interactive_login(start_url: str, profile_dir: str | None = None) -> None:
    """Open a visible browser so a human can log in once. Cookies persist to disk."""
    sess = BrowserSession(headless=False, profile_dir=profile_dir, slow_mo=50)
    sess.start()
    try:
        with sess.page() as pg:
            pg.goto(start_url, wait_until="domcontentloaded", timeout=120000)
            print("\n" + "=" * 68)
            print("  A browser window is open. Log in and solve any slider captcha.")
            print("  Browse to a search results page to confirm you are through.")
            print("  Then come back here and press ENTER to save the session.")
            print("=" * 68 + "\n")
            input("  Press ENTER when logged in... ")
            print(f"  Saved to profile: {sess.profile_dir}")
    finally:
        sess.close()
