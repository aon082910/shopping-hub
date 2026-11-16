# Shopping Hub

Shopping Hub compares the same product across marketplaces like AliExpress, Alibaba, 1688, DHgate, LCSC, eBay and Best Buy. Each product gets one page with every site's price, minimum order quantity and shipping, translated to English, plus a US retail price so you can tell whether importing is actually cheaper.

What it does well:

- **Matches products across sites**, so you see one item instead of fifteen listings.
- **Shows the real cost**, including minimums, fees and a US baseline.
- **Handles forwarding agents** for sites that won't ship to the US.

Image: [allornothing/shopping-hub](https://hub.docker.com/r/allornothing/shopping-hub) · Source: [aon082910/shopping-hub](https://github.com/aon082910/shopping-hub)

## Install

**Unraid:** search **shopping-hub** in Apps, set an admin token, and keep `--shm-size=1g` in Extra Parameters. The full walkthrough is in `UNRAID.md`.

**Docker:**

```bash
docker run -d --name shopping-hub -p 8000:8000 --shm-size=1g \
  -v ./appdata:/config \
  -e SOURCEHUB_ADMIN_TOKEN=change-me \
  allornothing/shopping-hub:latest
```

Open `http://127.0.0.1:8000`. There's a `docker-compose.yml` in the repo too.

| Setting | Default | What it's for |
|---|---|---|
| Port | `8000` | Web UI |
| `/config` | your appdata folder | Database and settings |
| `SOURCEHUB_ADMIN_TOKEN` | none | Admin access |
| `SOURCEHUB_PROXY` | none | Optional residential proxy for heavy crawling |

Use the `slim` tag if you don't want the bundled browser (about 0.3 GB instead of 1.2 GB). It skips eBay, Banggood, 1688, Taobao and Tmall.

## Read before crawling

Taobao, Tmall and 1688 need a logged-in session, and most marketplaces rate-limit scrapers and may forbid it in their terms. The defaults are deliberately slow. Check each site's terms and crawl politely.

## Help

[Open an issue](https://github.com/aon082910/shopping-hub/issues). MIT licensed.
