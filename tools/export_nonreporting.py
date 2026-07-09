#!/usr/bin/env python3
"""
Export registered slave devices NOT publishing any data.

v3: "publishing" judged by RAW TELEMETRY `deviceDatas.hr`, not the derived
`dailygenerations` rollup (some devices stream telemetry but never produce a
generation rollup, e.g. Pb udyog-22 / IMEI 862407082014733 — those were false
positives in v1). deviceDatas full-scans time out on the Online Archive, but
per-device lookups are fast, so we PARALLELIZE them across the flagged set.

Active = dailygenerations OR deviceDatas within --days (default 7). Only devices
with neither are exported. Writes to parent folder (PII — do not commit).
  cd dashboard && python tools/export_nonreporting.py [--days 7] [--workers 24]
"""
import os, sys, csv, argparse, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pymongo import MongoClient
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import build

IST = timezone(timedelta(hours=5, minutes=30))
def ist(dt):
    if not dt: return ""
    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "non_reporting_devices.csv"))
    a = ap.parse_args()
    cli = MongoClient(build.URI, serverSelectionTimeoutMS=60000, socketTimeoutMS=120000, maxPoolSize=a.workers+8)
    db = cli["test"]
    now = datetime.now(timezone.utc)
    today0 = now.astimezone(IST).replace(hour=0,minute=0,second=0,microsecond=0).astimezone(timezone.utc)
    cut_n = (today0 - timedelta(days=a.days)).replace(tzinfo=None); today_n = today0.replace(tzinfo=None)

    def norm(dt): return dt.replace(tzinfo=None) if dt and dt.tzinfo else dt

    print("signal 1: dailygenerations last-date per device ...", flush=True)
    dg_last = {}
    for r in db.dailygenerations.aggregate([{"$group":{"_id":"$slaveDeviceId","last":{"$max":"$date"}}}],
                                           allowDiskUse=True, maxTimeMS=180000):
        dg_last[r["_id"]] = r["last"]
    print(f"  {len(dg_last)} devices ever in dailygenerations", flush=True)

    print("signal 2: dashboardDatas last-seen per device ...", flush=True)
    dash_last = {}
    for d in db.dashboardDatas.find({}, {"slaveDeviceId":1,"previousDate":1,"updatedAt":1}):
        sid = d.get("slaveDeviceId"); cand = [norm(x) for x in (d.get("previousDate"), d.get("updatedAt")) if x]
        if sid and cand: dash_last[sid] = max(cand)
    print(f"  {len(dash_last)} devices in dashboardDatas", flush=True)

    print("loading plants/users/gateways/slaves ...", flush=True)
    plants = {p["_id"]: p for p in db.plants.find({}, {"plantName":1,"plantAddress":1,"lat":1,"long":1})}
    users  = {u["_id"]: u for u in db.users.find({}, {"name":1,"email":1,"mob":1,"type":1})}
    gws    = {g["_id"]: g for g in db.gateways.find({}, {"macId":1,"deviceType":1,"assignedSlavesCount":1,"updatedAt":1})}
    slaves = list(db.slaves.find({}))

    # active via generation rollup OR dashboard state -> definitely active (cheap signals)
    def cheap_active(sid):
        return (dg_last.get(sid) and dg_last[sid] >= cut_n) or (dash_last.get(sid) and dash_last[sid] >= cut_n)
    flagged = [s for s in slaves if not cheap_active(s["_id"])]
    active_dg   = sum(1 for s in slaves if dg_last.get(s["_id"]) and dg_last[s["_id"]] >= cut_n)
    active_dash = len(slaves) - len(flagged) - active_dg
    print(f"  active via dailygenerations={active_dg}, additionally via dashboardDatas={active_dash}", flush=True)
    print(f"  RESIDUAL: checking {len(flagged)} against raw deviceDatas ({a.workers} workers, ~{len(flagged)/3/60:.0f} min) ...", flush=True)

    def last_telemetry(sid):
        try:
            d = db.deviceDatas.find_one({"slaveDeviceId": sid}, {"hr":1}, sort=[("hr",-1)], max_time_ms=30000)
            return sid, (d.get("hr") if d else None)
        except Exception:
            return sid, "ERR"

    dd_last = {}
    done = t0 = time.time(); n = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for fut in as_completed([ex.submit(last_telemetry, s["_id"]) for s in flagged]):
            sid, hr = fut.result(); dd_last[sid] = hr; n += 1
            if n % 500 == 0:
                print(f"    {n}/{len(flagged)} telemetry-checked  ({time.time()-t0:.0f}s)", flush=True)

    rows = []; never = silent = active_dd = errs = 0
    for s in flagged:
        sid = s["_id"]; dg_dt = dg_last.get(sid); dd_dt = dd_last.get(sid); dash_dt = dash_last.get(sid)
        if dd_dt == "ERR": errs += 1; dd_dt = None
        if dd_dt is not None and dd_dt >= cut_n:
            active_dd += 1; continue
        last_any = max([d for d in (dg_dt, dd_dt, dash_dt) if d is not None], default=None)
        status = "never_any_data" if last_any is None else "silent"
        never += status == "never_any_data"; silent += status == "silent"
        pl = plants.get(s.get("plantId"), {}); us = users.get(s.get("userId"), {}); gw = gws.get(s.get("gatewayId"), {})
        rows.append({
            "slave_id": str(sid), "serial_no": s.get("serialNo",""), "nickname": s.get("nickName",""),
            "modbus_slave_id": s.get("slaveId",""), "status": status,
            "last_telemetry_ist": ist(dd_dt), "last_generation_ist": ist(dg_dt), "last_dashboard_ist": ist(dash_dt),
            "days_since_last_data": "" if last_any is None else (today_n - last_any).days,
            "registered_at_ist": ist(s.get("createdAt")),
            "plant_name": pl.get("plantName",""), "plant_address": pl.get("plantAddress",""),
            "plant_lat": pl.get("lat",""), "plant_long": pl.get("long",""),
            "owner_name": us.get("name",""), "owner_email": us.get("email",""),
            "owner_phone": us.get("mob") or "", "owner_type": us.get("type",""),
            "gateway_mac": gw.get("macId",""), "gateway_type": gw.get("deviceType",""),
            "gateway_last_contact_ist": ist(gw.get("updatedAt")),
            "plant_id": str(s.get("plantId","")), "user_id": str(s.get("userId","")), "gateway_id": str(s.get("gatewayId","")),
        })
    rows.sort(key=lambda r: (r["status"] != "never_any_data", -(r["days_since_last_data"] or 0) if isinstance(r["days_since_last_data"], int) else 0))
    out = os.path.abspath(a.out)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    print(f"\nWrote {len(rows)} non-reporting -> {out}  ({time.time()-t0:.0f}s)", flush=True)
    print(f"  never_any_data={never}  silent={silent}  (telemetry-check errors={errs})", flush=True)
    print(f"  EXCLUDED as active: {active_dg} dailygenerations + {active_dash} dashboardDatas + {active_dd} raw telemetry", flush=True)
    print(f"  >>> v1 FALSE POSITIVES corrected: {active_dash + active_dd} devices were active but flagged non-reporting", flush=True)

if __name__ == "__main__":
    main()
