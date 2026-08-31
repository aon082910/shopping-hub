# Running SourceHub on Unraid

Everything below was built and verified against a real Docker daemon: the image
builds, runs as `nobody:users`, launches Chromium unprivileged, crawls live sites,
and its data survives container recreation.

---

## What you get

One container running the web UI **and** the crawl scheduler. It stores everything --
database, downloaded images, browser login profile, and the editable YAML config --
under a single `/config` mount you point at appdata.

| | |
|---|---|
| Image size | **~4.2 GB** |
| Memory | ~400 MB idle, ~1.2 GB while a browser-rendered site is crawling |
| Appdata | ~16 MB after a small crawl; grows mostly with downloaded images |
| Ports | one, default `8000` |
| Volumes | one, `/config` -> `/mnt/user/appdata/sourcehub` |
| Database | SQLite, inside `/config` -- no second container needed |

The image is large because four of the working adapters (Banggood, eBay, AliExpress
detail pages, Chinavasion) render their listings in JavaScript and need a real
Chromium. It is built on Microsoft's official Playwright image rather than
`python:slim` -- Chromium pulls in around 90 system libraries, and hand-assembling
those on a slim base is a brittle apt incantation that breaks on Debian point
releases.

---

## Get the image

The image is published, so you do not need to build anything:

```bash
docker pull allornothing/shopping-hub:latest
```

It is around 4.2 GB, so give the pull a few minutes on a normal connection.

<details>
<summary>Building it yourself instead</summary>

Clone the repo onto a share and build on the Unraid box:

```bash
git clone https://github.com/aon082910/shopping-hub.git /mnt/user/isos/shopping-hub
```

```bash
cd /mnt/user/isos/shopping-hub && docker build -t allornothing/shopping-hub:latest .
```

Roughly 3-5 minutes, most of it pulling the Playwright base layer. Tagging it with
the same name means the template and compose file work unchanged.

</details>

---

## Add the container

**Easiest: Community Applications.** Open the **Apps** tab and search for
**shopping-hub**. Everything below is already filled in; just set the admin token.
The manual routes are below for anyone not using CA.

**Option A -- the template (recommended).** On the Unraid box:

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/my-shopping-hub.xml \
  https://raw.githubusercontent.com/aon082910/Unraid-CA/main/Shopping-Hub/shopping-hub.xml
```

Then
**Docker -> Add Container** and pick **shopping-hub** from the template dropdown. Every
setting below is already filled in and described in the form.

**Option B -- by hand.** Docker -> Add Container, then:

| Field | Value |
|---|---|
| Name | `SourceHub` |
| Repository | `allornothing/shopping-hub:latest` |
| Network Type | `Bridge` |
| Port | Container `8000` -> Host `8000` |
| Path | Container `/config` -> Host `/mnt/user/appdata/sourcehub` (Read/Write) |
| Extra Parameters | `--shm-size=1g` |

Add these variables:

| Variable | Value | Why |
|---|---|---|
| `SOURCEHUB_ADMIN_TOKEN` | *a long random string* | **Read the next section.** Without it the admin pages refuse to serve |
| `TZ` | `America/New_York` | The crawl schedule is interpreted in this zone |
| `PUID` | `99` | Unraid's `nobody` -- keeps appdata editable from the host |
| `PGID` | `100` | Unraid's `users` |
| `SOURCEHUB_MODE` | `serve` | Web UI and scheduler in one process |

Start it, then open `http://<tower-ip>:8000`.

---

## The two settings that will bite you

### 1. `SOURCEHUB_ADMIN_TOKEN` -- admin is 403 without it

This is deliberate and it *will* look like a bug the first time. The admin pages
mutate your catalog (merging products, recording permanent match rejections), so they
refuse to serve when they are reachable from a non-loopback address and no token is
set. **In a container the client is never loopback** -- traffic arrives from the
Docker bridge -- so this fires on every containerised deployment, always.

Set the variable to any strong random string, restart, and sign in with it as the
**password** (any username works). Browsing, search, the API and crawling are
unaffected either way; only the pages that can modify data are gated.

Failing closed is the right default here: silently serving an open admin panel to
your whole LAN is the outcome nobody wants.

### 2. `--shm-size=1g` -- or browser-rendered sites go blank

Docker gives `/dev/shm` 64 MB by default. Chromium needs far more on heavy pages and
crashes when it runs out. The symptom is nasty because it is not an error: Banggood
and eBay simply return **zero listings**, which looks exactly like a broken selector.
Put `--shm-size=1g` in Extra Parameters and it goes away.

---

## First run

The container seeds `/config` on first start and says so:

```
[sourcehub] seeded config.yaml into /config (edit it there; the image copy is ignored)
[sourcehub] running as sourcehub (99:100), TZ=America/New_York
Database ready. 16 sites seeded.
[sourcehub] mode=serve (web UI + scheduler) on port 8000
[sourcehub] scheduled full_crawl       0 3 * * *
[sourcehub] scheduled refresh_prices   0 */6 * * *
```

(The `useradd warning: uid 99 outside of the UID_MIN 1000` line above it is normal --
Unraid's `nobody` is genuinely below the usual range.)

Your appdata now looks like this:

```
/mnt/user/appdata/sourcehub/
├── config.yaml          which sites to crawl, keywords, schedule, matching weights
├── providers.yaml       API presets for Taobao / Tmall / 1688
├── duty.yaml            import duty rates (shipped disabled -- see below)
├── freight.yaml         shipping weight and volumetric rates
├── .env                 optional; container variables override anything here
├── db/sourcehub.db      the catalog
├── media/               downloaded product images
├── fixtures/            saved HTML snapshots used by the offline tests
└── browser_profile/     saved logins for the domestic-China sites
```

**Edit the YAML files here, not in the image.** They survive rebuilds and upgrades.

To see the UI populated without waiting for a crawl:

```bash
docker exec -u 99:100 SourceHub python -m sourcehub.cli demo-seed
```

Then a real crawl:

```bash
docker exec -u 99:100 SourceHub python -m sourcehub.cli crawl --sites dhgate,banggood --keyword "usb c hub" --pages 2
```

That exact command was run inside the container during testing: 81 listings, zero
errors, Chromium included.

---

## Running the CLI

Everything works through `docker exec`. Use `-u 99:100` so anything it writes stays
owned correctly:

```bash
docker exec -u 99:100 SourceHub python -m sourcehub.cli health
```

```bash
docker exec -u 99:100 SourceHub python -m sourcehub.cli selftest --site dhgate
```

```bash
docker exec -u 99:100 SourceHub python -m sourcehub.cli stats
```

`health` is the one worth scheduling. It compares each site's recent yield against
its own history, so it distinguishes *broken* (worked last week, zero today) from
*idle* (never worked), and exits non-zero when something needs attention -- which
makes it a clean **User Scripts** cron job.

---

## Scheduled crawling

`SOURCEHUB_MODE=serve` runs the scheduler inside the web container. Five jobs are
registered at boot; timings live in `config.yaml` under `schedule:` and are read in
your `TZ`:

```yaml
schedule:
  full_crawl: "0 3 * * *"        # 03:00 daily
  refresh_prices: "0 */6 * * *"  # every 6 hours
  fx_rates: "30 2 * * *"
  rematch: "0 5 * * 0"           # Sundays
  health_check: "0 7 * * *"
```

Edit, then restart the container.

A full sweep of the default keyword list across all enabled sites takes roughly 20-40
minutes, and it is deliberately slow -- the per-host rate limiter spaces requests
2.5-6 s apart. Do not tighten it. You will get your home IP blocked by the same sites
you are trying to buy from.

To split the UI and the crawler into separate containers (handy if you want to
restart the UI without interrupting a crawl), run a second container from the same
image with `SOURCEHUB_MODE=schedule` and no port mapping, and set the first to
`SOURCEHUB_MODE=web`. Point both at the same `/config`.

---

## Optional variables

None of these are required to boot; the app skips whatever capability is missing.

| Variable | Effect if unset |
|---|---|
| `TRANSLATE_PROVIDER` + `ANTHROPIC_API_KEY` | Chinese listings are not translated, so they cannot be title-matched and mostly stay on their own product pages. `claude`, `deepl`, `google_free` or `none` |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | eBay falls back to browser scraping -- works, just slower. Free keys at developer.ebay.com |
| `SOURCEHUB_PROXY` | Crawls come from your home IP. Fine for light use; a residential proxy is strongly recommended for sustained crawling. One URL, or several comma-separated for round-robin |
| `CN_PROVIDER_KEY` | Taobao / Tmall / 1688 stay unavailable without a browser login. See below |
| `OCTOPART_CLIENT_ID` / `SECRET` | Electronic-component part-number lookup is skipped |

Translations are cached by content hash, so a repeat crawl of the same listings costs
nothing.

---

## Taobao, Tmall and 1688 in a container

These three need either a one-time interactive browser login or an API key, and the
login is genuinely awkward in a container because it wants a visible browser window.

**Recommended:** get an API key and set `CN_PROVIDER_KEY` (presets are in
`providers.yaml`). No login, no browser, works unattended -- which is what you want on
a server anyway. Verify it before enabling the sites:

```bash
docker exec SourceHub python -m sourcehub.cli provider-probe --preset otapi --keyword "usb hub"
```

**Alternative:** do the browser login on a desktop with the project checked out
normally, then copy the resulting `data/browser_profile/` into
`/mnt/user/appdata/sourcehub/browser_profile/`. The cookies are portable. Expect to
redo it every few weeks.

Until you have done one or the other, leave those three `enabled: false` in
`config.yaml`, otherwise every crawl wastes minutes failing on them.

---

## Backup

Back up `/mnt/user/appdata/sourcehub` -- that is the entire application state. The CA
Appdata Backup plugin covers it with no special configuration. `media/` is the bulk of
it and is regenerable by re-crawling, so exclude it if you want a small backup.

---

## Updating

```bash
docker pull allornothing/shopping-hub:latest
```

Then **Docker -> SourceHub -> Force Update**, or stop and start it.

Your YAML files in `/config` are never overwritten. A pristine copy of the defaults
lives at `/defaults` inside the image, so after an upgrade you can see what changed:

```bash
docker exec SourceHub diff /defaults/config.yaml /config/config.yaml
```

Settings added by an upgrade show as missing from your copy. Add them by hand.

---

## Troubleshooting

**Admin returns 403.** Set `SOURCEHUB_ADMIN_TOKEN`. Expected in a container -- see
above. With the token set you get a normal HTTP Basic prompt: any username, the token
as the password.

**Banggood or eBay return zero listings.** Almost always a missing `--shm-size=1g` in
Extra Parameters.

**Permission denied writing to appdata.** Check `PUID=99` / `PGID=100` and that the
share is not read-only. The entrypoint chowns `/config` at start and warns in the log
if it cannot.

**A site that used to work now finds nothing.** Run `health` (above) to confirm, then
`selftest --site <key>` to see the live failure. These sites change their markup
without notice; that is why both commands exist.

**Everything fails with CERTIFICATE_VERIFY_FAILED.** Something on your network is
intercepting TLS -- antivirus HTTPS scanning is the usual culprit:

```bash
docker exec SourceHub python -m sourcehub.cli trust-setup
```

It probes a live certificate and reports which authority actually signed it.

**Container restarts in a loop.** `docker logs SourceHub`. The entrypoint prints each
step as it happens, so the failing stage is normally the last line.

**Is it alive?** `http://<tower-ip>:8000/healthz` returns 200 when it is. That is also
the container's own HEALTHCHECK, so Unraid's Docker tab shows it as healthy.

---

## Notes on what it will not do

Duty ships **disabled with an empty rate table**, because rates depend on HTS
classification and change. Enable it in `duty.yaml` only if you have real numbers for
the goods you actually import -- a plausible-looking wrong duty rate is worse than
none.

Three adapters (LCSC, Tomtop, Global Sources) ship **disabled** with the reason
recorded in `config.yaml`. Global Sources in particular was ingesting navigation links
as products, which is worse than returning nothing.

And the tool will not make importing cheaper than it is. The eBay baseline exists
precisely because buying domestically sometimes wins, and the break-even page will
tell you when.
