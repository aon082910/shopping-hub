"""fetch_json() auto-attaches a JWT found in local/session storage.

Reported live: USFans authenticates its own /api/goods/info call with
`Authorization: Bearer <JWT>`, attached by its own page JS from a token it
keeps in localStorage -- not a cookie. fetch_json()'s `credentials: 'include'`
only ever forwarded cookies, so even a genuinely logged-in profile still 401'd.
Confirmed via the user's own browser DevTools (Network tab, real USFans
session) before this fix, and this test proves the fix generically: any site
storing a JWT-shaped string anywhere in local/session storage gets it attached
automatically, without knowing that site's specific storage key up front.

This needs a real Chromium (the thing under test is real in-page JS execution
against real Web Storage APIs, which a mock can't stand in for) -- skips
cleanly, not a failure, where none is installed. CI deliberately runs without
one (see .github/workflows/tests.yml); this is meant to be run locally after
`playwright install chromium`, the same as `selftest --save-fixture`.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(label, got, want=True) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


REAL_LOOKING_JWT = (
    "eyJhbGciOiJIUzUxMiJ9.eyJzdWIiOm51bGwsInRva2VuX3NjZW5lIjoiZGVmYXVsdCJ9."
    "Yg96n-4nF2Y9jHzVnR6nm7weGLFxShkGJnoD201jBSs_cuejQCI32gABhfip9CbM"
)


class _Handler(BaseHTTPRequestHandler):
    received: dict = {}
    # Which storage API and how the token is wrapped -- set per test via class
    # attributes so one server handles every scenario without restarting it.
    storage = "localStorage"
    wrap = "bare"  # bare | json_string | nested_object

    def do_GET(self):
        if self.path.startswith("/api/check"):
            _Handler.received["auth_header"] = self.headers.get("Authorization")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())
            return

        if self.wrap == "bare":
            value = REAL_LOOKING_JWT
        elif self.wrap == "json_string":
            value = json.dumps(REAL_LOOKING_JWT)
        else:  # nested_object, the "some_app_stores_it_like_this" case
            value = json.dumps({"token": REAL_LOOKING_JWT, "expires": 12345})

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        script = f"{self.storage}.setItem('whatever_key_this_site_happens_to_use', {value!r});"
        self.wfile.write(f"<html><body><script>{script}</script></body></html>".encode())

    def log_message(self, *args):
        pass


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as e:
        print(f"SKIP: no real Chromium available in this environment ({e}). "
              f"Run `python -m playwright install chromium` to exercise this test.")
        return

    from sourcehub.util.browser import BrowserSession

    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    port = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp_profile = Path(tempfile.mkdtemp(prefix="sourcehub_jwt_test_"))
    try:
        scenarios = [
            ("localStorage, bare string", "localStorage", "bare"),
            ("localStorage, JSON-quoted string", "localStorage", "json_string"),
            ("sessionStorage, nested under an unrelated key", "sessionStorage", "nested_object"),
        ]
        for label, storage, wrap in scenarios:
            print(f"\n{label}")
            _Handler.storage, _Handler.wrap = storage, wrap
            _Handler.received.clear()

            # Each scenario gets its own profile dir: storage from a previous
            # scenario must not leak in and produce a false pass.
            profile = tmp_profile / storage / wrap
            sess = BrowserSession(profile_dir=str(profile))
            sess.start()
            try:
                result = sess.fetch_json(
                    f"http://127.0.0.1:{port}/api/check",
                    referer=f"http://127.0.0.1:{port}/",
                )
                check("request succeeded", result, {"ok": True})
                check("JWT auto-attached as a Bearer token",
                      _Handler.received.get("auth_header"), f"Bearer {REAL_LOOKING_JWT}")
            finally:
                sess.close()

        print("\nan explicit Authorization header is never overridden")
        _Handler.storage, _Handler.wrap = "localStorage", "bare"
        _Handler.received.clear()
        sess = BrowserSession(profile_dir=str(tmp_profile / "explicit"))
        sess.start()
        try:
            sess.fetch_json(
                f"http://127.0.0.1:{port}/api/check",
                referer=f"http://127.0.0.1:{port}/",
                headers={"Authorization": "Bearer explicit-value"},
            )
            check("caller-supplied header wins over an auto-detected one",
                  _Handler.received.get("auth_header"), "Bearer explicit-value")
        finally:
            sess.close()
    finally:
        srv.shutdown()
        shutil.rmtree(tmp_profile, ignore_errors=True)

    print("\n" + "=" * 60)
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(" -", f)
        sys.exit(1)
    print("fetch_json auth-detection OK")


if __name__ == "__main__":
    main()
