"""TLS trust setup for machines running an HTTPS-inspecting antivirus or proxy.

Avast, AVG, Kaspersky, ESET, BitDefender and most corporate middleboxes terminate
every HTTPS connection and re-sign it with their own root CA. That root is installed
in the *Windows* certificate store, which is why browsers are happy -- but Python
ships its own trust store (``certifi``), which has never heard of it. The result is
a confusing failure that looks like no network at all:

    [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate

Two different fixes are needed, because the two HTTP stacks trust differently:

* ``httpx`` / ``requests`` use Python's ``ssl`` module -> ``truststore`` redirects
  them at the OS trust store, which already trusts the interceptor.
* ``curl_cffi`` is a libcurl binding and ignores Python entirely -> it needs a PEM
  bundle on disk, so we point ``CURL_CA_BUNDLE`` at certifi-plus-the-local-root.

Called once at CLI/server startup. Silent and harmless on a machine with no
interception.

**Worth knowing:** interception also defeats part of why ``curl_cffi`` is here. It
exists to present a real Chrome TLS fingerprint (JA3); when antivirus terminates the
connection and re-originates it, the marketplace sees the *antivirus* stack's
fingerprint instead. Fixing trust makes requests succeed, but the anti-bot evasion is
weakened. If a site keeps refusing you, excluding it from your antivirus's HTTPS
scanning is the fix that actually restores the fingerprint.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

LOCAL_BUNDLE = "data/ca-bundle.pem"
_APPLIED = False


def interception_hint() -> str:
    """Cheap guess at an interceptor from the environment. May be stale.

    Avast/AVG leave SSLKEYLOGFILE pointing at their filter driver -- but the variable
    survives in already-running shells after HTTPS scanning is switched off, so this
    is a hint for diagnostics only. Never warn off it: use probe_interception().
    """
    if "asw" in os.environ.get("SSLKEYLOGFILE", "").lower():
        return "Avast/AVG"
    return ""


def probe_interception(host: str = "api.frankfurter.app") -> str:
    """Actually look at the certificate being served. '' when not intercepted.

    Costs one TLS handshake, so this is called from `trust-setup`, never at startup.
    """
    import re
    import socket
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except Exception:
        return ""
    text = b" ".join(re.findall(rb"[ -~]{4,}", der)).decode("ascii", "replace")
    for name in ("Avast", "AVG", "Kaspersky", "ESET", "Bitdefender", "NOD32",
                 "Fiddler", "Charles", "mitmproxy", "Zscaler", "Netskope"):
        if name.lower() in text.lower():
            return name
    return ""


# Kept for callers written against the old name.
interception_detected = interception_hint


def build_bundle(root_pem: Path, dest: Path) -> Path:
    """Concatenate certifi with a local root CA into one PEM."""
    import certifi

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        Path(certifi.where()).read_text(encoding="utf-8")
        + "\n# --- locally added root (TLS interception) ---\n"
        + root_pem.read_text(encoding="ascii", errors="ignore"),
        encoding="utf-8",
    )
    return dest


def setup_tls(root: Path | None = None, force: bool = False) -> None:
    """Make both HTTP stacks trust the OS certificate store. Idempotent.

    ``force`` re-runs after the bundle has been created -- startup calls this before
    ``trust-setup`` has written the file, and without force the guard would keep the
    env vars unset for the rest of the process.
    """
    global _APPLIED
    if _APPLIED and not force:
        return
    _APPLIED = True

    # 1. Python-level stacks (httpx, requests, anthropic, playwright's fetches).
    try:
        import truststore

        truststore.inject_into_ssl()
        log.debug("truststore: using the OS certificate store")
    except ImportError:
        log.debug("truststore not installed; leaving Python trust as-is")
    except Exception as e:  # pragma: no cover
        log.debug("truststore injection failed: %s", e)

    # 2. libcurl (curl_cffi) needs a file, not a Python trust store.
    base = root or Path(__file__).resolve().parent.parent
    bundle = base / LOCAL_BUNDLE if root is None else Path(root)
    if bundle.exists():
        for var in ("CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
            if force or not os.environ.get(var):
                os.environ[var] = str(bundle)
        log.debug("curl CA bundle: %s", bundle)
    # Deliberately no warning here. The env-var hint goes stale the moment HTTPS
    # scanning is switched off, and crying wolf on every command is worse than
    # staying quiet -- a genuine problem surfaces as a clear verification error, and
    # `trust-setup` diagnoses it properly with a real certificate probe.
