"""Command line interface.

    python -m sourcehub.cli --help

Common flows:
    init-db                          create tables + seed sites/categories/agents
    selftest --site aliexpress       check one adapter still parses (do this first)
    crawl --sites aliexpress,dhgate --keyword "usb c hub" --pages 2
    serve                            start the web UI on :8000
    schedule                         run the background scheduler (crawl + refresh)
    browser-login --site taobao      one-time login for the domestic-China sites
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

from sqlalchemy import func, select

from .config import get_settings, load_crawl_config
from .db.models import CanonicalProduct, Image, MatchReview, Offer, Site
from .db.search import index_product
from .db.session import init_db, session_scope
from .pipeline.categories import recategorize_all, recount_categories
from .pipeline.ingest import (
    crawl_all,
    crawl_site,
    crawl_site_categories,
    deactivate_stale,
    refresh_prices,
)
from .util.money import refresh_fx_rates


TRUST_PROBE_URL = "https://api.frankfurter.app/latest?from=USD&to=CNY"

# Raw string: the Cert: drive paths are full of backslashes.
PS_EXPORT_ROOT = r"""
$c = Get-ChildItem Cert:\LocalMachine\Root, Cert:\CurrentUser\Root |
     Where-Object {{ $_.Subject -like '*{pattern}*' }} | Select-Object -First 1
if (-not $c) {{ exit 2 }}
$b = [Convert]::ToBase64String($c.RawData, 'InsertLineBreaks')
Set-Content -Encoding ascii -Path '{dest}' -Value "-----BEGIN CERTIFICATE-----`n$b`n-----END CERTIFICATE-----"
"""


def _log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-28s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    for noisy in ("httpx", "httpcore", "urllib3", "PIL", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _csv(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


# ---------------------------------------------------------------------- commands


def cmd_init_db(args) -> int:
    init_db()
    with session_scope() as session:
        n_sites = session.scalar(select(func.count(Site.id)))
    print(f"Database ready. {n_sites} sites seeded.")
    print("Next: python -m sourcehub.cli crawl --sites aliexpress --keyword \"usb c hub\"")
    return 0


def cmd_fx(args) -> int:
    with session_scope() as session:
        n = refresh_fx_rates(session)
    print(f"Updated {n} exchange rates." if n else
          "Could not reach any FX provider; using built-in fallback rates.")
    return 0


def cmd_crawl(args) -> int:
    init_db()
    if not args.no_fx:
        with session_scope() as session:
            refresh_fx_rates(session)

    sites = _csv(args.sites) or list(load_crawl_config().enabled_sites())

    if args.categories:
        results = {}
        for key in sites:
            results[key] = crawl_site_categories(
                key,
                max_pages=args.pages,
                fetch_details=not args.no_details,
                detail_limit=args.detail_limit,
            )
    elif _csv(args.sites):
        keywords = _csv(args.keyword) or load_crawl_config().keywords
        results = {}
        for key in sites:
            results[key] = crawl_site(
                key, keywords,
                max_pages=args.pages,
                fetch_details=not args.no_details,
                detail_limit=args.detail_limit,
            )
    else:
        keywords = _csv(args.keyword) or load_crawl_config().keywords
        results = crawl_all(
            None, keywords,
            max_pages=args.pages,
            fetch_details=not args.no_details,
            detail_limit=args.detail_limit,
        )

    print("\n--- crawl summary ---")
    for key, stats in results.items():
        print(f"  {key:<15} {stats}")
    return 0


def cmd_refresh(args) -> int:
    stats = refresh_prices(
        _csv(args.sites), older_than_hours=args.older_than, limit=args.limit
    )
    print(f"Price refresh: {stats}")
    return 0


def cmd_demo_seed(args) -> int:
    from .demo import seed_demo

    n = seed_demo()
    print(f"Seeded {n} demo listings through the real pipeline.")
    print("Start the UI with:  python -m sourcehub.cli serve")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    init_db()

    if args.with_scheduler:
        # One process for both, which is what a single container wants. APScheduler's
        # BackgroundScheduler is thread-based, so it lives happily alongside uvicorn;
        # `--reload` would fork and start a second copy, hence the guard.
        if args.reload:
            print("Refusing --with-scheduler together with --reload: the reloader "
                  "would start a second scheduler and double every crawl.")
            return 1
        from .scheduler import build_scheduler

        sched = build_scheduler()
        sched.start()
        print("Scheduler started alongside the web UI:")
        for job in sched.get_jobs():
            print(f"  {job.name:<16} next run: {job.next_run_time}")

    print(f"Shopping Hub on http://{args.host}:{args.port}")
    uvicorn.run(
        "sourcehub.api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


def cmd_schedule(args) -> int:
    from .scheduler import run_scheduler

    init_db()
    run_scheduler()
    return 0


def cmd_browser_login(args) -> int:
    from .scrapers.registry import ADAPTERS, get_adapter
    from .util.browser import interactive_login

    if args.site not in ADAPTERS:
        print(f"unknown site {args.site!r}; known: {', '.join(sorted(ADAPTERS))}")
        return 1
    adapter = get_adapter(args.site)
    url = getattr(adapter, "login_url", "") or adapter.base_url
    print(f"Opening {url} for a one-time login to {adapter.name}...")
    interactive_login(url, locale=adapter.login_locale, timezone_id=adapter.login_timezone)
    print("Session saved. Headless crawls will now reuse these cookies.")
    return 0


def cmd_agent_login(args) -> int:
    """One-time interactive login to a forwarding agent's own site.

    1688/Taobao/Tmall themselves only need a login to be *crawled* at all
    (browser-login covers that). This is a different account on a different
    domain: the forwarding agent's own site, which some agents gate real
    pricing and full search behind -- what you get logged out is a teaser, not
    the number a human buying through them would actually see. Saved to its
    own persistent profile (not shared with the site-login one, or with any
    other agent), so a headless run can reuse it via
    ``agents.agent_browser_session(agent_key)``.
    """
    from .db.seed import AGENTS
    from .util.browser import interactive_login

    by_key = {a["key"]: a for a in AGENTS if a["key"] != "direct"}
    if args.list:
        print("Forwarding agents:\n")
        for key, spec in by_key.items():
            print(f"  {key:<10} {spec['name']:<12} {spec['home_url']}")
        return 0

    if not args.agent:
        print("Usage: agent-login --agent <key>  (--list to see the known agents)")
        return 1

    spec = by_key.get(args.agent)
    if spec is None:
        print(f"unknown agent {args.agent!r}. Known: {', '.join(sorted(by_key))}")
        return 1

    profile = get_settings().agent_profile_path(args.agent)
    print(f"Opening {spec['home_url']} for a one-time login to {spec['name']}...")
    print(f"(profile: {profile})")
    interactive_login(spec["home_url"], profile_dir=str(profile))
    print("Session saved. Anything reading this agent's own site through "
          "agent_browser_session() will reuse it.")
    return 0


def cmd_selftest(args) -> int:
    """Fetch a couple of pages per adapter and report what parsed.

    Selectors on these sites change without notice. Run this before blaming the
    pipeline: it tells you whether the adapter is still seeing listings at all.
    """
    from .fixtures import capture
    from .scrapers.registry import ADAPTERS, get_adapter

    keys = _csv(args.site) or list(ADAPTERS)
    keyword = args.keyword or "usb hub"
    failures = 0

    print(f"Self-test with keyword {keyword!r}\n" + "-" * 72)
    for key in keys:
        adapter = get_adapter(key)
        try:
            offers = []
            for offer in adapter.search(keyword, max_pages=1):
                offers.append(offer)
                if len(offers) >= 5:
                    break

            if not offers:
                print(f"  {key:<15} FAIL   0 listings parsed")
                failures += 1
                continue

            priced = sum(1 for o in offers if o.price_min is not None)
            imaged = sum(1 for o in offers if o.image_urls)
            print(f"  {key:<15} ok     {len(offers)} listings, "
                  f"{priced} priced, {imaged} with images")
            print(f"  {'':<15}        e.g. {offers[0].title[:58]!r}")
            if offers[0].price_min is not None:
                print(f"  {'':<15}        {offers[0].currency} {offers[0].price_min} "
                      f"MOQ {offers[0].moq}")

            if args.save_fixture:
                # Re-fetch rather than reuse the pages above: capture() also writes
                # the manifest and re-parses, which is the check that the saved file
                # is actually usable.
                try:
                    manifest = capture(adapter, keyword)
                    print(f"  {'':<15}        saved fixture: "
                          f"{manifest['search_bytes']:,}B search"
                          + (f", {manifest['detail_bytes']:,}B detail"
                             if manifest.get('detail_bytes') else "")
                          + f", {manifest['offers_parsed']} offers re-parsed")
                except Exception as e:
                    print(f"  {'':<15}        fixture capture FAILED: {e}")
                    failures += 1
        except Exception as e:
            print(f"  {key:<15} ERROR  {type(e).__name__}: {e}")
            failures += 1
        finally:
            adapter.close()

    print("-" * 72)
    print(f"{len(keys) - failures}/{len(keys)} adapters returned data.")
    if failures:
        print("\nFailures are usually one of:\n"
              "  * anti-bot block      -> set SOURCEHUB_PROXY, or install curl_cffi\n"
              "  * login required      -> python -m sourcehub.cli browser-login --site <key>\n"
              "  * markup changed      -> update the selectors in sourcehub/scrapers/<site>.py")
    return 1 if failures else 0


def cmd_image_search(args) -> int:
    """Find products from a photo, using the phashes the matcher already stores."""
    import pathlib

    from .pipeline.imagesearch import UndecodableImage, find_by_bytes, find_by_url

    with session_scope() as session:
        try:
            if args.target.startswith(("http://", "https://")):
                hits = find_by_url(session, args.target, limit=args.limit)
            else:
                data = pathlib.Path(args.target).read_bytes()
                hits = find_by_bytes(session, data, limit=args.limit)
        except UndecodableImage as e:
            print(f"ERROR: {e}")
            return 1
        except FileNotFoundError:
            print(f"ERROR: no such file: {args.target}")
            return 1

        if not hits:
            print("No visually similar products in the catalog.")
            return 0

        print(f"{len(hits)} match(es), closest first  [{hits[0].tier} index]")
        print("-" * 78)
        for h in hits:
            price = f"${h.product.best_price_usd:.2f}" if h.product.best_price_usd else "n/a"
            print(f"  d={h.distance:<3} {h.confidence:<18} {price:>9}  "
                  f"{(h.product.title_en or '')[:44]}")
            print(f"  {'':<22} /product/{h.product.slug}")
    return 0


def cmd_trust_setup(args) -> int:
    """Teach Python to trust a local HTTPS-inspecting antivirus or proxy.

    Symptom this fixes: every request dies with CERTIFICATE_VERIFY_FAILED while the
    browser works fine. That is not a network problem -- it is the interceptor's root
    CA being present in the OS store but absent from Python's.
    """
    import subprocess
    from pathlib import Path

    from .certs import build_bundle, probe_interception, setup_tls

    root_dir = Path(__file__).resolve().parent.parent
    pem = Path(args.root_pem) if args.root_pem else root_dir / "data" / "local-root.pem"

    who = probe_interception()
    print(f"HTTPS interception (live certificate probe): {who or 'none'}")
    if not who:
        print("  Nothing is intercepting TLS, so no CA workaround is needed.")
        print("  Verifying the default trust path...")
        setup_tls(force=True)
        try:
            import httpx
            from curl_cffi import requests as cr
            print(f"  httpx     -> {httpx.get(TRUST_PROBE_URL, timeout=20, follow_redirects=True).status_code}")
            print(f"  curl_cffi -> {cr.get(TRUST_PROBE_URL, impersonate='chrome124', timeout=20).status_code}")
            print()
            print("Both stacks verified against the real certificate chain.")
            return 0
        except Exception as e:
            print(f"  FAILED {type(e).__name__}: {str(e)[:110]}")
            print("  Continuing with the local-root workaround...")

    if not pem.exists():
        if sys.platform != "win32":
            print(f"No root PEM at {pem}. Export your interceptor's root CA there, "
                  "or pass --root-pem.")
            return 1
        pattern = args.subject or "Avast Web/Mail Shield Root"
        print(f"Exporting root CA matching {pattern!r} from the Windows store...")
        pem.parent.mkdir(parents=True, exist_ok=True)
        ps = PS_EXPORT_ROOT.format(pattern=pattern, dest=str(pem))
        rc = subprocess.run(["powershell", "-NoProfile", "-Command", ps]).returncode
        if rc != 0 or not pem.exists():
            print(f"No root CA matching {pattern!r} found. List candidates with:")
            print(r"  Get-ChildItem Cert:\LocalMachine\Root | "
                  r"Where-Object { $_.Subject -like '*Shield*' }")
            print("then re-run with --subject '<part of the subject>'.")
            return 1

    bundle = build_bundle(pem, root_dir / "data" / "ca-bundle.pem")
    print(f"Wrote {bundle} ({bundle.stat().st_size:,} bytes)")

    # force: startup already ran setup_tls() before this bundle existed.
    setup_tls(force=True)

    ok = True
    try:
        import httpx

        r = httpx.get(TRUST_PROBE_URL, timeout=20, follow_redirects=True)
        print(f"  httpx     -> {r.status_code}")
        ok = ok and r.status_code == 200
    except Exception as e:
        print(f"  httpx     -> FAILED {type(e).__name__}: {str(e)[:100]}")
        ok = False
    try:
        from curl_cffi import requests as cr

        r2 = cr.get(TRUST_PROBE_URL, impersonate="chrome124", timeout=20)
        print(f"  curl_cffi -> {r2.status_code}")
        ok = ok and r2.status_code == 200
    except Exception as e:
        print(f"  curl_cffi -> FAILED {type(e).__name__}: {str(e)[:100]}")
        ok = False

    if not ok:
        return 1

    print()
    print("Both HTTP stacks verified. Applied automatically from now on.")
    if who:
        print()
        print(f"Note: {who} terminates and re-signs every TLS connection, so sites see")
        print("its TLS fingerprint rather than the Chrome one curl_cffi forges. Trust is")
        print("fixed, but anti-bot evasion is weakened. If a marketplace keeps refusing")
        print("you, exclude it from your antivirus's HTTPS scanning.")
    return 0


def cmd_watch(args) -> int:
    """Add, list, remove and check price watches."""
    from sqlalchemy import select as sa_select

    from .db.models import CanonicalProduct, Watch
    from .pipeline.watch import _trigger_text, check_watches, current_price

    with session_scope() as session:
        if args.action == "list":
            rows = session.scalars(sa_select(Watch)).all()
            if not rows:
                print("No watches. Add one with:")
                print("  python -m sourcehub.cli watch add <product-slug> --target 9.99")
                return 0
            print(f"{'id':<5}{'target':>9}{'current':>10}  {'fired':>6}  product")
            print("-" * 78)
            for w in rows:
                price, site = current_price(session, w)
                product = session.get(CanonicalProduct, w.canonical_id)
                mark = " *" if price is not None and w.target_usd and price <= w.target_usd else ""
                print(f"{w.id:<5}{w.target_usd or 0:>9.2f}"
                      f"{(price if price is not None else 0):>10.2f}"
                      f"{w.trigger_count:>7}  {(product.title_en if product else '?')[:40]}{mark}")
            return 0

        if args.action == "add":
            if args.target is None and not args.restock and not args.new_site:
                print("Give --target, or --restock, or --new-site -- otherwise this "
                      "watch could never fire.")
                return 1
            product = session.scalar(
                sa_select(CanonicalProduct).where(CanonicalProduct.slug == args.slug)
            )
            if product is None:
                print(f"No product with slug {args.slug!r}.")
                return 1
            price, _ = current_price(session, Watch(canonical_id=product.id))
            watch = Watch(
                canonical_id=product.id,
                label=args.label or "",
                target_usd=args.target,
                use_landed=args.landed,
                direct_only=args.direct_only,
                on_restock=args.restock,
                on_new_site=args.new_site,
                notify_url=args.webhook,
                baseline_usd=price,
                last_price_usd=price,
            )
            if args.new_site:
                # Seeded to the sites it already has -- otherwise the first check
                # would "discover" every existing site as new.
                active = session.scalars(
                    sa_select(Offer).where(
                        Offer.canonical_id == product.id, Offer.is_active.is_(True)
                    )
                ).all()
                watch.known_site_ids = sorted({o.site_id for o in active})
            session.add(watch)
            session.flush()
            print(f"Watching {product.title_en[:60]} (id {watch.id})")
            if args.target is not None:
                print(f"  target ${args.target:.2f} against "
                      f"{'landed cost' if args.landed else 'unit price'}"
                      f"{' , direct-shipping sites only' if args.direct_only else ''}")
            if args.restock:
                print("  also alerting when it comes back in stock")
            if args.new_site:
                print("  also alerting when a new site starts carrying it")
            if price is not None:
                print(f"  currently ${price:.2f}")
            return 0

        if args.action == "remove":
            watch = session.get(Watch, int(args.slug))
            if watch is None:
                print(f"No watch with id {args.slug}.")
                return 1
            session.delete(watch)
            print(f"Removed watch {args.slug}.")
            return 0

        # check
        triggers = check_watches(session, notify=not args.no_notify)
        if not triggers:
            print("No watches triggered.")
            return 0
        for t in triggers:
            print(f"HIT  {_trigger_text(t)}")
            print(f"     /product/{t.product.slug}")
        return 0


def cmd_duty_check(args) -> int:
    """Re-verify duty.yaml's sourced rates against USITC's own live HTS rate
    table, and report any drift. Only checks entries with a known HTS line in
    duty.yaml's hts_by_category -- a rate sourced some other way (a broker,
    an agent's own reference page) has no HTS line to check against and is
    skipped, not flagged.
    """
    from .duty import check_against_usitc, load_duty_table

    table = load_duty_table()
    if not table.hts_by_category:
        print("No HTS-sourced rates configured in duty.yaml's hts_by_category -- nothing to check.")
        return 0

    results = check_against_usitc(table)
    drift_count = error_count = 0
    for r in results:
        if r["error"]:
            error_count += 1
            print(f"ERROR   {r['category']:<35} HTS {r['htsno']:<14} {r['error']}")
        elif r["drift"]:
            drift_count += 1
            print(f"DRIFT   {r['category']:<35} HTS {r['htsno']:<14} "
                  f"duty.yaml says {r['expected_rate']!r}, USITC currently says "
                  f"{r['live_rate_raw']!r} ({r['live_rate']})")
        else:
            print(f"ok      {r['category']:<35} HTS {r['htsno']:<14} "
                  f"{r['live_rate_raw']!r} (matches duty.yaml)")

    print(f"\n{len(results)} checked, {drift_count} drifted, {error_count} errors.")
    if drift_count:
        print("Update duty.yaml's by_category (and its as_of date) for anything "
              "that drifted -- USITC's own rate is authoritative for the base "
              "HTS line, though it still doesn't include Section 301/trade-"
              "remedy surcharges.")
    return 1 if drift_count or error_count else 0


def cmd_health(args) -> int:
    """Report which adapters have quietly stopped finding listings."""
    from .health import health_summary

    with session_scope() as session:
        summary = health_summary(session)

    print(f"{'site':<16}{'status':<11}{'recent':>7}{'baseline':>10}{'live':>8}  detail")
    print("-" * 92)
    for r in summary["sites"]:
        print(f"{r.site_name:<16}{r.status:<11}{r.recent_avg:>7.0f}"
              f"{r.baseline_avg:>10.0f}{r.active_offers:>8,}  {r.detail}")
    print("-" * 92)
    counts = summary["counts"]
    print("  " + "  ".join(f"{k}={v}" for k, v in counts.items() if v))

    attention = summary["attention"]
    if attention:
        print()
        print(f"{len(attention)} site(s) need attention:")
        for r in attention:
            print(f"  {r.site_name}: {r.detail}")
        print()
        print("  broken/degraded is usually a changed selector:")
        print("    python -m sourcehub.cli selftest --site <key>")
        print("  blocked is usually anti-bot or an expired login:")
        print("    set SOURCEHUB_PROXY, or browser-login --site <key>")
    # Non-zero exit so this can gate a cron job or CI check.
    return 1 if attention else 0


def cmd_bom(args) -> int:
    """Cost a parts list from a file or stdin."""
    import pathlib

    from .pipeline.export import export_bom_csv, parse_bom, price_bom

    text = (pathlib.Path(args.file).read_text(encoding="utf-8")
            if args.file != "-" else sys.stdin.read())
    entries = parse_bom(text)
    if not entries:
        print("No usable lines. One item per line, quantity optional (e.g. 'usb hub x5').")
        return 1

    with session_scope() as session:
        result = price_bom(session, entries, direct_only=args.direct_only)

        if args.csv:
            print(export_bom_csv(result), end="")
            return 0

        print(f"{'line':<34}{'need':>5}{'order':>6}  {'site':<14}{'unit':>9}{'total':>10}")
        print("-" * 80)
        for ln in result.lines:
            if not ln.matched:
                print(f"{ln.query[:33]:<34}{ln.qty:>5}{'':>6}  {'NOT FOUND':<14}")
                continue
            print(f"{ln.query[:33]:<34}{ln.qty:>5}{ln.order_qty:>6}  "
                  f"{ln.site_name[:13]:<14}{ln.unit_usd:>9.2f}{ln.line_total_usd:>10.2f}"
                  + ("  [agent]" if ln.needs_agent else ""))
            if ln.note:
                print(f"  {'':<32}{ln.note}")
        print("-" * 80)
        print(f"{'TOTAL':<60}{result.total_usd:>19.2f}")
        if result.unmatched:
            print(f"  {result.unmatched} line(s) had no priced listing.")
        if result.undisclosed_shipping:
            print(f"  {result.undisclosed_shipping} line(s) have undisclosed shipping; "
                  "the real total is higher.")
        if result.agent_lines:
            print(f"  {result.agent_lines} line(s) need a forwarding agent "
                  "(fees and freight not included).")
    return 0


def cmd_fixtures(args) -> int:
    from .fixtures import list_fixtures
    from .scrapers.registry import ADAPTERS

    rows = list_fixtures()
    if not rows:
        print("No fixtures saved yet. Capture some with:")
        print("  python -m sourcehub.cli selftest --site dhgate --save-fixture")
        return 0

    print(f"{'site':<16}{'captured':<22}{'keyword':<16}{'offers':>7}  {'detail':<7} size")
    print("-" * 78)
    for r in rows:
        flag = "  [SYNTHETIC]" if r["synthetic"] else ""
        print(f"{r['site']:<16}{r['captured_at'][:19]:<22}{str(r['keyword'])[:15]:<16}"
              f"{str(r['offers_parsed'] or '?'):>7}  {'yes' if r['has_detail'] else 'no':<7}"
              f"{r['search_bytes']:,}B{flag}")

    missing = sorted(set(ADAPTERS) - {r["site"] for r in rows})
    if missing:
        print()
        print(f"No fixture for: {', '.join(missing)}")
        print("  python -m sourcehub.cli selftest --site "
              f"{missing[0]} --save-fixture")
    return 0


def _upsert_env_vars(path: Path, updates: dict[str, str]) -> None:
    """Set KEY=value lines in a .env file, touching only those keys.

    Line-based rather than a library round-trip so every comment, blank line and
    unrelated setting in the file survives untouched -- this file is meant to be
    hand-edited too, and a rewrite that silently drops comments would be hostile.
    """
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# added by `sourcehub agent-auth`")
        lines.extend(f"{k}={v}" for k, v in remaining.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mask(secret: str) -> str:
    if len(secret) <= 8:
        return "*" * len(secret)
    return secret[:4] + "*" * (len(secret) - 8) + secret[-4:]


def cmd_agent_auth(args) -> int:
    """One-command setup for a forwarding agent's API, the credential-based route
    onto 1688/Taobao/Tmall.

    These three sites will not ship abroad or take a foreign card, so nothing
    reaches them without going through a forwarding agent one way or another.
    ``browser-login`` covers the *free* route (a human solves the agent's/site's
    login once, headless crawls reuse that browser session). This covers the
    *API* route some agents and resellers sell instead -- OTAPI, a RapidAPI
    listing, or an agent's own lookup endpoint, in providers.yaml as a preset.

    This does three things a hand-edit of .env does not: it validates the preset
    name against providers.yaml before writing anything, it verifies the key
    actually works with a real call per site rather than trusting it silently,
    and it tells you exactly what to change in config.yaml -- which this does
    NOT edit itself, since it is a hand-maintained file full of comments a
    programmatic rewrite would flatten.
    """
    from .scrapers.provider import ProviderClient, ProviderError, get_preset, load_presets
    from .util.http import Fetcher

    if args.list:
        presets = load_presets().get("providers", {})
        print("Presets in providers.yaml:\n")
        for name, spec in sorted(presets.items()):
            caps = []
            if spec.get("search"):
                caps.append("search")
            if spec.get("detail"):
                caps.append("detail")
            sites = ", ".join(sorted(spec.get("sites", {})))
            print(f"  {name:<18} [{'+'.join(caps) or 'none'}]  sites: {sites}")
        return 0

    try:
        spec = get_preset(args.preset)
    except ProviderError as e:
        print(f"ERROR: {e}")
        return 1

    auth_mode = (spec.get("auth") or {}).get("mode", "none")
    if auth_mode != "none" and not args.key:
        print(f"preset {args.preset!r} needs a key (--key). Sign up with the "
              f"provider first if you haven't -- this command validates a key, "
              f"it doesn't issue one.")
        return 1

    updates = {"CN_PROVIDER_PRESET": args.preset}
    if args.key:
        updates["CN_PROVIDER_KEY"] = args.key
    if args.base_url:
        updates["CN_PROVIDER_BASE_URL"] = args.base_url

    env_path = Path(__file__).resolve().parent.parent / ".env"
    _upsert_env_vars(env_path, updates)
    print(f"wrote {env_path}"
          + (f"  (CN_PROVIDER_KEY={_mask(args.key)})" if args.key else ""))

    from dotenv import load_dotenv

    # Reload so the verification below sees what was just written, in this same
    # process -- the module-level load at import time already happened.
    load_dotenv(env_path, override=True)

    sites = _csv(args.site) or sorted(spec.get("sites", {}))
    if not sites:
        print("\npreset declares no sites; nothing to verify.")
        return 1

    if not spec.get("search"):
        print(f"\npreset {args.preset!r} has no search endpoint (detail lookup only -- "
              f"typical of an agent's own 'buy this link' API). It can enrich items "
              f"you already discovered some other way but cannot populate the catalog "
              f"on its own; pair it with `driver: hybrid` and browser-based discovery.")
        print("Verify a real item id manually with:")
        print(f"  python -m sourcehub.cli provider-probe --preset {args.preset} --site <site>")
        return 0

    print(f"\nverifying against {', '.join(sites)}...")
    ok_sites: list[str] = []
    for site in sites:
        if site not in spec.get("sites", {}):
            print(f"  {site:<8} SKIP   preset {args.preset!r} does not cover this site")
            continue
        fetcher = Fetcher(delay=1.0, retries=2, timeout=45)
        try:
            client = ProviderClient(args.preset, site, fetcher)
            payload = client.call("search", {"keyword": "usb hub", "page": 1})
            from .scrapers.provider import as_list, dig

            items = as_list(dig(payload, client.preset.get("map", {}).get("items_path")))
            if not items:
                print(f"  {site:<8} FAIL   key accepted the request but 0 items came back "
                      f"-- check items_path in providers.yaml")
                continue
            offer = client.to_offer(items[0])
            if offer is None:
                print(f"  {site:<8} FAIL   {len(items)} items found but none mapped to an "
                      f"offer -- check map.item.* in providers.yaml")
                continue
            print(f"  {site:<8} ok     {len(items)} items, e.g. {offer.title[:50]!r}")
            ok_sites.append(site)
        except Exception as e:
            print(f"  {site:<8} FAIL   {type(e).__name__}: {e}")
        finally:
            fetcher.close()

    if not ok_sites:
        print("\nNothing verified. Check the key and CN_PROVIDER_BASE_URL, or try "
              "`provider-probe` for the raw response.")
        return 1

    print(f"\n{len(ok_sites)}/{len(sites)} site(s) verified. In config.yaml, set for each:")
    for site in ok_sites:
        print(f"  {site}:\n    driver: hybrid   # or: provider")
    return 0


def cmd_provider_probe(args) -> int:
    """Call a configured provider and show what the field mapping extracted.

    The point of this command: a wrong ``items_path`` in providers.yaml otherwise
    shows up as a crawl that silently returns nothing. This makes it obvious, and
    prints the candidate paths so the fix is a one-line YAML edit.
    """
    import json as _json

    from .scrapers.provider import ProviderError, load_presets, probe
    from .util.http import Fetcher

    if args.list:
        presets = load_presets().get("providers", {})
        print("Presets in providers.yaml:\n")
        for name, spec in sorted(presets.items()):
            caps = []
            if spec.get("search"):
                caps.append("search")
            if spec.get("detail"):
                caps.append("detail")
            sites = ", ".join(sorted(spec.get("sites", {})))
            print(f"  {name:<18} [{'+'.join(caps) or 'none'}]  sites: {sites}")
            print(f"  {'':<18} base_url: {spec.get('base_url', '')}")
        return 0

    s = get_settings()
    preset = args.preset or s.cn_provider_preset
    fetcher = Fetcher(delay=1.0, retries=2, timeout=45)
    try:
        report = probe(preset, args.site, args.keyword or "usb hub", fetcher, item_url=args.url)
    except ProviderError as e:
        print(f"ERROR: {e}")
        return 1
    except Exception as e:
        print(f"Request failed: {type(e).__name__}: {e}")
        print("\nCheck CN_PROVIDER_KEY / CN_PROVIDER_BASE_URL and the preset's base_url.")
        return 1
    finally:
        fetcher.close()

    print(_json.dumps(report, indent=2, ensure_ascii=False))
    if report.get("mode") == "detail":
        if not report["mapped"]:
            status = report.get("status_fields") or {}
            code = str(status.get("code", ""))
            if code and code not in ("200", "0"):
                print(f"\nThe detail call itself reports an error -- see 'status_fields' above "
                      f"({status}). That's the actual cause, not the field mapping. A 401/"
                      f"'log in' message here almost always means the saved browser profile "
                      f"isn't logged in (or the login expired) -- try `agent-login --agent "
                      f"<key>` again and check you're actually signed in before pressing Enter.")
            elif report.get("resolve") and not report["resolve"].get("resolved_id"):
                print("\nThe resolve step itself returned no id -- that's upstream of the "
                      "detail mapping. Check 'resolve.top_level_keys' above against "
                      "resolve.map.id in providers.yaml.")
            else:
                print("\nThe detail call returned no mappable item. Compare 'node_keys' above "
                      "against map.item.* in providers.yaml -- a null/renamed field there is "
                      "the usual cause.")
        return 0 if report["mapped"] else 1
    if not report["items_found"]:
        print("\nNo items matched `map.items_path`. Candidate array paths found in the")
        print("response are listed above under 'candidate_item_paths' -- set the right")
        print("one as items_path in providers.yaml and re-run.")
        return 1
    if not report["mapped"]:
        print("\nItems were found but none mapped to an offer. Check map.item.id/title/url.")
        return 1
    print("\nMapping looks good. Enable it with `driver: provider` in config.yaml.")
    return 0


def _resolve_product(session, ref: str) -> Optional[CanonicalProduct]:
    if ref.isdigit():
        return session.get(CanonicalProduct, int(ref))
    return session.scalar(select(CanonicalProduct).where(CanonicalProduct.slug == ref))


def cmd_match_explain(args) -> int:
    """Score one product's offer against another product directly.

    303 of this catalog's 317 products currently sit alone with no other site's
    offer attached, and most of those pairs never even reach the matcher's scoring
    step -- candidate search (image hashing, shared title tokens, model codes) has
    to surface a pair before ``_score`` ever runs on it. This bypasses that and
    scores two specific listings against each other regardless, so "why didn't
    these merge" has an answer instead of a shrug.
    """
    from .pipeline.matching import MatchEngine
    from .db.models import OfferSpec

    with session_scope() as session:
        product_a = _resolve_product(session, args.product_a)
        if product_a is None:
            print(f"no product matches {args.product_a!r}")
            return 1
        product_b = _resolve_product(session, args.product_b)
        if product_b is None:
            print(f"no product matches {args.product_b!r}")
            return 1
        if product_a.id == product_b.id:
            print("that's the same product")
            return 1

        offer = session.scalar(
            select(Offer)
            .where(Offer.canonical_id == product_a.id, Offer.is_active.is_(True))
            .order_by(Offer.id)
        )
        if offer is None:
            print(f"{args.product_a!r} has no active offer to score")
            return 1
        specs = session.scalars(select(OfferSpec).where(OfferSpec.offer_id == offer.id)).all()

        engine = MatchEngine(session, load_crawl_config())
        exp = engine.explain_pair(offer, product_b, specs)

        print(f"A: [{offer.id}] {offer.site.key}  {(offer.title_en or offer.title_raw)[:70]!r}")
        print(f"B: [{product_b.id}] {product_b.slug}  {product_b.title_en[:70]!r}")
        print("-" * 72)

        if exp.blocked_by_rejection:
            print("blocked: a human already rejected this pairing (see match_rejections)")
        print(f"would ever be compared by the live matcher: "
              f"{'yes' if exp.was_candidate else 'no -- candidate search never finds this pair'}")
        print()

        print("tier 1 -- hard identifiers")
        print(f"  gtin   A={offer.gtin or '-'}  B={product_b.gtin or '-'}"
              + ("  MATCH -> instant merge" if exp.gtin_hit
                 else "  CONFLICT (blocks any merge)" if exp.gtin_conflict else ""))
        print(f"  brand+mpn  A={offer.brand or '-'}/{offer.mpn or '-'}"
              f"  B={product_b.brand or '-'}/{product_b.mpn or '-'}"
              + ("  MATCH -> instant merge" if exp.mpn_hit else ""))

        if exp.method in ("gtin", "brand_mpn"):
            print(f"\nresolved at tier 1 ({exp.method}), score {exp.score:.3f} -- "
                  "the weighted signals below never ran")
            return 0

        print()
        print("tier 2 -- weighted signals")
        w = engine.weights
        for key, label in (("image", "image_phash"), ("title", "title"), ("specs", "specs")):
            val = exp.signals.get(key)
            if val is None:
                continue
            used = (
                (key == "image" and val > 0)
                or (key == "title" and val > 0)
                or (key == "specs" and exp.signals.get("spec_overlap"))
            )
            extra = f"  (spec_overlap={exp.signals['spec_overlap']})" if key == "specs" else ""
            print(f"  {label:<12} {val:.3f}  weight {w[label]:.2f}"
                  + ("" if used else "  -- not evaluated, excluded from the average") + extra)

        if exp.signals.get("code_match"):
            print("  model code match: +0.12 bonus")
        if exp.signals.get("code_conflict"):
            print("  model code conflict: score x0.75 penalty")

        base = exp.signals.get("base")
        if base is not None:
            print(f"\n  weighted average (base): {base:.3f}")
            if exp.signals["image"] >= 0.85:
                print("  strong_image rule applies (image >= 0.85): final score is "
                      "floored against title/spec agreement, not just the average")
        print(f"  final score: {exp.score:.3f}")

        print()
        print(f"thresholds: review >= {engine.review_threshold:.2f}  "
              f"auto-merge >= {engine.auto_threshold:.2f}")
        verdict = {
            "weighted": "would AUTO-MERGE",
            "review": "would go to the REVIEW QUEUE",
            "below_threshold": "would NOT merge",
            "blocked": "blocked by a prior human rejection",
        }.get(exp.method, exp.method)
        print(f"verdict: {verdict}")
    return 0


def cmd_rematch(args) -> int:
    """Re-run matching over offers that never joined a multi-site product."""
    from .pipeline.matching import MatchEngine, rebuild_product
    from .db.models import OfferSpec

    cfg = load_crawl_config()
    merged = 0
    with session_scope() as session:
        engine = MatchEngine(session, cfg)
        singles = session.scalars(
            select(Offer)
            .join(CanonicalProduct, CanonicalProduct.id == Offer.canonical_id)
            .where(CanonicalProduct.offer_count <= 1, Offer.is_active.is_(True))
            .limit(args.limit)
        ).all()

        print(f"Re-matching {len(singles)} unmatched listings...")
        for offer in singles:
            old_id = offer.canonical_id
            specs = session.scalars(
                select(OfferSpec).where(OfferSpec.offer_id == offer.id)
            ).all()
            result = engine.match(offer, specs)
            if result.matched and result.product.id != old_id:
                offer.canonical_id = result.product.id
                offer.match_score = result.score
                offer.match_method = result.method
                session.flush()
                rebuild_product(session, result.product)
                index_product(session, result.product)
                old = session.get(CanonicalProduct, old_id) if old_id else None
                if old is not None:
                    rebuild_product(session, old)
                    if old.offer_count == 0:
                        session.delete(old)
                merged += 1
        recount_categories(session)
    print(f"Merged {merged} listings into existing products.")
    return 0


def cmd_recategorize(args) -> int:
    with session_scope() as session:
        changed = recategorize_all(session)
    print(f"Recategorized {changed} products.")
    return 0


def cmd_reindex(args) -> int:
    with session_scope() as session:
        products = session.scalars(select(CanonicalProduct)).all()
        for p in products:
            index_product(session, p)
        recount_categories(session)
    print(f"Reindexed {len(products)} products.")
    return 0


def cmd_prune(args) -> int:
    """Retire vanished listings, and optionally reclaim the space behind them.

    Deactivating listings is the default because it is the safe, everyday half.
    The reclaim flags delete data, so they are opt-in -- except under --all, which
    is the one you want on a schedule.
    """
    from .pipeline.retention import (
        human_bytes,
        prune_orphan_media,
        prune_price_history,
        vacuum,
    )

    n = deactivate_stale(days=args.days)
    print(f"Deactivated {n} listings not seen in {args.days} days.")

    history_days = args.history_days
    do_media = args.media or args.all
    do_vacuum = args.vacuum or args.all
    if args.all and history_days is None:
        from .pipeline.retention import RetentionPolicy

        history_days = RetentionPolicy.from_config().price_history_days

    if history_days:
        rows = prune_price_history(history_days)
        print(f"Removed {rows} price points older than {history_days} days "
              f"(the newest point per listing is always kept).")

    if do_media:
        rows, files, size = prune_orphan_media()
        print(f"Removed {rows} unreachable image records and {files} unreferenced "
              f"files ({human_bytes(size)}).")

    if do_vacuum:
        freed = vacuum()
        print(f"VACUUM reclaimed {human_bytes(freed)}."
              if freed > 0 else "VACUUM freed nothing (already compact).")

    return 0


def cmd_backup(args) -> int:
    """Snapshot the database while it is being served."""
    from .pipeline.retention import backup, default_backup_dir, human_bytes

    dest = args.out
    try:
        path = backup(dest=dest, keep=args.keep)
    except Exception as e:
        print(f"Backup failed: {e}")
        return 1

    size = path.stat().st_size
    print(f"Wrote {path} ({human_bytes(size)}).")
    if args.keep and not dest:
        print(f"Keeping the {args.keep} newest snapshots in {default_backup_dir()}.")
    print("Restore by stopping the app and copying this file over the live database.")
    return 0


def cmd_retention(args) -> int:
    """Run the whole scheduled retention pass by hand."""
    from .pipeline.retention import RetentionPolicy, human_bytes, run_retention

    pol = RetentionPolicy.from_config()
    if args.no_backup:
        pol.backups = 0
    report = run_retention(pol)

    if report.backup:
        print(f"Backup: {report.backup}")
        if report.backups_removed:
            print(f"  rotated out {report.backups_removed} older snapshot(s)")
    print(f"Price history: removed {report.history_rows} rows "
          f"older than {pol.price_history_days} days")
    if pol.orphan_media:
        print(f"Media: {report.media_rows} records, {report.media_files} files, "
              f"{human_bytes(report.media_bytes)}")
    if pol.vacuum:
        print(f"VACUUM: reclaimed {human_bytes(report.vacuum_bytes)}")
    for err in report.errors:
        print(f"  ! {err}")
    return 1 if report.errors else 0


def cmd_demand(args) -> int:
    """What people searched for, and whether crawling it found anything.

    This is the feedback loop for tuning live_search: keywords with many requests
    and zero results are either bad queries or adapters that need attention.
    """
    from .db.models import SearchDemand

    init_db()
    with session_scope() as session:
        q = session.query(SearchDemand)
        if args.failed:
            q = q.filter(SearchDemand.offers_found == 0)
        rows = q.order_by(SearchDemand.request_count.desc()).limit(args.limit).all()

        if not rows:
            print("No searches recorded yet.")
            return 0

        print(f"{'keyword':<34}{'asked':>6}{'crawls':>7}{'found':>7}  {'status':<8}last crawled")
        print("-" * 88)
        for r in rows:
            when = r.last_crawled.strftime("%Y-%m-%d %H:%M") if r.last_crawled else "never"
            print(f"{r.display[:33]:<34}{r.request_count:>6}{r.crawl_count:>7}"
                  f"{r.offers_found:>7}  {(r.last_status or '-'):<8}{when}")
            if r.last_error:
                print(f"{'':<34}error: {r.last_error[:60]}")
    return 0


def cmd_stats(args) -> int:
    with session_scope() as session:
        products = session.scalar(select(func.count(CanonicalProduct.id))) or 0
        offers = session.scalar(select(func.count(Offer.id))) or 0
        images = session.scalar(select(func.count(Image.id))) or 0
        pending = session.scalar(
            select(func.count(MatchReview.id)).where(MatchReview.status == "pending")
        ) or 0
        multi = session.scalar(
            select(func.count(CanonicalProduct.id)).where(CanonicalProduct.site_count > 1)
        ) or 0

        print(f"products            {products:>9,}")
        print(f"  on 2+ sites       {multi:>9,}")
        print(f"listings            {offers:>9,}")
        print(f"images              {images:>9,}")
        print(f"pending reviews     {pending:>9,}")
        print("\nlistings per site:")
        rows = session.execute(
            select(Site.name, func.count(Offer.id))
            .join(Offer, Offer.site_id == Site.id, isouter=True)
            .group_by(Site.id)
            .order_by(func.count(Offer.id).desc())
        ).all()
        for name, count in rows:
            print(f"  {name:<20} {count:>8,}")
    return 0


# ------------------------------------------------------------------------ main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sourcehub", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create tables and seed reference data").set_defaults(
        func=cmd_init_db)
    sub.add_parser("fx", help="refresh currency exchange rates").set_defaults(func=cmd_fx)

    c = sub.add_parser("crawl", help="search sites and ingest listings")
    c.add_argument("--sites", help="comma-separated site keys (default: all enabled)")
    c.add_argument("--keyword", help="comma-separated keywords (default: config.yaml)")
    c.add_argument("--categories", action="store_true",
                   help="walk each site's category_seeds() instead of searching "
                        "keywords -- only sites that implement it support this")
    c.add_argument("--pages", type=int, help="listing pages per keyword")
    c.add_argument("--no-details", action="store_true",
                   help="skip product pages (fast, but no specs or shipping cost)")
    c.add_argument("--detail-limit", type=int,
                   help="cap detail fetches per keyword")
    c.add_argument("--no-fx", action="store_true", help="skip the FX refresh")
    c.set_defaults(func=cmd_crawl)

    r = sub.add_parser("refresh", help="re-price known listings without discovery")
    r.add_argument("--sites")
    r.add_argument("--older-than", type=int, default=12, help="hours (default 12)")
    r.add_argument("--limit", type=int, default=500)
    r.set_defaults(func=cmd_refresh)

    sub.add_parser(
        "demo-seed", help="load sample listings so you can see the UI without crawling"
    ).set_defaults(func=cmd_demo_seed)

    s = sub.add_parser("serve", help="run the web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--reload", action="store_true")
    s.add_argument("--with-scheduler", action="store_true",
                   help="also run the crawl scheduler in this process (container mode)")
    s.set_defaults(func=cmd_serve)

    sub.add_parser("schedule", help="run scheduled crawls in the foreground").set_defaults(
        func=cmd_schedule)

    b = sub.add_parser("browser-login", help="one-time login for a site that needs one")
    b.add_argument("--site", required=True,
                   help="a site key from config.yaml/scrapers/registry.py, "
                   "e.g. taobao, tmall, 1688, temu")
    b.set_defaults(func=cmd_browser_login)

    al = sub.add_parser(
        "agent-login",
        help="one-time login to a forwarding agent's own site (superbuy, cssbuy, ...)",
    )
    al.add_argument("--agent", help="agent key -- see --list")
    al.add_argument("--list", action="store_true", help="list known agents and exit")
    al.set_defaults(func=cmd_agent_login)

    t = sub.add_parser("selftest", help="check adapters still parse their sites")
    t.add_argument("--site", help="comma-separated site keys (default: all)")
    t.add_argument("--keyword", help="search term to test with")
    t.add_argument("--save-fixture", action="store_true",
                   help="save the fetched HTML to tests/fixtures/ for offline replay")
    t.set_defaults(func=cmd_selftest)

    fx = sub.add_parser("fixtures", help="list saved adapter fixtures")
    fx.set_defaults(func=cmd_fixtures)

    sub.add_parser(
        "health", help="report adapters that stopped finding listings"
    ).set_defaults(func=cmd_health)

    sub.add_parser(
        "duty-check", help="re-verify duty.yaml's rates against USITC's live HTS table"
    ).set_defaults(func=cmd_duty_check)

    w = sub.add_parser("watch", help="price watches and alerts")
    w.add_argument("action", choices=["add", "list", "remove", "check"])
    w.add_argument("slug", nargs="?", default="",
                   help="product slug (add) or watch id (remove)")
    w.add_argument("--target", type=float, help="alert when the price drops to this")
    w.add_argument("--label", help="a name for this watch")
    w.add_argument("--landed", action="store_true",
                   help="compare landed cost rather than unit price")
    w.add_argument("--direct-only", action="store_true",
                   help="ignore sites that need a forwarding agent")
    w.add_argument("--restock", action="store_true",
                   help="alert when the product comes back in stock, not on price")
    w.add_argument("--new-site", dest="new_site", action="store_true",
                   help="alert when a site that didn't already sell this starts to")
    w.add_argument("--webhook", help="POST alerts here (Slack/Discord compatible)")
    w.add_argument("--no-notify", action="store_true", help="check without delivering")
    w.set_defaults(func=cmd_watch)

    ts = sub.add_parser(
        "trust-setup",
        help="fix CERTIFICATE_VERIFY_FAILED caused by antivirus HTTPS scanning",
    )
    ts.add_argument("--root-pem", help="path to the interceptor root CA (PEM)")
    ts.add_argument("--subject", help="substring of the root CA subject to export")
    ts.set_defaults(func=cmd_trust_setup)

    b = sub.add_parser("bom", help="cost a parts list (bulk sourcing)")
    b.add_argument("file", help="path to a text file, or - for stdin")
    b.add_argument("--direct-only", action="store_true",
                   help="only sites that ship to the US without an agent")
    b.add_argument("--csv", action="store_true", help="emit CSV instead of a table")
    b.set_defaults(func=cmd_bom)

    im = sub.add_parser("image-search", help="find products from a photo (file or URL)")
    im.add_argument("target", help="path to an image file, or an image URL")
    im.add_argument("--limit", type=int, default=15)
    im.set_defaults(func=cmd_image_search)

    aa = sub.add_parser(
        "agent-auth",
        help="set up and verify a forwarding-agent API key for 1688/taobao/tmall",
    )
    aa.add_argument("--preset", default="otapi", help="a preset name from providers.yaml")
    aa.add_argument("--key", help="the API key/instanceKey the provider issued you")
    aa.add_argument("--base-url", help="override the preset's base_url (agent_lookup/custom)")
    aa.add_argument("--site", help="comma-separated sites to verify (default: all the preset covers)")
    aa.add_argument("--list", action="store_true", help="list available presets and exit")
    aa.set_defaults(func=cmd_agent_auth)

    pp = sub.add_parser(
        "provider-probe",
        help="test a providers.yaml preset and show what the mapping extracted",
    )
    pp.add_argument("--preset", help="preset name (default: CN_PROVIDER_PRESET)")
    pp.add_argument("--site", default="taobao", choices=["taobao", "tmall", "1688"])
    pp.add_argument("--keyword", help="search term to probe with (presets that support search)")
    pp.add_argument("--url", help="a real item URL (presets that only do item lookup, "
                                  "e.g. agent_lookup, usfans -- no keyword search)")
    pp.add_argument("--list", action="store_true", help="list available presets and exit")
    pp.set_defaults(func=cmd_provider_probe)

    m = sub.add_parser("rematch", help="retry matching on unmatched listings")
    m.add_argument("--limit", type=int, default=2000)
    m.set_defaults(func=cmd_rematch)

    me = sub.add_parser(
        "match-explain",
        help="show why (or why not) two products' offers would merge",
    )
    me.add_argument("product_a", help="product slug or id (its first active offer is scored)")
    me.add_argument("product_b", help="product slug or id (the comparison target)")
    me.set_defaults(func=cmd_match_explain)

    sub.add_parser("recategorize", help="reclassify every product").set_defaults(
        func=cmd_recategorize)
    sub.add_parser("reindex", help="rebuild the search index").set_defaults(func=cmd_reindex)

    pr = sub.add_parser("prune", help="retire vanished listings and reclaim space")
    pr.add_argument("--days", type=int, default=30,
                    help="deactivate listings not seen in this many days")
    pr.add_argument("--history-days", type=int, default=None,
                    help="also delete price points older than this")
    pr.add_argument("--media", action="store_true",
                    help="also delete unreachable image records and unreferenced files")
    pr.add_argument("--vacuum", action="store_true",
                    help="also compact the database file afterwards")
    pr.add_argument("--all", action="store_true",
                    help="every reclaim step, using retention: from config.yaml")
    pr.set_defaults(func=cmd_prune)

    bk = sub.add_parser("backup", help="snapshot the database (safe while serving)")
    bk.add_argument("--out", type=Path, default=None,
                    help="destination file (default: data/backups/sourcehub-<stamp>.db)")
    bk.add_argument("--keep", type=int, default=7,
                    help="snapshots to keep in the default directory; 0 keeps all")
    bk.set_defaults(func=cmd_backup)

    rt = sub.add_parser("retention",
                        help="run the scheduled backup + reclaim pass by hand")
    rt.add_argument("--no-backup", action="store_true",
                    help="skip the snapshot and only reclaim")
    rt.set_defaults(func=cmd_retention)

    dm = sub.add_parser("demand", help="keywords people searched for")
    dm.add_argument("--limit", type=int, default=30)
    dm.add_argument("--failed", action="store_true", help="only those that found nothing")
    dm.set_defaults(func=cmd_demand)

    sub.add_parser("stats", help="catalog summary").set_defaults(func=cmd_stats)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    from .certs import setup_tls

    # This CLI routinely prints untranslated CJK text (raw titles, shop names,
    # specs) straight from the sites it scrapes -- Chinese by far the most
    # common. Windows' console encoding defaults to a legacy codepage (cp1252
    # etc.) depending on locale/terminal, which can't represent most of that
    # and crashes with a raw UnicodeEncodeError on an ordinary `print()`, not
    # anything specific to one command. Force UTF-8 defensively rather than
    # hoping every user's console happens to already be configured for it.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    args = build_parser().parse_args(argv)
    _log(args.verbose)
    setup_tls()   # no-op unless HTTPS interception needs working around
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
