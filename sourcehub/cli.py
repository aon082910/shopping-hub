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
from typing import Sequence

from sqlalchemy import func, select

from .config import get_settings, load_crawl_config
from .db.models import CanonicalProduct, Image, MatchReview, Offer, Site
from .db.search import index_product
from .db.session import init_db, session_scope
from .pipeline.categories import recategorize_all, recount_categories
from .pipeline.ingest import crawl_all, crawl_site, deactivate_stale, refresh_prices
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

    keywords = _csv(args.keyword) or load_crawl_config().keywords
    sites = _csv(args.sites)

    if sites:
        results = {}
        for key in sites:
            results[key] = crawl_site(
                key, keywords,
                max_pages=args.pages,
                fetch_details=not args.no_details,
                detail_limit=args.detail_limit,
            )
    else:
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

    print(f"SourceHub on http://{args.host}:{args.port}")
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
    from .scrapers.registry import get_adapter
    from .util.browser import interactive_login

    adapter = get_adapter(args.site)
    url = getattr(adapter, "login_url", "") or adapter.base_url
    print(f"Opening {url} for a one-time login to {adapter.name}...")
    interactive_login(url)
    print("Session saved. Headless crawls will now reuse these cookies.")
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
    from .pipeline.watch import check_watches, current_price

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
                notify_url=args.webhook,
                baseline_usd=price,
                last_price_usd=price,
            )
            session.add(watch)
            session.flush()
            print(f"Watching {product.title_en[:60]} (id {watch.id})")
            print(f"  target ${args.target:.2f} against "
                  f"{'landed cost' if args.landed else 'unit price'}"
                  f"{' , direct-shipping sites only' if args.direct_only else ''}")
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
            print(f"HIT  ${t.price:.2f} on {t.site} "
                  f"(target ${t.watch.target_usd:.2f})  {t.product.title_en[:50]}")
            print(f"     /product/{t.product.slug}")
        return 0


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
        report = probe(preset, args.site, args.keyword or "usb hub", fetcher)
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
    n = deactivate_stale(days=args.days)
    print(f"Deactivated {n} listings not seen in {args.days} days.")
    return 0


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

    b = sub.add_parser("browser-login", help="one-time login for taobao/tmall/1688")
    b.add_argument("--site", required=True, choices=["taobao", "tmall", "1688"])
    b.set_defaults(func=cmd_browser_login)

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

    pp = sub.add_parser(
        "provider-probe",
        help="test a providers.yaml preset and show what the mapping extracted",
    )
    pp.add_argument("--preset", help="preset name (default: CN_PROVIDER_PRESET)")
    pp.add_argument("--site", default="taobao", choices=["taobao", "tmall", "1688"])
    pp.add_argument("--keyword", help="search term to probe with")
    pp.add_argument("--list", action="store_true", help="list available presets and exit")
    pp.set_defaults(func=cmd_provider_probe)

    m = sub.add_parser("rematch", help="retry matching on unmatched listings")
    m.add_argument("--limit", type=int, default=2000)
    m.set_defaults(func=cmd_rematch)

    sub.add_parser("recategorize", help="reclassify every product").set_defaults(
        func=cmd_recategorize)
    sub.add_parser("reindex", help="rebuild the search index").set_defaults(func=cmd_reindex)

    pr = sub.add_parser("prune", help="deactivate listings that have disappeared")
    pr.add_argument("--days", type=int, default=30)
    pr.set_defaults(func=cmd_prune)

    dm = sub.add_parser("demand", help="keywords people searched for")
    dm.add_argument("--limit", type=int, default=30)
    dm.add_argument("--failed", action="store_true", help="only those that found nothing")
    dm.set_defaults(func=cmd_demand)

    sub.add_parser("stats", help="catalog summary").set_defaults(func=cmd_stats)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    from .certs import setup_tls

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
