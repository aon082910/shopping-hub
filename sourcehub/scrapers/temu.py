"""Temu -- ships to the US directly, no forwarding agent needed.

STUB, deliberately: confirmed live (2026-09-07) that an anonymous session gets
redirected straight to a mandatory "Sign in / Register" wall -- not just for
search, but for the plain homepage too, within a couple of seconds of landing.
There is no anonymous path here the way there is for AliExpress/DHgate/etc.,
so search()/fetch_detail() are not implemented yet: writing selectors against
guessed markup would risk silently shipping something that looks plausible but
was never verified against a real page, which this project treats as worse
than not shipping it at all (see providers.yaml's own comments on the same
principle for USFans).

What this stub DOES provide: enough (`key`/`name`/`base_url`) for
``python -m sourcehub.cli browser-login --site temu`` to open a real,
visible browser for a one-time interactive login -- the same shared
persistent profile every other browser-driven adapter's headless crawls
reuse (see util/browser.py's BrowserSession default profile_dir), so once
logged in here, it stays logged in for real crawls too.

Next step once logged in: open a search results page in that SAME browser
window (or your own Chrome, logged into the same account) and check
DevTools' Network tab for an XHR/fetch call carrying JSON product data --
Temu is known to expose one internally (commonly something like
``/api/poppy/v1/search`` in other integrations, unconfirmed here) rather
than server-rendering cards. Report back what's actually there and this
gets filled in against real data, the same way providers.yaml's usfans
preset and scrapers/globalsources.py were built this session.
"""

from __future__ import annotations

import logging
from typing import Iterator

from .base import RawOffer, SiteAdapter

log = logging.getLogger(__name__)


class TemuAdapter(SiteAdapter):
    key = "temu"
    name = "Temu"
    base_url = "https://www.temu.com"
    home_currency = "USD"
    needs_agent = False

    def search(self, keyword: str, max_pages: int | None = None) -> Iterator[RawOffer]:
        log.warning(
            "[temu] search() not yet implemented -- this is a login-only stub. "
            "Run `browser-login --site temu`, then capture the real search "
            "endpoint/markup (see this module's docstring) before this can work."
        )
        return iter(())
