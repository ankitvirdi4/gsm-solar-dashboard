# GSM Solar — Fleet Dashboard

A lightweight, static dashboard for the solar-IoT fleet. No backend, no database
load per viewer: a scheduled ETL pre-aggregates the data into small JSON files,
and a single `index.html` renders it with Chart.js + Leaflet.

```
build.py  ──(hourly)──►  data/data.json + data/geo.json  ──►  index.html
   ▲                                                            (Chart.js / Leaflet)
   └── Atlas Online Archive (queried once per refresh, never by the browser)
```

## Why this shape
The Atlas endpoint is an **Online Archive** — large scans time out (~82 s) and
`distinct` is unsupported. So the browser must **never** query it. `build.py`
runs only cheap, date-bounded aggregations over the small rollup collections
(`dailygenerations`, `gateways`, `plants`, `alarms`) and writes ~9 KB + ~470 KB
of JSON. The page loads instantly and can be hosted on any static host.

## What it shows
- **KPIs:** devices reported yesterday, data-loggers online today (live), energy
  yesterday, month-to-date, open alarms, active plants.
- **Generation & active-device trend** (90 days).
- **Fleet reporting donut** (reporting in last 7 days vs. silent).
- **Top plants** by 30-day generation.
- **Alarms per day** (30 days) + **top alarm types** (7 days).
- **Plant map** (~13.7k geo-located plants, clustered).

> Timestamps are IST (UTC+5:30). The current day is in progress, so "today"
> figures are partial and live; the trend charts exclude the in-progress day so
> the line doesn't dip misleadingly. Yesterday is the reliable headline number.

## Run it

```bash
# 1. one-time: install the driver (already present on this machine)
python3 -m pip install --user pymongo

# 2. build the data snapshot (connects to Mongo once)
cd dashboard
python3 build.py

# 3. serve the static files (fetch() needs http://, not file://)
python3 -m http.server 8000
# open http://localhost:8000
```

The connection string is read from the `MONGODB_URI` env var, with the project
default baked in. Prefer the env var so credentials aren't committed:

```bash
export MONGODB_URI='mongodb://USER:PASS@...mongodb.net/?ssl=true&authSource=admin'
python3 build.py
```

## Refresh hourly (local cron)

```cron
0 * * * * cd /Users/ankit/Downloads/GSM/dashboard && /usr/bin/python3 build.py >> build.log 2>&1
```

## Deploy — private, via Cloudflare Pages + Access

The hourly build runs in **GitHub Actions**; the site is served by **Cloudflare
Pages** and gated by **Cloudflare Access** (team-only login). The repo is
private and the DB credential lives only in GitHub Secrets — never in the code.
The workflow is `.github/workflows/refresh.yml`.

### One-time setup

**1. GitHub repo secrets** (repo → Settings → Secrets and variables → Actions):
| Secret | Value |
|---|---|
| `MONGODB_URI` | the Atlas connection string |
| `CLOUDFLARE_API_TOKEN` | a token with **Account → Cloudflare Pages → Edit** |
| `CLOUDFLARE_ACCOUNT_ID` | from the Cloudflare dashboard URL / Workers & Pages overview |

**2. Create the Cloudflare Pages project** (once) — either in the dashboard
(Workers & Pages → Create → Pages → "Direct Upload", name it
`gsm-solar-dashboard`), or via CLI:
```bash
npx wrangler pages project create gsm-solar-dashboard --production-branch=main
```

**3. Turn on Cloudflare Access** (this is what makes it private):
Cloudflare dashboard → **Zero Trust → Access → Applications → Add application →
Self-hosted** → point it at your `*.pages.dev` hostname → add a policy that
**Allows** only your team (e.g. emails ending `@yourcompany.com`, or a specific
allow-list). Until this policy exists, the `pages.dev` URL is publicly reachable.

**4. Trigger it:** push to `main`, or run the workflow manually (Actions →
Refresh & deploy dashboard → Run workflow). It then redeploys every hour.

### Why this shape
- GitHub Pages on Free/Pro is always a **public** site, so it can't host a
  team-only dashboard. Cloudflare Access provides a real login gate for free
  (≤50 users) while GitHub Actions still does the scheduled build.
- Only `index.html` + `data/*.json` are published (the workflow copies them into
  `dist/`); `build.py` and secrets stay in the private repo.

## Hosting (alternatives)
The `dashboard/` folder is fully static — it can also go on any web server,
S3+CloudFront, etc. Only `build.py` ever holds the DB credential (server-side).

## Files
| File | Purpose |
|---|---|
| `build.py` | ETL — connects to Mongo, writes `data/*.json` |
| `index.html` | the dashboard (single file, CDN libs) |
| `data/data.json` | pre-aggregated KPIs, trends, alarms (~9 KB) |
| `data/geo.json` | plant coordinates for the map (~470 KB) |
