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


def sku_combinations(
    row_options: list[list[dict]], row_names: list[str], max_combinations: int,
) -> list[dict]:
    """Cross the option rows a SKU picker exposed into the combinations to price.

    Pure and Playwright-free on purpose: this is the part of variant extraction
    that has actual logic to get wrong (bounding a combinatorial explosion,
    aggregating "any option in this combo is sold out", picking which option's
    photo represents the combo) -- the surrounding click-and-read loop is just
    I/O. Each row's options come as ``{"col", "label", "sold_out", "image"}``.
    """
    if not row_options:
        return []
    import itertools

    combos = list(itertools.product(*row_options))[:max_combinations]
    return [
        {
            "attrs": {row_names[i]: opt["label"] for i, opt in enumerate(combo)},
            "sku": "|".join(opt["col"] for opt in combo),
            "cols": [opt["col"] for opt in combo],
            "sold_out": any(opt["sold_out"] for opt in combo),
            "image": next((opt["image"] for opt in combo if opt["image"]), None),
        }
        for combo in combos
    ]


class BrowserSession:
    """Thin wrapper over a persistent Playwright context."""

    def __init__(self, *, headless: Optional[bool] = None, profile_dir: Optional[str] = None,
                 slow_mo: int = 0, block_media: bool = True,
                 locale: str = "zh-CN", timezone_id: str = "Asia/Shanghai"):
        s = get_settings()
        self.headless = s.sourcehub_headless if headless is None else headless
        self.profile_dir = profile_dir or str(s.browser_profile_path)
        # Scraping doesn't need images/fonts/media -- product images are fetched
        # separately over plain HTTP, and it roughly halves page time. A *human*
        # logging in needs the real page: some sites gate their own JS
        # interactivity on webfonts finishing load, so aborting that request
        # (rather than a clean 404) can leave a page looking half-rendered with
        # click handlers that never attach -- interactive_login() turns this off.
        self.block_media = block_media
        self.proxy = s.sourcehub_proxy or None
        self.user_agent = s.sourcehub_user_agent
        self.slow_mo = slow_mo
        # Defaults match the taobao/tmall/1688/USFans traffic this was built
        # for. A non-China login (e.g. Temu via Google) needs the opposite:
        # Google's sign-in does its own geo/fingerprint consistency checks and
        # can silently hang or blank out on a browser claiming to be in
        # Shanghai while signing into an account with no China history --
        # SiteAdapter.login_locale/login_timezone override this per site.
        self.locale = locale
        self.timezone_id = timezone_id
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
            "locale": self.locale,
            "timezone_id": self.timezone_id,
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
        if self.block_media:
            # Images/fonts/media are dead weight for scraping; we fetch product
            # images separately over plain HTTP. Cuts page time roughly in half.
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

    def get_sku_variants(
        self, url: str, *, max_combinations: int = 16, timeout_ms: int = 45000,
    ) -> list[dict]:
        """Click through a product's colour/size/length picker, reading back the
        price and stock state the page shows for each combination.

        Written for AliExpress, whose product page ships no per-SKU data at all any
        more -- ``window.runParams`` is a literal ``{}`` at parse time (the page sets
        ``isCSR: true`` and fetches everything after load). The picker itself still
        renders as ordinary DOM elements (``[data-sku-row]`` / ``[data-sku-col]``),
        so the only way left to learn what the 100ft version of a listing costs is
        to actually click it, the same as a shopper would, and read what changed.

        Returns ``[]`` immediately, with no clicking, for the common case of a
        listing that has no SKU picker at all.
        """
        with self.page() as pg:
            pg.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            self._assert_not_blocked(pg)
            pg.wait_for_timeout(1500)  # let the CSR shell finish hydrating

            rows = pg.locator("[data-sku-row]")
            row_count = rows.count()
            if row_count == 0:
                return []

            row_options: list[list[dict]] = []
            row_names: list[str] = []
            for i in range(row_count):
                row = rows.nth(i)
                options = []
                cols = row.locator("[data-sku-col]")
                for j in range(cols.count()):
                    opt = cols.nth(j)
                    col = opt.get_attribute("data-sku-col")
                    if not col:
                        continue
                    label = (opt.get_attribute("title") or opt.inner_text() or "").strip()
                    if not label:
                        continue
                    cls = opt.get_attribute("class") or ""
                    img = opt.locator("img").first
                    img_url = img.get_attribute("src") if img.count() else None
                    options.append({
                        "col": col, "label": label,
                        "sold_out": "soldOut" in cls, "image": img_url,
                    })
                if not options:
                    continue
                row_options.append(options)

                wrap = row.locator("xpath=ancestor::*[contains(@class,'sku-item--wrap')]").first
                title_el = wrap.locator(
                    "[class*='sku-item--title'], [class*='sku-item--property']"
                ).first
                text = title_el.inner_text().strip() if wrap.count() and title_el.count() else ""
                row_names.append(text.split(":")[0].strip() or f"Option {len(row_names) + 1}")

            combos = sku_combinations(row_options, row_names, max_combinations)
            if not combos:
                return []

            price_sel = "[class*='price-default--current'], [class*='price--current']"
            for combo in combos:
                for col in combo["cols"]:
                    try:
                        pg.locator(f"[data-sku-col='{col}']").first.click(timeout=3000)
                        pg.wait_for_timeout(350)
                    except Exception as e:
                        log.debug("sku click failed for %s on %s: %s", col, url, e)
                price_el = pg.locator(price_sel).first
                combo["price_text"] = price_el.inner_text() if price_el.count() else None
                del combo["cols"]
            return combos

    def fetch_json(
        self, url: str, *, referer: str | None = None, headers: dict[str, str] | None = None,
        method: str = "GET", body: object = None,
    ) -> object:
        """Call a site's own XHR endpoint from inside the page, so cookies and
        anti-bot tokens (and, for a profile set up via ``agent-login``, a real
        logged-in session) are attached by the browser itself rather than by a
        plain HTTP client that was never signed in to anything.

        Cookies alone are not always the whole story: confirmed live on
        USFans, whose own ``/api/goods/info`` call is authenticated with an
        ``Authorization: Bearer <JWT>`` header its own JS attaches from
        localStorage, not a cookie -- ``credentials: 'include'`` never sends
        that, and the call 401s regardless of how genuinely logged-in the
        profile is. Rather than hardcode USFans' storage key, this scans
        local/session storage for anything JWT-shaped (three base64url
        segments -- ``eyJ...`` is the near-universal fingerprint, since that's
        the base64 of ``{"typ":`` or ``{"alg":``) and attaches the first match
        as a Bearer token, unless the caller already supplied one.
        """
        with self.page() as pg:
            if referer:
                pg.goto(referer, wait_until="domcontentloaded")
                self._assert_not_blocked(pg)
            return pg.evaluate(
                """async ([u, h, m, b]) => {
                    h = Object.assign({}, h || {});
                    if (!Object.keys(h).some(k => k.toLowerCase() === 'authorization')) {
                        const jwtRe = /^eyJ[\\w-]+\\.[\\w-]+\\.[\\w-]+$/;
                        const stores = [localStorage, sessionStorage];
                        outer:
                        for (const store of stores) {
                            for (let i = 0; i < store.length; i++) {
                                const raw = store.getItem(store.key(i));
                                if (!raw) continue;
                                // A token is sometimes stored bare, sometimes JSON-quoted
                                // ('"eyJ..."') or nested one level ({"token":"eyJ..."}).
                                let candidate = raw.trim();
                                if (jwtRe.test(candidate)) { h['Authorization'] = 'Bearer ' + candidate; break outer; }
                                try {
                                    const parsed = JSON.parse(candidate);
                                    if (typeof parsed === 'string' && jwtRe.test(parsed)) {
                                        h['Authorization'] = 'Bearer ' + parsed; break outer;
                                    }
                                    if (parsed && typeof parsed === 'object') {
                                        for (const v of Object.values(parsed)) {
                                            if (typeof v === 'string' && jwtRe.test(v)) {
                                                h['Authorization'] = 'Bearer ' + v; break outer;
                                            }
                                        }
                                    }
                                } catch (e) { /* not JSON, and not a bare JWT either -- skip */ }
                            }
                        }
                    }
                    const opts = {credentials: 'include', headers: h, method: m};
                    if (b !== null && b !== undefined) {
                        opts.body = JSON.stringify(b);
                        if (!Object.keys(h).some(k => k.toLowerCase() === 'content-type')) {
                            h['Content-Type'] = 'application/json';
                        }
                    }
                    const r = await fetch(u, opts);
                    const t = await r.text();
                    try { return JSON.parse(t); } catch (e) { return {__raw: t}; }
                }""",
                [url, headers or {}, method, body],
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


def interactive_login(
    start_url: str, profile_dir: str | None = None,
    locale: str = "zh-CN", timezone_id: str = "Asia/Shanghai",
) -> None:
    """Open a visible browser so a human can log in once. Cookies persist to disk."""
    sess = BrowserSession(headless=False, profile_dir=profile_dir, slow_mo=50, block_media=False,
                          locale=locale, timezone_id=timezone_id)
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
