#!/usr/bin/env python3
"""
GSM-only (modem) operations snapshot -> data/gsm.json

Metrics (GSM/cellular devices ONLY — WiFi excluded entirely):
  1. Deployed        : all slaves on GSM gateways
  2. Active today    : devices with data today (IST)
  3. Offline list    : devices NOT active today, with their last working day
  4. Boot-up storms  : NOT AVAILABLE — boot-up messages are not captured in the DB

"Active/last-seen" combines fast signals (dailygenerations, dashboardDatas) and,
for the residual with no fast signal, a per-device raw-telemetry check
(deviceHexDatas + deviceDatas) run in parallel.
"""
import os, sys, json, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pymongo import MongoClient
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build

IST = timezone(timedelta(hours=5, minutes=30))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "gsm.json")
WORKERS = 5   # archive returns false-None on the sorted telemetry lookup under high concurrency

def ist(dt, fmt="%Y-%m-%d %H:%M"):
    if not dt: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime(fmt)
def norm(dt): return dt.replace(tzinfo=None) if dt and dt.tzinfo else dt

def main():
    cli = MongoClient(build.URI, serverSelectionTimeoutMS=60000, socketTimeoutMS=120000, maxPoolSize=WORKERS+8)
    db = cli["test"]
    now = datetime.now(timezone.utc)
    today0 = now.astimezone(IST).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)

    print("loading gateways/slaves/plants ...", flush=True)
    gw = {g["_id"]: g for g in db.gateways.find({}, {"deviceType":1,"macId":1})}
    gsm_gw = {gid for gid,g in gw.items() if g.get("deviceType") == "gsm"}
    plants = {p["_id"]: p for p in db.plants.find({}, {"plantName":1,"plantAddress":1})}
    users  = {u["_id"]: u for u in db.users.find({}, {"name":1,"mob":1})}
    SLFIELDS = {"serialNo":1,"nickName":1,"slaveId":1,"plantId":1,"gatewayId":1,"userId":1,"createdAt":1}
    gsm_slaves = [s for s in db.slaves.find({}, SLFIELDS) if s.get("gatewayId") in gsm_gw]
    print(f"  GSM deployed devices: {len(gsm_slaves)}", flush=True)

    # LAST-SEEN signals. NOTE: dashboardDatas.previousDate is blanket-reset to
    # today for every device, so it's useless as a last-seen — use updatedAt,
    # which retains the real last-touch date, combined with dailygenerations.
    print("last-seen signals: dailygenerations + dashboardDatas.updatedAt ...", flush=True)
    dg = {r["_id"]: norm(r["last"]) for r in db.dailygenerations.aggregate(
        [{"$group":{"_id":"$slaveDeviceId","last":{"$max":"$date"}}}], allowDiskUse=True, maxTimeMS=150000)}
    dash = {}
    for d in db.dashboardDatas.find({}, {"slaveDeviceId":1,"updatedAt":1}):
        sid=d.get("slaveDeviceId"); u=norm(d.get("updatedAt"))
        if sid and u: dash[sid]=u

    last = {}   # sid -> last-seen datetime (naive UTC)
    residual = []
    for s in gsm_slaves:
        sid = s["_id"]
        f = max([x for x in (dg.get(sid), dash.get(sid)) if x], default=None)
        if f is not None:
            last[sid] = f          # real last-seen date (today OR an earlier day = offline)
        else:
            residual.append(sid)   # NO signal at all -> raw-telemetry check (hex-only or never)
    print(f"  {len(gsm_slaves)-len(residual)} have a last-seen date; hex-checking {len(residual)} with no signal ...", flush=True)

    def one(coll, sid):
        for attempt in range(2):   # retry once: distinguish a real absence from a flaky timeout
            try:
                d = db[coll].find_one({"slaveDeviceId": sid}, {"hr":1}, sort=[("hr",-1)], max_time_ms=45000)
                return norm(d["hr"]) if d and d.get("hr") else None
            except Exception:
                if attempt == 1: return None
        return None
    def telem_last(sid):
        vals = [v for v in (one("deviceHexDatas", sid), one("deviceDatas", sid)) if v]
        return sid, (max(vals) if vals else None)
    t0=time.time(); n=0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for fut in as_completed([ex.submit(telem_last, sid) for sid in residual]):
            sid, hr = fut.result()
            # keep the best of fast-signal and telemetry
            prev = max([x for x in (dg.get(sid), dash.get(sid)) if x], default=None)
            last[sid] = max([x for x in (prev, hr) if x], default=None)
            n+=1
            if n % 200 == 0: print(f"    {n}/{len(residual)} ({time.time()-t0:.0f}s)", flush=True)

    # assemble
    active_today = 0; offline = []
    for s in gsm_slaves:
        sid = s["_id"]; ls = last.get(sid)
        if ls is not None and ls >= today0:
            active_today += 1; continue
        pl = plants.get(s.get("plantId"), {}); u = users.get(s.get("userId"), {}); g = gw.get(s.get("gatewayId"), {})
        offline.append({
            "serial_no": s.get("serialNo",""), "nickname": s.get("nickName",""),
            "plant_name": pl.get("plantName",""), "plant_address": pl.get("plantAddress",""),
            "gateway_imei": g.get("macId",""), "owner_name": u.get("name",""), "owner_phone": u.get("mob") or "",
            "last_working_day": ist(ls, "%Y-%m-%d") if ls else None,
            "days_offline": (today0 - ls).days if ls else None,
            "last_seen_ist": ist(ls) if ls else None,
        })
    # most-recently-offline first; never-seen last
    offline.sort(key=lambda r: (r["days_offline"] is None, r["days_offline"] if r["days_offline"] is not None else 0))

    snap = {
        "generated_at_ist": now.astimezone(IST).strftime("%Y-%m-%d %H:%M IST"),
        "today_ist": now.astimezone(IST).strftime("%Y-%m-%d"),
        "deployed": len(gsm_slaves),
        "active_today": active_today,
        "offline_count": len(offline),
        "offline": offline,
        "bootups": {"available": False,
                    "note": "Boot-up / restart messages are not captured in the database. To enable this, the backend must persist each device boot/birth event (device id + timestamp) — e.g. a `deviceEvents` collection written when the modem reconnects. Then this panel can list devices with >1 boot/day and the boot times."},
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f: json.dump(snap, f, separators=(",", ":"), default=str)
    with_day = sum(1 for r in offline if r["days_offline"] is not None)
    print(f"\nWrote {OUT}  ({time.time()-t0:.0f}s)", flush=True)
    print(f"  deployed={snap['deployed']}  active_today={active_today}  offline={len(offline)}", flush=True)
    print(f"  offline with a real last-working-day={with_day}  never-reported={len(offline)-with_day}", flush=True)
    pb = next((r for r in offline if r["serial_no"]=="001260101010106305300002602000760"), None)
    print(f"  Pb udyog-22 check (should show a real last day): {pb['last_working_day'] if pb else 'not in offline set'}", flush=True)

if __name__ == "__main__":
    main()
