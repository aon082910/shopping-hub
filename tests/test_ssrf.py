"""SSRF guard on image-URL search, BOM input caps, admin gate behind a proxy."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_ssrf_"))
os.environ.setdefault("SOURCEHUB_DB_URL", f"sqlite:///{(_TMP / 't.db').as_posix()}")
os.environ.setdefault("SOURCEHUB_MEDIA_DIR", str(_TMP / "media"))
os.environ.pop("SOURCEHUB_ADMIN_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402

from sourcehub.api.main import app  # noqa: E402
from sourcehub.util.ssrf import UnsafeURL, check_public_url  # noqa: E402

FAILS: list[str] = []


def check(label, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(label)


def refused(url):
    try:
        check_public_url(url)
        return False
    except UnsafeURL:
        return True


def run() -> int:
    print("\nURL guard")
    for u in ["http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8000/admin", "http://localhost/x",
              "http://10.0.0.5/a.png", "http://192.168.1.144/a.png", "http://[::1]/a.png", "file:///etc/passwd",
              "ftp://example.com/a.png", "gopher://x/", "http:///nohost", "http://0.0.0.0/"]:
        check(f"refuses {u}", refused(u), True)

    print("\nroutes")
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.post("/api/search/image", data={"image_url": "http://169.254.169.254/latest/meta-data/"})
        check("API route refuses metadata URL with 400", r.status_code, 400)
        r = c.post("/search/image", data={"image_url": "http://127.0.0.1:1/x"})
        check("HTML route shows a plain error, not a 500", r.status_code, 200)
        check("and leaks no exception text", "Errno" in r.text or "Traceback" in r.text, False)
        r = c.post("/api/bom", json={"items": [["x", "abc"]]})
        check("bad quantity is 400, not 500", r.status_code, 400)
        r = c.post("/api/bom", json={"items": [["x", 1]] * 501})
        check("too many lines is 413", r.status_code, 413)
        r = c.post("/api/bom", json={"items": 5})
        check("non-list items is 400", r.status_code, 400)

        print("\nadmin is not trusted behind a proxy when no token is set")
        r = c.get("/admin", headers={"x-forwarded-for": "198.51.100.7"})
        check("proxied request to admin refused", r.status_code, 403)
        check("direct loopback request still allowed", c.get("/admin").status_code, 200)

    print("\n" + "=" * 62)
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        return 1
    print("ssrf guard OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
