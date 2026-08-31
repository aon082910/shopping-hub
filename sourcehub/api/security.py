"""Access control for the admin pages.

The admin routes mutate the catalog -- merging products, recording permanent match
rejections, splitting offers apart. On localhost that is fine. The moment the app
is bound to 0.0.0.0, put behind a tunnel, or reverse-proxied, an unauthenticated
mutating POST is a hole.

Policy, in order:

* ``SOURCEHUB_ADMIN_TOKEN`` set  -> admin requires it (HTTP Basic, any username).
* unset, bound to loopback       -> allowed, because only this machine can reach it.
* unset, bound to anything else  -> **refused**, with an error telling you to set a
  token. Failing closed is the right default here: silently serving an open admin
  panel on a public interface is the outcome nobody wants.

Basic auth over plain HTTP sends the token in near-clear on every request. That is
acceptable on a LAN or behind a TLS-terminating proxy, and is called out in the
docs rather than glossed over.
"""

from __future__ import annotations

import hmac
import logging
import os

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

log = logging.getLogger(__name__)

_basic = HTTPBasic(auto_error=False)

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def admin_token() -> str:
    return os.environ.get("SOURCEHUB_ADMIN_TOKEN", "").strip()


def _is_loopback(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in LOOPBACK


def require_admin(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(_basic),
) -> None:
    """Gate an admin route. Raises 401/403 when access is not permitted."""
    token = admin_token()

    if not token:
        if _is_loopback(request):
            return
        # In a container the client is *never* loopback -- requests arrive from the
        # Docker bridge -- so this fires on every containerised deployment by
        # design. Say so explicitly, or the first Unraid user to click Admin gets a
        # bare 403 and no idea that setting one variable fixes it.
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Admin is disabled because it is reachable from a non-loopback "
            f"address ({(request.client.host if request.client else 'unknown')}) "
            "and SOURCEHUB_ADMIN_TOKEN is not set. \n\n"
            "If you are running in Docker or on Unraid this is expected: "
            "container traffic never appears as loopback. Set "
            "SOURCEHUB_ADMIN_TOKEN to any strong random string in the container "
            "variables, restart, and sign in with that string as the password "
            "(any username). \n\n"
            "Browsing, search and the API are unaffected -- only the pages that "
            "can modify the catalog are gated.",
        )

    supplied = credentials.password if credentials else ""
    # compare_digest, not ==, so a wrong token cannot be recovered by timing.
    if not supplied or not hmac.compare_digest(supplied, token):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid admin token.",
            headers={"WWW-Authenticate": 'Basic realm="SourceHub admin"'},
        )


def require_same_origin(request: Request) -> None:
    """Reject cross-site form posts to mutating admin routes.

    These are plain HTML forms with cookie-free Basic auth, so the classic CSRF
    token dance buys little; what actually matters is that a page on another origin
    cannot drive them. Browsers always send Origin on cross-site POSTs, so checking
    it covers the realistic attack while leaving curl and scripts working.
    """
    origin = request.headers.get("origin")
    if not origin:
        return
    expected = f"{request.url.scheme}://{request.url.netloc}"
    if origin.rstrip("/") != expected.rstrip("/"):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Cross-origin admin request refused (Origin: {origin}).",
        )
