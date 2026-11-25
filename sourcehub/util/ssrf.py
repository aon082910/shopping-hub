"""Safe fetching of a user-supplied image URL.

The image search accepts any URL and the server downloads it. Without a guard that
is server-side request forgery: a caller could make the server read cloud metadata
(169.254.169.254), the Docker host, or anything on the LAN. This module allows only
http/https, refuses hosts that resolve to a non-public address, follows redirects
by hand so every hop is checked, and caps the download size.

Residual risk: DNS can change between the check and the connect (rebinding). Run
the container without access to sensitive internal services if that matters.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

MAX_BYTES = 12 * 1024 * 1024
MAX_REDIRECTS = 3
TIMEOUT = 15.0


class UnsafeURL(ValueError):
    """The URL is not allowed. The message is safe to show to the caller."""


def check_public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURL("only http and https image URLs are allowed")
    host = parsed.hostname
    if not host:
        raise UnsafeURL("the URL has no host")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError):
        raise UnsafeURL("the host name could not be resolved") from None
    if not infos:
        raise UnsafeURL("the host name could not be resolved")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise UnsafeURL("that address is not a public internet address")
    return url


def fetch_public_image(url: str) -> bytes:
    """Download ``url`` with SSRF checks on every redirect hop and a size cap."""
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0 SourceHub"}) as client:
        for _ in range(MAX_REDIRECTS + 1):
            check_public_url(url)
            with client.stream("GET", url) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = r.headers.get("location")
                    if not loc:
                        raise UnsafeURL("the redirect had no target")
                    url = urljoin(url, loc)
                    continue
                if r.status_code >= 400:
                    raise UnsafeURL(f"the image server answered HTTP {r.status_code}")
                declared = r.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_BYTES:
                    raise UnsafeURL("the image is too large")
                buf = bytearray()
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        raise UnsafeURL("the image is too large")
                return bytes(buf)
    raise UnsafeURL("too many redirects")
