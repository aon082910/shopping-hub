# SourceHub

Scrapes 11 China-facing marketplaces, matches the same physical product across
them, and publishes one item page per product showing every site's price, minimum
order quantity, and disclosed shipping/fees — translated to English, with
forwarding-agent links for the sites that won't ship to the US.

```
AliExpress  Alibaba  1688  Taobao  Tmall  DHgate
Chinavasion  Global Sources  Made-in-China  GearBest  Banggood
```

---

## Quick start

### Unraid (easiest)

Open the **Apps** tab, search **shopping-hub**, hit Install. Set an admin token and
leave `--shm-size=1g` in Extra Parameters. Full walkthrough in [UNRAID.md](UNRAID.md); the container template lives in
[aon082910/Unraid-CA](https://github.com/aon082910/Unraid-CA).

### Docker

```bash
docker run -d --name shopping-hub -p 8000:8000 --shm-size=1g \
  -v ./appdata:/config \
  -e SOURCEHUB_ADMIN_TOKEN=change-me \
  allornothing/shopping-hub:latest
```

Open <http://127.0.0.1:8000>. A `docker-compose.yml` is included, and Unraid users
have a container template plus a full guide in [UNRAID.md](UNRAID.md).

`--shm-size=1g` is not optional: Docker's default 64 MB `/dev/shm` makes Chromium
crash on heavy pages, and it fails silently -- the browser-rendered sites just
return zero listings.

### From source

```bash
git clone https://github.com/aon082910/shopping-hub.git
cd shopping-hub
python -m venv .venv
.venv/bin/pip install -r requirements.txt          # Windows: .venv\Scripts\pip
.venv/bin/python -m playwright install chromium
cp .env.example .env                               # Windows: copy
```

See the UI immediately with sample data (no network needed):

```bash
.venv/bin/python -m sourcehub.cli demo-seed
```

```bash
.venv/bin/python -m sourcehub.cli serve
```

Then open <http://127.0.0.1:8000>.

Check the scrapers still parse their sites before your first real crawl:

```bash
.venv/bin/python -m sourcehub.cli selftest --site aliexpress,dhgate,banggood
```

Crawl for real:

```bash
.venv/bin/python -m sourcehub.cli crawl --sites aliexpress,dhgate,banggood --keyword "usb c hub" --pages 3
```

Run it continuously (daily full crawl + 6-hourly re-pricing):

```bash
.venv/bin/python -m sourcehub.cli schedule
```

---

## Read this before you crawl

Three things about this problem domain that shape everything below.

**1. Taobao, Tmall and 1688 cannot be scraped anonymously.** They require a
logged-in session and serve a slide captcha to anything that looks automated.
There is no header trick around this. Two supported options — see
[Getting these three without a login](#getting-taobao--tmall--1688-without-a-login)
below for the full comparison:

- `driver: browser` (default) — log in by hand *once*, and the cookies persist:
  ```bash
  .venv/bin/python -m sourcehub.cli browser-login --site taobao
  ```
  A real Chromium window opens; log in, solve the slider, press Enter. Headless
  runs then reuse that profile. Expect to redo this every few weeks.
- `driver: provider` — an HTTP JSON API. No login, no captcha, no browser. This is
  the option for unattended operation.
- `driver: hybrid` — browser for discovery, API for enrichment. Usually the best
  trade-off, and the only way to use a forwarding agent's endpoint.

**2. The other eight sites will rate-limit or block a bare Python client.** They
fingerprint the TLS handshake, not just the User-Agent. `curl_cffi` (in
requirements) impersonates a real Chrome handshake, which is what actually gets
through. For sustained crawling you will also want a residential proxy in
`SOURCEHUB_PROXY`. Selectors on these sites change without notice — `selftest`
tells you which adapter broke, and each lives in one file under
`sourcehub/scrapers/`.

**3. Crawl politely, and check each site's terms.** Defaults in `config.yaml` are
2.5–6 s between requests per host with jitter and concurrency 1–2. Aggressive
settings will get your IP banned quickly and are a good way to turn a working
system into a broken one.

---

## Getting Taobao / Tmall / 1688 without a login

Four routes exist. They differ in one way that matters more than price:

| Route | Keyword search | Item detail | Notes |
|---|---|---|---|
| Forwarding agents' own endpoints (CSSBuy, Superbuy, Sugargoo…) | rarely | yes | Free-ish, undocumented, Cloudflare-fronted, ToS-gray |
| **Data-provider API (OTAPI et al.)** | **yes** | yes | What many agent sites run on top of. Paid, documented |
| Unblocker services (Bright Data, Oxylabs, Zyte) | yes | yes | They solve login/captcha; you still parse |
| Official 1688 / Taobao Open Platform | yes | yes | Needs a Chinese business account + approval |

**The catch with agents: most do item lookup only.** They resolve a URL or item id
you already have — they don't search the catalog. That enriches a known product but
cannot *discover* new ones, so an agent endpoint alone will never populate the
catalog. The shipped `agent_lookup` preset deliberately has no `search:` section.

### Hybrid mode

Crawling is two jobs with very different costs, so they're configured separately.
`driver:` is shorthand for a (search, detail) pair:

| `driver:` | search (discovery) | detail (enrichment) |
|---|---|---|
| `browser` | browser | browser |
| `provider` | provider | provider |
| **`hybrid`** | **browser** | **provider** |

**Hybrid is usually the right answer.** Discovery costs one gated page per *results
page*; enrichment costs one per *product* — so detail is where nearly all the
traffic goes, and where you get blocked. Hybrid keeps discovery on the browser
(which agents can't do anyway) and moves the expensive bulk onto the API, cutting
gated page loads by roughly the number of products per results page — ~40× on
Taobao.

```yaml
taobao:
  driver: hybrid
```

Finer control if you want it: `search_driver:` / `detail_driver:` override either
half, `provider_preset:` overrides the preset per site, and `detail_fallback: false`
stops the browser being used when a provider lookup comes back empty (default is to
fall back).

Anything requesting the provider is **downgraded to browser when the provider can't
do that job** — so a lookup-only agent configured as `driver: provider` resolves to
hybrid automatically, with a log line saying so, instead of crawling nothing.
Enrichment merges onto the discovered offer rather than replacing it, so fields the
listing page had (sales counts, seller location) survive even when the detail call
omits them.

All four routes are the same shape — call an endpoint, normalize JSON — so none of
it is hardcoded. Endpoints, auth and field mappings live in
[`providers.yaml`](providers.yaml); switching vendors is a YAML edit.

```yaml
map:
  items_path: ["Result.Items.Items.Content", "Items.Content"]   # first hit wins
  item:
    id:    ["Id", "ItemId"]
    price: ["Price.ConvertedPriceWithoutSign", "Price.OriginalPrice"]
    images: "Pictures[].Url"                                     # [] = fan out
```

Set `CN_PROVIDER_PRESET` + `CN_PROVIDER_KEY` in `.env`, then verify before trusting
it:

```bash
.venv/bin/python -m sourcehub.cli provider-probe --preset otapi --site 1688 --keyword "usb hub"
```

That prints the response's top-level keys, **every array-of-objects it found** (i.e.
candidates for `items_path`), and exactly what the mapping extracted. A wrong path
becomes obvious instead of showing up later as an inexplicably empty crawl. Then set
`driver: provider` for those sites in `config.yaml`.

> The presets ship with paths written from vendor documentation, **not** verified
> against a live account — vendors rename fields and every RapidAPI reseller invents
> its own shape. Assume you will need one round of `provider-probe` to correct them.
> That is the intended workflow, not a defect.

If no key is configured, `driver: provider` logs a warning and falls back to the
browser driver rather than failing the run.

---

## How matching works

This is the hard part and the whole point. The same earbuds appear as:

| Site | Title | Price |
|---|---|---|
| 1688 | `2024新款TWS蓝牙耳机5.3无线降噪运动耳机厂家批发` | ¥18.50, MOQ 2 |
| AliExpress | `TWS Wireless Bluetooth 5.3 Earbuds Noise Cancelling Sport Headset` | $7.99 |
| DHgate | `Wholesale TWS Earbuds BT5.3 ANC Sports Headphones Lot` | $5.40, MOQ 5 |

No shared identifier, three flavours of keyword soup. Signals are applied
cheapest-and-most-certain first (`sourcehub/pipeline/matching.py`):

1. **GTIN** (UPC/EAN, checksum-validated) — exact match merges immediately.
2. **Brand + MPN** — near-certain.
3. **Perceptual image hash.** These marketplaces reuse the *identical supplier
   photograph* across every reseller, which makes this unreasonably effective.
   Candidates come from LSH banding (4 × 16-bit bands over the 64-bit hash), then
   scored by Hamming distance.
4. **Normalized title similarity**, computed on the *English* title after
   stripping marketing filler (`2024 New Hot Sale Free Shipping Wholesale…`), so
   Chinese and English listings become comparable.
5. **Spec agreement** across normalized attribute keys.

Signals 3–5 combine into a weighted score, **renormalized over only the signals
that could actually be evaluated** — a listing with no spec table isn't penalized
as though its specs disagreed. A near-identical photo plus a consistent title
auto-merges; a near-identical photo with an unrelated title is treated as a
probable colour/capacity variant and goes to the review queue at `/admin` rather
than being guessed either way. Thresholds and weights are in `config.yaml`.

Conflicting model codes in the titles are a separating signal, so `10000mAh` vs
`20000mAh` stays apart on its own without anyone reviewing it.

### Rejections are permanent

"Keep separate" in the review queue is durable. Rejections are stored as **offer
pairs**, not `(offer, product)` — canonical products are derived state that gets
merged, split and emptied as the catalog churns, so a rejection anchored to a
product id decays into meaninglessness, while offers are stable for as long as the
listing exists.

The consequence: `rematch` will never re-propose a merge you already refused, and
neither will a re-crawl. If the offer had already been merged, rejecting it also
splits it back out. Rejections outrank every automated signal including an exact
GTIN match — though that combination logs a warning, since identical validated
GTINs on genuinely different products means one side's data is wrong. Anything
rejected is listed at `/admin` with an **Undo**, because a ruling the matcher
treats as authoritative has to be reversible.

**Translation is load-bearing here.** With `TRANSLATE_PROVIDER=none`, Chinese
listings cannot be title-matched and will mostly stay on their own product pages.
Set a provider if you want 1688/Taobao/Tmall to merge with the English sites.

---

## Prices, MOQ and the number that actually matters

Unit price alone is misleading: `$0.90 × MOQ 500` is a $450 purchase and not
comparable to a $6 single unit. Every offer therefore stores:

- `price_usd` — unit price at MOQ, FX-converted. If the listing has a volume
  ladder, this is the tier that **actually applies at MOQ**, not the headline
  "as low as" price.
- `landed_cost_usd` — `unit × MOQ + shipping`. This is the comparison column.
- Full tier ladder, MOQ + unit, shipping cost/free/from, fees note, lead time.

Anything a site doesn't disclose is stored as `NULL` and rendered as *"not
listed"* — never silently treated as zero. Global Sources listings that say
"Negotiable" are ingested with a null price so they still appear as a sourcing
option but are excluded from best-price rollups.

FX rates refresh daily from Frankfurter with a fallback table baked in, so the
system works offline.

---

## US ordering & forwarding agents

1688, Taobao and Tmall are domestic-China only. Their offers are flagged
`needs_agent`, and instead of a buy button the item page shows deep links into
five forwarding agents that accept US customers — **Superbuy, Wegobuy, CSSBuy,
Sugargoo, Hagobuy** — each pre-loaded with that exact item, plus a rough all-in
estimate (goods + service fee + China domestic + international freight), clearly
labelled an estimate because real freight is billed on volumetric weight once the
item reaches the agent's warehouse.

The other eight sites get a single direct "Buy" link.

Referral codes are optional (`AGENT_REF_*` in `.env`); without them the links are
clean and untagged. Add or edit agents in `sourcehub/db/seed.py` +
`sourcehub/agents.py`.

---

## Browsing and search

- **Search** — SQLite FTS5 with BM25 ranking over title, brand, model,
  identifiers, specs and category path. CJK is segmented per character at both
  index and query time, so `蓝牙耳机` works and so does Latin text embedded inside
  a Chinese title. Filters: marketplace, price range, max MOQ, "ships to US
  without an agent"; sorts include *biggest price spread* and *most sites
  compared*.
- **Categories** — 14 top-level × ~7 subcategories each, seeded in
  `sourcehub/db/seed.py`. Each site's own breadcrumb is mapped onto this tree via
  a keyword table (`sourcehub/pipeline/categories.py`) and the decision is cached
  per `(site, raw breadcrumb)`. Extend the keyword table and run `recategorize`.
- **JSON API** — `/api/search`, `/api/product/{slug}`, `/api/categories`,
  `/api/stats`, `/api/suggest`.

---

## Staying up to date

`python -m sourcehub.cli schedule` runs four cron jobs (expressions in
`config.yaml`):

| Job | Default | What it does |
|---|---|---|
| `full_crawl` | 03:00 daily | Sweeps every enabled site for every seed keyword; new listings are ingested, imaged and translated automatically |
| `refresh_prices` | every 6 h | Re-prices known listings without re-running discovery |
| `fx_rates` | 02:30 daily | Currency refresh |
| `rematch` | Sundays 05:00 | Retries matching on unmatched listings — a product crawled today may be the missing sibling for one from last week |

On Windows, wrap that command in Task Scheduler to survive reboots.

Everything is idempotent: re-crawling updates rows in place, appends a
price-history point only when something changed, and never duplicates a product.
Translation is cached by content hash, so a repeat crawl translates only genuinely
new strings.

---

## Commands

```
init-db          create tables, seed sites/categories/agents
demo-seed        load sample listings (offline) to preview the UI
selftest         check adapters parse their sites  --site --keyword --save-fixture
fixtures         list saved adapter fixtures
health           report adapters that stopped finding listings (exit 1 if any)
image-search     find products from a photo         <file|url> --limit
bom              cost a parts list                  <file|-> --direct-only --csv
crawl            search sites and ingest      --sites --keyword --pages --no-details
refresh          re-price known listings      --sites --older-than --limit
serve            web UI                       --host --port --reload
schedule         run the background scheduler
browser-login    one-time login for taobao/tmall/1688
provider-probe   test a providers.yaml preset      --preset --site --keyword --list
rematch          retry matching on unmatched listings
recategorize     reclassify every product
reindex          rebuild the search index
prune            deactivate listings that have disappeared
fx               refresh exchange rates
stats            catalog summary
```

---

## Layout

```
sourcehub/
  config.py            settings (.env) + crawl config (config.yaml)
  agents.py            forwarding-agent deep links
  demo.py              offline sample data
  scheduler.py         APScheduler jobs
  cli.py               command line
  db/
    models.py          schema  (Site, Category, CanonicalProduct, Offer, ...)
    search.py          FTS5 index + CJK segmentation
    seed.py            the 11 sites, the taxonomy, the agents
  scrapers/
    base.py            SiteAdapter contract + RawOffer
    registry.py        site key -> adapter
    provider.py        data-driven API driver (no-login route for the CN sites)
    aliexpress.py alibaba.py taobao_family.py dhgate.py chinavasion.py
    globalsources.py madeinchina.py banggood.py     (+ GearBest)
  pipeline/
    ingest.py          crawl -> normalize -> translate -> match -> publish
    matching.py        cross-site product matching + rollups
    translate.py       provider-agnostic translation w/ persistent cache
    images.py          download, dedupe, thumbnail, perceptual hash
    categories.py      site taxonomy -> our tree
  api/main.py          FastAPI routes
  web/                 Jinja templates + one CSS file
tests/
  test_parsing.py      price/MOQ/tier/GTIN/model-code parsing
  test_pipeline.py     end-to-end: 4 listings -> 2 products, offline
  test_provider.py     provider path resolution + field mapping
  test_rejections.py   human match rulings survive rematch and re-crawl
  test_adapters.py     replay captured site HTML through the real adapters
  fixtures/<site>/     captured search.html + detail.html + manifest.json
providers.yaml         API presets for the no-login route
```

### Adding a site

Write one file in `sourcehub/scrapers/`, subclass `SiteAdapter`, implement
`search()` and `fetch_detail()` returning `RawOffer`, register it in
`registry.py`, add it to `seed.py` and `config.yaml`. Normalization, FX,
translation, image handling, matching and categorization are all handled
downstream — the adapter only returns raw, as-listed values.

---

## Tests

```bash
.\.venv\Scripts\python tests\test_parsing.py
.\.venv\Scripts\python tests\test_pipeline.py
.\.venv\Scripts\python tests\test_provider.py
.\.venv\Scripts\python tests\test_rejections.py
.\.venv\Scripts\python tests\test_adapters.py
.\.venv\Scripts\python tests\test_concurrency.py
.\.venv\Scripts\python tests\test_imagesearch.py
.\.venv\Scripts\python tests\test_export_bom.py
.\.venv\Scripts\python tests\test_health.py
.\.venv\Scripts\python tests\test_supplier_duty.py
.\.venv\Scripts\python tests\test_variants.py
.\.venv\Scripts\python tests\test_admin_auth.py
```

### Adapter fixtures

Selector rot is the main ongoing cost of running this, and it is invisible: a
crawl just quietly stops finding things. So capture each site's real HTML once
and replay it offline.

```bash
.venv/bin/python -m sourcehub.cli selftest --site dhgate --save-fixture
```

That saves `search.html`, `detail.html` and a manifest under
`tests/fixtures/<site>/`, and re-parses them immediately -- a fixture the adapter
cannot read is not worth keeping. `tests/test_adapters.py` then replays every
saved fixture with no network, asserting each page yields unique product ids,
absolute URLs, and sane ratios of priced/imaged listings.

Sites without a fixture report as **SKIP**, never as passes -- a green run that
silently tested nothing would be worse than a red one. `sourcehub.cli fixtures`
lists what you have and what is missing.

Be clear about what this proves. It catches a change *you* make breaking parsing,
offline and in CI, and when a site does change it gives you a diff of exactly what
moved. It cannot tell you whether your selectors match the site *today* -- a
fixture is a snapshot, and only `selftest` against the live site answers that.
The two are complementary.

`test_pipeline.py` runs the real pipeline against a temp database with the
network stubbed: it asserts that three listings across AliExpress, 1688 and
DHgate — in two languages — collapse into one product while an unrelated product
does not, that tiered pricing picks the tier applying at MOQ, that landed costs
are right, that agent links appear only where an agent is required, and that
re-ingesting updates in place rather than duplicating.

---

## Known limits

- Adapter selectors are best-effort against sites that change frequently. Run
  `selftest` first and expect periodic maintenance; this is inherent to scraping,
  not a defect you can fix once.
- Product *variants* (colour, capacity) usually share supplier photography and
  will often merge into one page. For price comparison this is normally what you
  want; if you need per-variant pages, raise `auto_merge_threshold` and work the
  review queue.
- Shipping cost is only as good as what each site discloses on the page, which is
  frequently nothing until checkout. `landed_cost_usd` is therefore a floor, not
  a quote.
- Agent fee estimates use conservative flat defaults and are labelled estimates.
- Scraping these sites is generally against their terms of service. You are
  responsible for how you use this.

---

## Sourcing tools

**Reverse image search** (`/search/image`, or `image-search` on the CLI). Upload a
product photo and find every marketplace selling it. This works because these sites
overwhelmingly reuse the *same supplier photograph* across resellers, so an
identical hash usually means an identical product. Results report a perceptual-hash
distance and are labelled honestly: **identical image** (0–2), **same photo** (3–10,
re-encoded or lightly edited), **visually similar** (beyond that — a lead, not a
match).

**Bulk sourcing / BOM** (`/bom`, or `bom` on the CLI). Paste a parts list and get
the cheapest source per line plus a total. Quantities are optional and parse from
either side (`esp32 x10`, `10 x esp32`, `esp32, 10`).

Totals use **landed cost, never unit price** — a line at $0.90 with MOQ 500 is a
$450 commitment, and summing unit prices would produce a number that looks great and
cannot be ordered. Where a line needs fewer units than the minimum order, the forced
overage is shown rather than hidden, and lines whose site does not disclose shipping
are counted separately so the total is understood as a floor.

**CSV export** of any search result or any product's full price table.

**Suppliers** are entities, not strings. `/supplier/{id}` shows everything one
seller lists. Identity is (site, normalized name) — there is no cross-site seller id
to join on, and pretending otherwise would merge unrelated companies that share a
name.

---

## Import duty

Duty is frequently the largest missing line item in a landed-cost comparison, so it
is supported — but it ships **switched off**, and that is deliberate. Rates depend on
the HTS classification of the specific good (which is genuinely hard), on origin and
trade programme, and they change; de-minimis treatment for China-origin goods in
particular has moved more than once.

So there are no rates in the box. You enter them in [`duty.yaml`](duty.yaml) from a
source you trust and stamp `as_of` with the date you checked. Off, the item page says
plainly that duty is excluded. On, every figure is labelled an estimate and the as-of
date is shown, with a warning once it goes stale. Nothing here is tax advice.

---

## Operations

**Admin access.** `/admin` mutates the catalog, so: with `SOURCEHUB_ADMIN_TOKEN` set
it requires HTTP Basic auth; unset and bound to loopback it is open; unset and
reachable from anywhere else it **refuses to serve** and tells you why. Failing
closed is the right default — silently serving an open admin panel on a public
interface is the outcome nobody wants. Mutating routes also reject cross-origin
posts. Basic auth sends the token near-clear over plain HTTP, so use it on a LAN or
behind a TLS-terminating proxy.

**Adapter health** (`health`, and a panel on `/admin`). A rotted selector does not
raise — the crawl reports success and finds nothing. This compares each site's recent
yield against its own history and reports `ok` / `degraded` / `broken` / `blocked` /
`idle`. "Broken" (worked last week, zero today) is deliberately distinguished from
"idle" (never worked), because they need different responses. It exits non-zero so it
can gate a cron job, and the scheduler logs it daily.

**Crawl concurrency** is real, not decorative: `concurrency` in `config.yaml` sets
how many product pages are fetched in parallel per site. Only the network half is
parallelized — ingest stays on one thread against one session, because SQLite takes
one writer and parallel writes would buy nothing but lock contention. The per-host
rate limiter is process-global, so N workers still start at most one request per
`delay_seconds`; the gain is that response latency overlaps the wait instead of
adding to it.

**robots.txt** awareness exists (`SOURCEHUB_RESPECT_ROBOTS`) and is off by default.
Every one of these sites disallows large parts of itself to crawlers, so enabling it
will stop most crawling. It is here so the choice is explicit rather than never
considered.

---

## Manual catalog fixes

The matcher is conservative, so real duplicates sometimes survive it and occasional
bad merges get through. Both are fixable from the UI:

- **Merge** — on a product page, fold this product's listings into another by slug.
- **Split** — on a product page, pull one listing out onto its own product. This
  also records a rejection, so the next crawl does not merge it straight back.
- **Undo** — every rejection is listed on `/admin` with an undo.

---

## Adapter status (verified live)

Checked against the real sites on 2026-08-30 with `selftest`. Re-check any time —
these change without notice, and `python -m sourcehub.cli health` will tell you when
one quietly stops yielding.

| Site | Status | Notes |
|---|---|---|
| AliExpress | ✅ 59/page, priced, images, **6 specs** | reads the `_init_data_` JSON blob; detail needs `render_detail: browser` |
| DHgate | ✅ 40/page, priced, **6–7 specs** | JSON-LD on the product page; search cards lazy-load photos |
| Banggood | ✅ 52/page, priced, images, **19 specs + brand** | `render: browser` — prices arrive by XHR |
| eBay | ✅ 63–76/page, priced, images | **US retail baseline.** Browse API with keys, browser fallback without |
| Made-in-China | ✅ 30/page, priced, images | |
| Geekbuying | ✅ 10/page, priced, images | |
| Alibaba | ✅ 3/page, priced, images | grid markup changes often; falls back to walking up from product links |
| Chinavasion | ⚠️ discovery only | on-site search is JS-only; uses the site's own product sitemap (~21k URLs). Prices partial — the main price is JS-injected |
| 1688 / Taobao / Tmall | 🔑 login required | see the browser/provider section above |
| Octopart | 🔑 key required | free Nexar key; skipped cleanly without one |
| **LCSC** | ❌ disabled | its public search endpoint moved and now answers HTTP 200 with an application-level 404. Adapter is correct once `SEARCH_API` points at the current path |
| **Global Sources** | ❌ disabled | search is a JS shell yielding no product cards even rendered. It previously "worked" only because a too-broad selector was ingesting the site *navigation* as products |
| **TOMTOP** | ❌ disabled | resets the TLS connection on every request including its homepage — site-level block or outage |
| GearBest | ❌ 403 | blocks scraping; the `.ma` storefront is largely defunct anyway |

A full crawl (1 page per site) ingests **~200 listings across 7 working sites**.

Deliberately **not** included: **Temu** (device fingerprinting and signed requests —
a stub that always returned zero would be worse than nothing, because it would sit
in the health report looking like a regression) and **JD.com / Pinduoduo** (gated
like Taobao; route them through the browser or provider drivers instead).

### Rendering### Rendering

Some storefronts build their listings client-side. `render: browser` in
`config.yaml` runs the page in Chromium so the XHRs finish before parsing:

```yaml
banggood:
  render: browser      # cards are server-rendered but prices arrive by XHR
```

Needs `playwright install chromium`. Browser-rendered sites are forced to enrich
serially regardless of `concurrency`, because Playwright's sync API is thread-bound
and a session cannot be driven from a worker thread.

---

## If HTTPS requests fail with CERTIFICATE_VERIFY_FAILED

That is almost never a network problem. Antivirus with HTTPS scanning (Avast, AVG,
Kaspersky, ESET…) and corporate middleboxes terminate every TLS connection and
re-sign it with their own root CA. That root sits in the **OS** trust store, so
browsers are happy, while Python uses `certifi` and rejects it.

```bash
.venv/bin/python -m sourcehub.cli trust-setup
```

It probes a live certificate to see whether anything is actually intercepting,
then fixes both HTTP stacks — `truststore` points `httpx`/`requests` at the OS
store, and a certifi-plus-local-root PEM is generated for `curl_cffi`, which is a
libcurl binding and ignores Python's trust entirely. Applied automatically from
then on.

Worth knowing: interception also **replaces the TLS fingerprint**, defeating the
JA3 impersonation `curl_cffi` exists to provide. Fixing trust makes requests
succeed, but anti-bot evasion stays weakened — DHgate refused every request under
interception and worked immediately once HTTPS scanning was turned off. If a
marketplace keeps refusing you, that is the first thing to check.

---

## Sourcing economics

Three things the price table alone cannot tell you, all on the item page.

**Is importing actually cheaper?** eBay is crawled as a **US retail baseline** and
labelled as one — you are not going to buy 500 units from an eBay listing, but you
do need to know that the $4 hub landing in three weeks competes with a $9 one
arriving Tuesday with a returns policy. The page states which wins at one unit, and
by how much.

**At what quantity does wholesale win?** A listing at $0.90 with MOQ 500 looks ten
times cheaper than one at $9.00 with MOQ 1, but you cannot buy one of it. The
break-even table evaluates landed cost at 1 / 5 / 10 / 25 … 5000 units and names the
crossover quantity. Because of the MOQ floor this is a step function, not a line, so
the forced overage is shown too ("buys 400 more than you need").

**Who is selling it?** Trust signals read rating, sales history, account age,
verification and price-versus-peers together and state **reasons**, never a score:
*"82% below the median of other listings, seller under a year old, almost no sales
history"* is actionable; "trust: 42/100" is not. Absence of a rating is reported as
*unknown*, not as a red flag — absence of evidence is not evidence of a problem.

Freight is estimated from **volumetric weight** (the greater of actual and
L×W×H/5000) rather than a flat fee, because couriers bill that way: a 0.3 kg bulky
parcel charges as 6 kg. Weight and dimensions are read from the spec sheet where the
listing publishes them, and guessed per category otherwise — flagged as guessed.
Tune in [`freight.yaml`](freight.yaml).

---

## Watches and alerts

```bash
python -m sourcehub.cli watch add <product-slug> --target 9.99 --landed
python -m sourcehub.cli watch add <product-slug> --restock
python -m sourcehub.cli watch list
python -m sourcehub.cli watch check
```

Watches target **products, not offers** — sellers delist and relist constantly, so a
watch pinned to an offer id would silently stop firing. They re-arm rather than
repeat: a watch that stayed triggered would notify on every crawl for as long as the
price stayed low, so it only fires again after the price recovers above the target.

`--landed` compares landed cost instead of unit price (a unit-price alert on a
MOQ-500 listing fires on a number you cannot pay). `--direct-only` ignores sites
needing a forwarding agent. `--restock` fires on availability instead of price.
`--webhook` POSTs to any Slack/Discord/Teams-compatible URL. Checked automatically
after every crawl, not on a separate schedule — an alert six hours late on a sold-out
listing is worthless.

---

## License

MIT - see [LICENSE](LICENSE).

One caveat that no license covers: this crawls marketplaces in ways that very
likely breach their terms of service. Robots.txt handling is off by default and
the rate limiter is deliberately slow. Check each site's terms and decide for
yourself before pointing it at anything.
