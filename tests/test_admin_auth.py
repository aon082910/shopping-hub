"""Admin access control.

The admin routes mutate the catalog permanently (merging products, recording
rejections). They were reachable by anyone who could reach the port. These tests
pin the policy, including the part that is easy to get wrong: with no token set and
a non-loopback client, the app must fail *closed*.
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_auth_"))
os.environ.setdefault("SOURCEHUB_DB_URL", f"sqlite:///{(_TMP / 't.db').as_posix()}")
os.environ.setdefault("SOURCEHUB_MEDIA_DIR", str(_TMP / "media"))
os.environ.pop("SOURCEHUB_ADMIN_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

from sourcehub.api.main import app  # noqa: E402

FAILS: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


def basic(token: str) -> dict:
    raw = base64.b64encode(f"admin:{token}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def run() -> int:
    with TestClient(app, raise_server_exceptions=False) as c:
        print()
        print("no token, loopback client")
        os.environ.pop("SOURCEHUB_ADMIN_TOKEN", None)
        check("admin open on loopback", c.get("/admin").status_code, 200)
        check("public pages unaffected", c.get("/").status_code, 200)

        print()
        print("no token, remote client  (must fail closed)")
        # TestClient's default peer counts as loopback, so use a second client
        # bound to a public address.
        with TestClient(app, raise_server_exceptions=False,
                        client=("203.0.113.9", 51000)) as remote:
            r = remote.get("/admin")
            check("admin refused from a remote address", r.status_code, 403)
            check("and says why", "SOURCEHUB_ADMIN_TOKEN" in r.text, True)
            check("public pages still served remotely",
                  remote.get("/").status_code, 200)

        print()
        print("token set")
        os.environ["SOURCEHUB_ADMIN_TOKEN"] = "s3cret-token"
        check("no credentials -> 401", c.get("/admin").status_code, 401)
        check("wrong token -> 401",
              c.get("/admin", headers=basic("wrong")).status_code, 401)
        check("challenge header present",
              "Basic" in c.get("/admin").headers.get("www-authenticate", ""), True)
        check("correct token -> 200",
              c.get("/admin", headers=basic("s3cret-token")).status_code, 200)
        check("public pages still open", c.get("/").status_code, 200)
        check("public API still open", c.get("/api/stats").status_code, 200)

        print()
        print("mutating routes are gated too")
        check("review POST unauthenticated -> 401",
              c.post("/admin/review/1/reject", follow_redirects=False).status_code, 401)
        check("unblock POST unauthenticated -> 401",
              c.post("/admin/unblock/1/1", follow_redirects=False).status_code, 401)

        print()
        print("cross-origin form posts refused")
        r = c.post("/admin/review/1/reject", headers={**basic("s3cret-token"),
                                                      "origin": "https://evil.example"},
                   follow_redirects=False)
        check("foreign Origin -> 403", r.status_code, 403)
        r = c.post("/admin/unblock/999999/999999",
                   headers={**basic("s3cret-token"), "origin": "http://testserver"},
                   follow_redirects=False)
        check("same Origin allowed through", r.status_code in (303, 404), True)

        os.environ.pop("SOURCEHUB_ADMIN_TOKEN", None)

    print()
    print("=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("admin access control OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
