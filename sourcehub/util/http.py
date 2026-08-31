"""HTTP fetching with per-host rate limiting, retries and TLS impersonation.

These marketplaces fingerprint the TLS handshake (JA3), not just the User-Agent, so
plain ``requests``/``httpx`` gets a 403 on AliExpress and DHgate no matter what
headers you send. When ``curl_cffi`` is installed we impersonate a real Chrome
handshake, which is what actually gets through. httpx remains the fallback.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

from ..config import get_settings

log = logging.getLogger(__name__)

try:  # optional but strongly recommended
    from curl_cffi import requests as curl_requests

    HAS_CURL_CFFI = True
except Exception:  # pragma: no cover
    curl_requests = None
    HAS_CURL_CFFI = False

import httpx

IMPERSONATE_PROFILES = ["chrome124", "chrome123", "chrome120", "edge101", "safari17_0"]

DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
    "image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


class FetchError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, url: str = ""):
        super().__init__(message)
        self.status = status
        self.url = url


class BlockedError(FetchError):
    """403 / captcha / login-wall. Distinct from transient failures so callers can
    stop hammering instead of burning their retry budget."""


@dataclass
class Response:
    url: str
    status: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""

    def json(self) -> Any:
        import json

        return json.loads(self.text)


class _ProxyPool:
    """Round-robin over SOURCEHUB_PROXY, which may hold several comma-separated URLs.

    Sustained crawling from one IP is the main reason these sites start returning
    403s, and a single proxy just moves the problem to a different address. Rotation
    is per Fetcher rather than per request so a site's cookies stay with one exit IP
    for the life of a crawl -- switching mid-session looks more suspicious than not
    rotating at all.
    """

    def __init__(self) -> None:
        self._index = 0
        self._lock = threading.Lock()

    @staticmethod
    def configured() -> list:
        raw = get_settings().sourcehub_proxy or ""
        return [p.strip() for p in raw.split(",") if p.strip()]

    def next(self):
        pool = self.configured()
        if not pool:
            return None
        with self._lock:
            proxy = pool[self._index % len(pool)]
            self._index += 1
        return proxy

    def __len__(self) -> int:
        return len(self.configured())


PROXIES = _ProxyPool()


class _HostLimiter:
    """Serializes and spaces requests per hostname across threads."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._guard = threading.Lock()

    def wait(self, host: str, delay: float) -> None:
        with self._guard:
            lock = self._locks.setdefault(host, threading.Lock())
        with lock:
            last = self._last.get(host, 0.0)
            # +/-30% jitter so the request cadence isn't machine-regular
            target = delay * random.uniform(0.7, 1.3)
            gap = time.monotonic() - last
            if gap < target:
                time.sleep(target - gap)
            self._last[host] = time.monotonic()


_LIMITER = _HostLimiter()

BLOCK_MARKERS = (
    "punish?", "_____tmd_____", "captcha", "nocaptcha", "sec.taobao",
    "login.taobao.com", "verify.aliexpress", "slide to verify", "请输入验证码",
    "访问验证", "安全验证", "unusual traffic", "access denied",
)


class Fetcher:
    """One instance per site adapter. Holds cookies for the life of a crawl run."""

    def __init__(
        self,
        *,
        delay: float = 2.0,
        timeout: float = 45.0,
        retries: int = 3,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        impersonate: str | None = None,
    ):
        s = get_settings()
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.proxy = proxy if proxy is not None else PROXIES.next()
        self.impersonate = impersonate or random.choice(IMPERSONATE_PROFILES)
        self.headers = {**DEFAULT_HEADERS, "User-Agent": s.sourcehub_user_agent}
        if headers:
            self.headers.update(headers)
        self._session: Any = None

    # -- session management -------------------------------------------------

    def _ensure_session(self) -> Any:
        if self._session is not None:
            return self._session
        if HAS_CURL_CFFI:
            self._session = curl_requests.Session(
                impersonate=self.impersonate,
                proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
                timeout=self.timeout,
            )
        else:
            self._session = httpx.Client(
                http2=True,
                follow_redirects=True,
                timeout=self.timeout,
                proxy=self.proxy,
            )
        return self._session

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- requests -----------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        referer: str | None = None,
        expect_json: bool = False,
    ) -> Response:
        return self._request("GET", url, params=params, headers=headers, referer=referer,
                             expect_json=expect_json)

    def post(
        self,
        url: str,
        *,
        data: Any = None,
        json_body: Any = None,
        headers: dict | None = None,
        referer: str | None = None,
    ) -> Response:
        return self._request("POST", url, data=data, json_body=json_body,
                             headers=headers, referer=referer)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        data: Any = None,
        json_body: Any = None,
        headers: dict | None = None,
        referer: str | None = None,
        expect_json: bool = False,
    ) -> Response:
        host = urlparse(url).netloc
        hdrs = dict(self.headers)
        if referer:
            hdrs["Referer"] = referer
            hdrs["Sec-Fetch-Site"] = "same-origin"
        if expect_json:
            hdrs["Accept"] = "application/json, text/plain, */*"
            hdrs["X-Requested-With"] = "XMLHttpRequest"
        if headers:
            hdrs.update(headers)

        if not ROBOTS.allowed(url):
            raise BlockedError(
                f"robots.txt disallows {url} (SOURCEHUB_RESPECT_ROBOTS is on)",
                None, url,
            )

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            _LIMITER.wait(host, self.delay)
            try:
                sess = self._ensure_session()
                kwargs: dict[str, Any] = {"headers": hdrs, "params": params}
                if data is not None:
                    kwargs["data"] = data
                if json_body is not None:
                    kwargs["json"] = json_body

                if HAS_CURL_CFFI:
                    resp = sess.request(method, url, allow_redirects=True, **kwargs)
                    text_body, raw, status = resp.text, resp.content, resp.status_code
                    final_url = str(resp.url)
                    resp_headers = dict(resp.headers)
                else:
                    resp = sess.request(method, url, **kwargs)
                    text_body, raw, status = resp.text, resp.content, resp.status_code
                    final_url = str(resp.url)
                    resp_headers = dict(resp.headers)

                if status in (403, 401):
                    raise BlockedError(f"HTTP {status} (blocked)", status, url)
                if status == 429:
                    raise FetchError("HTTP 429 rate limited", status, url)
                if status >= 500:
                    raise FetchError(f"HTTP {status}", status, url)
                if status >= 400:
                    raise FetchError(f"HTTP {status}", status, url)

                low = text_body[:4000].lower()
                if any(mark in low or mark in final_url.lower() for mark in BLOCK_MARKERS):
                    raise BlockedError("anti-bot challenge served", status, final_url)

                return Response(final_url, status, text_body, resp_headers, raw)

            except BlockedError as e:
                # Rotate the TLS/UA fingerprint once, then give up -- retrying a
                # captcha wall just deepens the block.
                last_exc = e
                log.warning("blocked: %s (%s)", url, e)
                self.close()
                self.impersonate = random.choice(IMPERSONATE_PROFILES)
                # A block is about the IP as often as the fingerprint, so rotate
                # both when more than one proxy is available.
                if len(PROXIES) > 1:
                    self.proxy = PROXIES.next()
                    log.info("rotated to the next proxy after a block on %s", host)
                if attempt >= 2:
                    raise
                time.sleep(self.delay * 4)
            except Exception as e:
                last_exc = e
                log.debug("fetch attempt %s/%s failed for %s: %s", attempt, self.retries, url, e)
                self.close()
                if attempt < self.retries:
                    time.sleep(min(30.0, self.delay * (2 ** attempt) + random.random()))

        raise FetchError(f"giving up on {url}: {last_exc}", None, url)

    def download(self, url: str, referer: str | None = None) -> bytes:
        hdrs = {"Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                "Sec-Fetch-Dest": "image", "Sec-Fetch-Mode": "no-cors"}
        return self._request("GET", url, headers=hdrs, referer=referer).content


class RobotsPolicy:
    """robots.txt awareness, off by default and honest about why.

    Every one of these sites disallows large parts of itself to crawlers, so
    enabling this will stop most crawling outright. That is the point: it is here so
    you can *choose* to comply, and so the choice is explicit and recorded rather
    than simply never considered.

    Set SOURCEHUB_RESPECT_ROBOTS=true to enforce. Fetches are cached per host, and a
    host whose robots.txt cannot be read is treated as allowed (the file is advisory
    and its absence is not a prohibition).
    """

    def __init__(self, enabled: bool | None = None, user_agent: str = "*"):
        import os

        if enabled is None:
            enabled = os.environ.get("SOURCEHUB_RESPECT_ROBOTS", "").lower() in (
                "1", "true", "yes",
            )
        self.enabled = enabled
        self.user_agent = user_agent
        self._cache: dict[str, Any] = {}

    def allowed(self, url: str) -> bool:
        if not self.enabled:
            return True
        from urllib.parse import urlparse
        from urllib.robotparser import RobotFileParser

        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._cache.get(origin)
        if parser is None:
            parser = RobotFileParser()
            parser.set_url(origin + "/robots.txt")
            try:
                parser.read()
            except Exception as e:
                log.debug("robots.txt unreadable for %s (%s); allowing", origin, e)
                parser = False
            self._cache[origin] = parser
        if parser is False:
            return True
        try:
            return parser.can_fetch(self.user_agent, url)
        except Exception:
            return True


ROBOTS = RobotsPolicy()


def absolute_url(base: str, href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith("//"):
        return "https:" + href
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("/"):
        p = urlparse(base)
        return f"{p.scheme}://{p.netloc}{href}"
    return base.rstrip("/") + "/" + href
