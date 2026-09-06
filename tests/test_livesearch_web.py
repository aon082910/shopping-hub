"""The live-search throttle as reached through the web layer.

The unit tests cover the gating itself. What is only testable from here is the
plumbing: that the request's source address actually arrives at the queue as a
client key, and that the trust-proxy switch decides whether a caller can supply
that key itself.

No crawl ever runs: the queue's worker is replaced, so submit() records the
decision and nothing touches the network.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="sourcehub_lsweb_"))
os.environ["SOURCEHUB_DB_URL"] = f"sqlite:///{(_TMP / 't.db').as_posix()}"
os.environ["SOURCEHUB_MEDIA_DIR"] = str(_TMP / "media")
os.environ["TRANSLATE_PROVIDER"] = "none"

from fastapi.testclient import TestClient  # noqa: E402

from sourcehub.api import main as api_main  # noqa: E402
from sourcehub.db.session import init_db  # noqa: E402
from sourcehub.pipeline import ondemand  # noqa: E402
from sourcehub.pipeline.ondemand import QUEUED, THROTTLED, CrawlQueue, LiveSearchPolicy  # noqa: E402

POL = LiveSearchPolicy(enabled=True, min_results=5, cooldown_hours=24, max_pages=1,
                       max_queue=100, min_chars=3, per_client_hourly=3)


def _inert_queue() -> CrawlQueue:
    """A live queue whose worker never starts -- decisions only, no crawling."""
    q = CrawlQueue()
    q._ensure_worker = lambda: None  # type: ignore[method-assign]
    ondemand.QUEUE = q
    return q


def _client() -> TestClient:
    init_db()
    return TestClient(app=api_main.app)


def _patch_policy(monkey_value: LiveSearchPolicy) -> None:
    LiveSearchPolicy.from_config = classmethod(  # type: ignore[method-assign]
        lambda cls, cfg=None: monkey_value
    )


def test_search_charges_the_requesting_address():
    _inert_queue()
    _patch_policy(POL)
    client = _client()

    outcomes = [
        client.get("/api/search", params={"q": f"gizmo {i}"}).json()["live"]
        for i in range(5)
    ]
    assert outcomes[:3] == [QUEUED] * 3, f"first three should queue: {outcomes}"
    assert outcomes[3:] == [THROTTLED] * 2, f"rest should throttle: {outcomes}"
    print("  distinct queries from one address exhaust that address's budget")


def test_forwarded_header_is_ignored_by_default():
    """Otherwise a caller mints a new identity per request and the limit is a no-op."""
    _inert_queue()
    _patch_policy(POL)
    client = _client()

    outcomes = [
        client.get(
            "/api/search",
            params={"q": f"doohickey {i}"},
            headers={"X-Forwarded-For": f"203.0.113.{i}"},
        ).json()["live"]
        for i in range(5)
    ]
    assert outcomes[3:] == [THROTTLED] * 2, (
        f"a forged X-Forwarded-For bought extra crawls: {outcomes}"
    )
    print("  a forged X-Forwarded-For does not buy extra crawls")


def test_forwarded_header_is_honoured_when_trusted():
    """Behind a real proxy every request shares one address, so it must be read."""
    _inert_queue()
    _patch_policy(POL)
    settings = api_main.get_settings()
    original = settings.sourcehub_trust_proxy
    settings.sourcehub_trust_proxy = True
    try:
        client = _client()
        outcomes = [
            client.get(
                "/api/search",
                params={"q": f"widget {i}"},
                headers={"X-Forwarded-For": f"198.51.100.{i}, 10.0.0.1"},
            ).json()["live"]
            for i in range(5)
        ]
    finally:
        settings.sourcehub_trust_proxy = original

    assert outcomes == [QUEUED] * 5, (
        f"trusted proxy: each forwarded client should get its own budget: {outcomes}"
    )
    print("  with the proxy trusted, each forwarded client gets its own budget")


if __name__ == "__main__":
    test_search_charges_the_requesting_address()
    test_forwarded_header_is_ignored_by_default()
    test_forwarded_header_is_honoured_when_trusted()
    print("live search web OK")
