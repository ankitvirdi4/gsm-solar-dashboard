#!/usr/bin/env python3
"""
GSM Solar Dashboard — ETL snapshot builder.

Connects to the Atlas Online Archive ONCE, runs only cheap date-bounded
aggregations, and writes small pre-aggregated JSON files into ./data/.
The dashboard (index.html) reads those JSON files — it never touches Mongo.

Run hourly (cron / launchd / GitHub Action). See README.md.

IMPORTANT about this data source:
  * It is an Atlas ONLINE ARCHIVE — large/unbounded scans TIME OUT (~82s) and
    the `distinct` command is unsupported. Every query below is date-bounded
    and uses allowDiskUse. Do not add unbounded scans of deviceDatas (11M docs).
  * All timestamps are IST (UTC+5:30) stored as UTC. Start-of-day IST = the
    previous calendar date at 18:30:00Z. We bucket with timezone "+05:30".
"""
import os
import sys
import json
import time
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_uri():
    """Resolve the Mongo URI from the environment, or a local gitignored
    .secret.env file (KEY=VALUE lines). Never hard-coded, never committed."""
    uri = os.environ.get("MONGODB_URI")
    if uri:
        return uri
    secret = os.path.join(HERE, ".secret.env")
    if os.path.exists(secret):
        with open(secret) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() == "MONGODB_URI":
                        return v.strip().strip('"').strip("'")
    sys.exit(
        "ERROR: set the MONGODB_URI env var, or put it in dashboard/.secret.env "
        "(MONGODB_URI=mongodb://...). It must never be committed to git."
    )


URI = _load_uri()
OUT_DIR = os.path.join(HERE, "data")
IST = timezone(timedelta(hours=5, minutes=30))

# Data-quality guard: a small number of devices write garbage into `generation`
# (float-overflow sentinels ~3.4e38 = 2**128, and negative values). A real
# inverter never exceeds a few MWh/day, so anything outside [0, GEN_MAX] is junk
# and is excluded from every generation SUM so it can't poison the totals.
GEN_MAX = 1_000_000  # kWh/day per device (1 GWh — far above any real device)
VALID_GEN = {"generation": {"$gte": 0, "$lte": GEN_MAX}}


def ist_midnight_utc(d: datetime) -> datetime:
    """Start of the IST calendar day containing `d`, expressed in UTC."""
    local = d.astimezone(IST)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc)


def write(name: str, obj) -> None:
    path = os.path.join(OUT_DIR, name)
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"), default=str)
    print(f"  wrote {name} ({os.path.getsize(path)/1024:.1f} KB)")


def section(label, fn, default=None):
    """Run one ETL section; never let a single failure abort the snapshot."""
    t = time.time()
    try:
        out = fn()
        print(f"[ok]  {label}  ({time.time()-t:.1f}s)")
        return out
    except Exception as e:  # noqa: BLE001
        print(f"[ERR] {label}: {type(e).__name__}: {str(e)[:120]}  ({time.time()-t:.1f}s)")
        return default


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    client = MongoClient(URI, serverSelectionTimeoutMS=60000, socketTimeoutMS=300000)
    db = client["test"]

    now = datetime.now(timezone.utc)
    today0 = ist_midnight_utc(now)            # start of today (IST) in UTC
    yest0 = today0 - timedelta(days=1)
    tomo0 = today0 + timedelta(days=1)
    month0 = ist_midnight_utc(now).replace(day=1)
    cut7 = today0 - timedelta(days=7)
    cut30 = today0 - timedelta(days=30)
    cut90 = today0 - timedelta(days=89)       # 90 inclusive days

    # ---- registered fleet totals (instant) -------------------------------
    def _reg():
        return {
            "plants": db.plants.estimated_document_count(),
            "slaves": db.slaves.estimated_document_count(),
            "gateways": db.gateways.estimated_document_count(),
            "gateways_assigned": db.gateways.count_documents({"assignedSlavesCount": {"$gt": 0}}),
        }
    reg = section("registered totals", _reg, {}) or {}

    # ---- connectivity ----------------------------------------------------
    def _conn():
        def active_devices(lo, hi=None):
            m = {"date": {"$gte": lo}} if hi is None else {"date": {"$gte": lo, "$lt": hi}}
            p = [{"$match": m}, {"$group": {"_id": "$slaveDeviceId"}}, {"$count": "n"}]
            r = list(db.dailygenerations.aggregate(p, allowDiskUse=True))
            return r[0]["n"] if r else 0

        def gw_seen(lo, assigned_only=True):
            q = {"updatedAt": {"$gte": lo}}
            if assigned_only:
                q["assignedSlavesCount"] = {"$gt": 0}
            return db.gateways.count_documents(q)

        return {
            # inverter devices that have reported a daily-generation record
            "active_today": active_devices(today0, tomo0),          # partial day
            "active_yesterday": active_devices(yest0, today0),
            "active_7d": active_devices(cut7),
            "active_30d": active_devices(cut30),
            # data-logger (gateway) heartbeat — live "last contact" snapshot
            "gw_today": gw_seen(today0),
            "gw_24h": gw_seen(today0 - timedelta(days=1)),
            "gw_7d": gw_seen(cut7),
            "gw_30d": gw_seen(cut30),
        }
    conn = section("connectivity", _conn, {}) or {}

    # ---- generation totals ----------------------------------------------
    def _gen():
        def gsum(lo, hi=None):
            m = dict(VALID_GEN)
            m["date"] = {"$gte": lo} if hi is None else {"$gte": lo, "$lt": hi}
            p = [{"$match": m}, {"$group": {"_id": None, "g": {"$sum": "$generation"}}}]
            r = list(db.dailygenerations.aggregate(p, allowDiskUse=True))
            return round(r[0]["g"], 1) if r else 0
        return {
            "today_kwh": gsum(today0, tomo0),
            "yesterday_kwh": gsum(yest0, today0),
            "month_kwh": gsum(month0),
        }
    gen = section("generation totals", _gen, {}) or {}

    # ---- alarms ----------------------------------------------------------
    def _alarms_open():
        return db.alarms.count_documents({"acknowledge": False}, maxTimeMS=60000)
    alarms_open = section("open alarms", _alarms_open, None)

    def _alarms_top():
        p = [
            {"$match": {"createdAt": {"$gte": cut7}}},
            {"$group": {"_id": {"$toString": "$parameter"}, "n": {"$sum": 1}}},
            {"$sort": {"n": -1}}, {"$limit": 8},
        ]
        return [{"type": r["_id"], "count": r["n"]}
                for r in db.alarms.aggregate(p, allowDiskUse=True, maxTimeMS=90000)]
    alarms_top = section("top alarm types (7d)", _alarms_top, []) or []

    def _alarms_daily():
        p = [
            {"$match": {"createdAt": {"$gte": cut30}}},
            {"$group": {"_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$createdAt", "timezone": "+05:30"}},
                        "n": {"$sum": 1}}},
            {"$sort": {"_id": 1}},
        ]
        return [{"date": r["_id"], "count": r["n"]}
                for r in db.alarms.aggregate(p, allowDiskUse=True, maxTimeMS=120000)]
    alarms_daily = section("alarms per day (30d)", _alarms_daily, []) or []

    # ---- daily generation + active-device trend (90 days) ----------------
    def _daily():
        # count ALL reporting devices, but only sum generation values in range
        valid_gen = {"$cond": [
            {"$and": [{"$gte": ["$generation", 0]}, {"$lte": ["$generation", GEN_MAX]}]},
            "$generation", 0]}
        p = [
            {"$match": {"date": {"$gte": cut90}}},
            {"$group": {
                "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date", "timezone": "+05:30"}},
                "gen": {"$sum": valid_gen},
                "devices": {"$sum": 1},                 # 1 doc per device per day
                "plants": {"$addToSet": "$plantId"},
            }},
            {"$project": {"gen": {"$round": ["$gen", 1]}, "devices": 1, "plants": {"$size": "$plants"}}},
            {"$sort": {"_id": 1}},
        ]
        return [{"date": r["_id"], "gen": r["gen"], "devices": r["devices"], "plants": r["plants"]}
                for r in db.dailygenerations.aggregate(p, allowDiskUse=True, maxTimeMS=120000)]
    daily = section("daily trend (90d)", _daily, []) or []

    # ---- top plants by generation (30 days) ------------------------------
    def _top_plants():
        p = [
            {"$match": {"date": {"$gte": cut30}, **VALID_GEN}},
            {"$group": {"_id": "$plantId", "gen": {"$sum": "$generation"}}},
            {"$sort": {"gen": -1}}, {"$limit": 12},
        ]
        rows = list(db.dailygenerations.aggregate(p, allowDiskUse=True, maxTimeMS=90000))
        ids = [r["_id"] for r in rows]
        names = {d["_id"]: d.get("plantName", "—")
                 for d in db.plants.find({"_id": {"$in": ids}}, {"plantName": 1})}
        return [{"name": names.get(r["_id"], str(r["_id"])[-6:]), "gen": round(r["gen"], 1)} for r in rows]
    top_plants = section("top plants (30d)", _top_plants, []) or []

    # ---- data quality: corrupt generation records excluded ---------------
    def _dq():
        bad = db.dailygenerations.count_documents(
            {"$or": [{"generation": {"$gt": GEN_MAX}}, {"generation": {"$lt": 0}}]},
            maxTimeMS=60000)
        return {"excluded_bad_generation": bad, "gen_cap_kwh": GEN_MAX}
    dq = section("data quality", _dq, {}) or {}

    # ---- geo (plants with valid coordinates) -----------------------------
    def _geo():
        pts = []
        cur = db.plants.find(
            {"lat": {"$nin": ["0", "0.0", "", None]}},
            {"plantName": 1, "lat": 1, "long": 1},
        )
        for d in cur:
            try:
                la = float(d.get("lat")); lo = float(d.get("long"))
            except (TypeError, ValueError):
                continue
            # plausible India bounds; drops 0/0 and garbage
            if 6 <= la <= 38 and 67 <= lo <= 98:
                pts.append([round(la, 5), round(lo, 5), (d.get("plantName") or "")[:40]])
        return pts
    geo = section("plant geo points", _geo, []) or []

    # ---- assemble + write -----------------------------------------------
    snapshot = {
        "generated_at": now.isoformat(),
        "generated_at_ist": now.astimezone(IST).strftime("%Y-%m-%d %H:%M IST"),
        "registered": reg,
        "connectivity": conn,
        "generation": gen,
        "alarms_open": alarms_open,
        "alarms_top": alarms_top,
        "alarms_daily": alarms_daily,
        "daily": daily,
        "top_plants": top_plants,
        "data_quality": dq,
        "geo_count": len(geo),
    }
    print("\nWriting snapshot files:")
    write("data.json", snapshot)
    write("geo.json", {"generated_at": now.isoformat(), "points": geo})
    print(f"\nDone. {len(geo)} geo points, {len(daily)} trend days.")


if __name__ == "__main__":
    sys.exit(main())
