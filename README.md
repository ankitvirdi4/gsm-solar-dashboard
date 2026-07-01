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
- **KPIs:** devices reported yesterday, data-loggers online today (live),
  open alarms, offline devices (silent >7d), forwarding-queue depth, energy
  yesterday, month-to-date, active plants.
- **GSM / WiFi / All toggle** (top-right, defaults to **GSM**) — filters the
  generation trend and reporting-consistency panels to that gateway radio type.
- **Generation & active-device trend** (90 days), per selected radio type.
- **Reporting consistency (7d)** — of the devices "active" this week, how many
  reported daily (6–7 days) vs. intermittently vs. *only once*. (Replaces the
  old binary "reporting vs silent" donut, which counted a single ping as active.)
- **Connectivity mix** — actively-reporting *devices* by radio type (WiFi vs
  cellular/GSM), classified from actual generation data (each device's gateway
  `deviceType`), with the registered installed-base shown alongside. NB: don't
  use `gateways.updatedAt` for this — it bumps unreliably and undercounts WiFi.
- **New devices onboarded / week** (8 weeks).
- **Top plants** by 30-day generation.
- **Alarms per day** (30 days) + **top alarm types** (7 days).
- **Data quality** — corrupt `generation` values excluded + worst-offending
  devices (see note below).
- **Fleet & pipeline health** — silent devices (>7d / >30d), forwarding-queue
  backlog, throttled devices.
- **Plant map** (~13.7k geo-located plants, clustered).

> Timestamps are IST (UTC+5:30). The current day is in progress, so "today"
> figures are partial and live; the trend charts exclude the in-progress day so
> the line doesn't dip misleadingly. Yesterday is the reliable headline number.

### Data-quality handling
A small number of devices write garbage into `dailygenerations.generation`
(float-overflow sentinels ~`3.4e38 = 2¹²⁸`, plus negatives). Because the charts
SUM per day, a single bad record can pin a whole day to 1e38. `build.py` excludes
any value outside `[0, GEN_MAX]` (1 GWh/day) from every generation sum, and the
Data-quality panel reports how many were dropped and which devices produced them.
The raw DB is still polluted at the source — see the cleanup tool below.

## Tools
`tools/cleanup_bad_generation.py` — removes the corrupt generation values from
your **read-write primary cluster** (the Online Archive used by the dashboard is
read-only and can't delete). Dry-run by default; writes a JSON backup before any
change; `--strategy zero` (keep record, null the value) or `--strategy delete`.
```bash
export MONGODB_RW_URI='mongodb+srv://USER:PASS@your-primary.mongodb.net'
python tools/cleanup_bad_generation.py            # dry run — shows what it would do
python tools/cleanup_bad_generation.py --apply    # zero out the bad values (backup first)
```

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
