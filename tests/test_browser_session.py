"""BrowserSession's media-blocking toggle.

The bug this closes: images/fonts/media are aborted for every page by default
(a scraping optimization -- product images are fetched separately over plain
HTTP, and it roughly halves page load time), but interactive_login() reused the
exact same session setup for a *human* logging in by hand. A login page can look
half-rendered with dead click handlers when its own fonts are aborted rather
than cleanly failing -- some sites gate JS interactivity on webfonts finishing
load. Confirmed by a live report: both browser-login (Taobao) and agent-login
(USFans) showed pages that "loaded some but not all" with a Login button that
"does nothing" -- one shared root cause across two unrelated sites, not two
independent site-specific failures.

Mocked at the playwright.sync_api boundary so this runs with no real Chromium.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(label, got, want=True) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


class _FakeContext:
    def __init__(self):
        self.routes: list[tuple] = []
        self.init_scripts: list[str] = []

    def add_init_script(self, script):
        self.init_scripts.append(script)

    def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    def new_page(self):
        raise NotImplementedError("not needed for this test")

    def close(self):
        pass


class _FakeChromium:
    def __init__(self):
        self.launch_calls: list[dict] = []
        self.context = _FakeContext()

    def launch_persistent_context(self, **kwargs):
        self.launch_calls.append(kwargs)
        return self.context


class _FakePlaywright:
    def __init__(self):
        self.chromium = _FakeChromium()

    def stop(self):
        pass


class _FakeSyncPlaywright:
    def __init__(self):
        self.instance = _FakePlaywright()

    def start(self):
        return self.instance


def main() -> None:
    import playwright.sync_api as sync_api_module

    from sourcehub.util.browser import BrowserSession

    original = sync_api_module.sync_playwright
    fake_factory = _FakeSyncPlaywright()
    sync_api_module.sync_playwright = lambda: fake_factory
    try:
        print("default (scraping): media is blocked")
        sess = BrowserSession(profile_dir="/tmp/does-not-matter")
        sess.start()
        check("route() was registered", len(fake_factory.instance.chromium.context.routes), 1)

        print("\ninteractive login: media is NOT blocked")
        fake_factory2 = _FakeSyncPlaywright()
        sync_api_module.sync_playwright = lambda: fake_factory2
        sess2 = BrowserSession(headless=False, profile_dir="/tmp/does-not-matter",
                               slow_mo=50, block_media=False)
        sess2.start()
        check("no route() registered when block_media=False",
              len(fake_factory2.instance.chromium.context.routes), 0)

        print("\ninteractive_login() itself asks for block_media=False")
        # Confirm the wiring, not just that the parameter exists and works in
        # isolation -- this is what actually would have caught the bug.
        import inspect

        from sourcehub.util.browser import interactive_login

        src = inspect.getsource(interactive_login)
        check("interactive_login constructs its session with block_media=False",
              "block_media=False" in src)

        print("\nlocale/timezone default to zh-CN/Shanghai (taobao/tmall/1688/USFans)")
        fake_factory3 = _FakeSyncPlaywright()
        sync_api_module.sync_playwright = lambda: fake_factory3
        sess3 = BrowserSession(profile_dir="/tmp/does-not-matter")
        sess3.start()
        launch_kwargs = fake_factory3.instance.chromium.launch_calls[0]
        check("default locale", launch_kwargs["locale"], "zh-CN")
        check("default timezone", launch_kwargs["timezone_id"], "Asia/Shanghai")

        print("\nlocale/timezone are overridable (a non-China login, e.g. Temu via "
              "Google, needs the opposite -- Google's sign-in can hang or blank "
              "out on a browser claiming to be in Shanghai)")
        fake_factory4 = _FakeSyncPlaywright()
        sync_api_module.sync_playwright = lambda: fake_factory4
        sess4 = BrowserSession(profile_dir="/tmp/does-not-matter",
                               locale="en-US", timezone_id="America/New_York")
        sess4.start()
        launch_kwargs4 = fake_factory4.instance.chromium.launch_calls[0]
        check("overridden locale", launch_kwargs4["locale"], "en-US")
        check("overridden timezone", launch_kwargs4["timezone_id"], "America/New_York")

        print("\ninteractive_login() forwards locale/timezone_id to the session")
        src2 = inspect.getsource(interactive_login)
        check("interactive_login passes locale through", "locale=locale" in src2)
        check("interactive_login passes timezone_id through", "timezone_id=timezone_id" in src2)
    finally:
        sync_api_module.sync_playwright = original

    print("\n" + "=" * 60)
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(" -", f)
        sys.exit(1)
    print("browser session OK")


if __name__ == "__main__":
    main()
