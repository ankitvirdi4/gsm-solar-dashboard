#!/usr/bin/env python3
"""
Export every registered slave device that is NOT reporting, enriched with as much
device/plant/owner/gateway detail as possible.

"Not reporting" = no daily-generation record in the last 7 days (IST). Each row is
tagged status = 'never_reported' (no record ever) or 'silent' (reported before,
gone quiet), with the last-seen date and days-since.

Writes to the PARENT folder (outside the git repo) because it contains customer
PII (names, emails, phones, GPS). Do NOT commit the CSV.

  cd dashboard && python tools/export_nonreporting.py [--days 7] [--out PATH]
"""
import os
import csv
import sys
import argparse
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import build  # URI loader (build.py in the dashboard dir)

IST = timezone(timedelta(hours=5, minutes=30))


def ist_date(dt):
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%Y-%m-%d")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="silent window in days (default 7)")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                   "..", "..", "non_reporting_devices.csv"))
    a = ap.parse_args()

    db = MongoClient(build.URI, serverSelectionTimeoutMS=60000, socketTimeoutMS=300000)["test"]
    now = datetime.now(timezone.utc)
    today0 = now.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    cut = today0 - timedelta(days=a.days)
    # Mongo returns naive UTC datetimes — compare against naive versions
    today0_n = today0.replace(tzinfo=None)
    cut_n = cut.replace(tzinfo=None)

    print("Loading last-reported per device from dailygenerations ...")
    last = {}
    for r in db.dailygenerations.aggregate([
            {"$group": {"_id": "$slaveDeviceId", "last": {"$max": "$date"}, "n": {"$sum": 1}}}],
            allowDiskUse=True, maxTimeMS=180000):
        last[r["_id"]] = (r["last"], r["n"])
    print(f"  {len(last)} devices have reported at least once")

    print("Loading plants / users / gateways ...")
    plants = {p["_id"]: p for p in db.plants.find({}, {
        "plantName": 1, "plantAddress": 1, "lat": 1, "long": 1})}
    users = {u["_id"]: u for u in db.users.find({}, {
        "name": 1, "email": 1, "mob": 1, "phone": 1, "mobile": 1, "type": 1})}
    gws = {g["_id"]: g for g in db.gateways.find({}, {
        "macId": 1, "deviceType": 1, "assignedSlavesCount": 1, "updatedAt": 1})}
    print(f"  plants={len(plants)} users={len(users)} gateways={len(gws)}")

    def model_of(slave):
        for p in (slave.get("parameters") or [])[:1]:
            m = p.get("MODEL NAME")
            if m:
                return m
        return ""

    rows, never, silent = [], 0, 0
    for s in db.slaves.find({}):
        sid = s["_id"]
        rep = last.get(sid)
        last_dt = rep[0] if rep else None
        # skip devices that ARE reporting within the window
        if last_dt is not None and last_dt >= cut_n:
            continue
        status = "never_reported" if last_dt is None else "silent"
        if status == "never_reported":
            never += 1
        else:
            silent += 1
        days_since = "" if last_dt is None else (today0_n - last_dt).days
        pl = plants.get(s.get("plantId"), {})
        us = users.get(s.get("userId"), {})
        gw = gws.get(s.get("gatewayId"), {})
        rows.append({
            "slave_id": str(sid),
            "serial_no": s.get("serialNo", ""),
            "nickname": s.get("nickName", ""),
            "modbus_slave_id": s.get("slaveId", ""),
            "model": model_of(s),
            "status": status,
            "last_reported_ist": ist_date(last_dt),
            "days_since_report": days_since,
            "lifetime_daily_records": rep[1] if rep else 0,
            "registered_at_ist": ist_date(s.get("createdAt")),
            "plant_name": pl.get("plantName", ""),
            "plant_address": pl.get("plantAddress", ""),
            "plant_lat": pl.get("lat", ""),
            "plant_long": pl.get("long", ""),
            "owner_name": us.get("name", ""),
            "owner_email": us.get("email", ""),
            "owner_phone": us.get("mob") or us.get("phone") or us.get("mobile") or "",
            "owner_type": us.get("type", ""),
            "gateway_mac": gw.get("macId", ""),
            "gateway_type": gw.get("deviceType", ""),
            "gateway_last_contact_ist": ist_date(gw.get("updatedAt")),
            "gateway_assigned_slaves": gw.get("assignedSlavesCount", ""),
            "plant_id": str(s.get("plantId", "")),
            "user_id": str(s.get("userId", "")),
            "gateway_id": str(s.get("gatewayId", "")),
        })

    # sort: never-reported first, then longest-silent
    rows.sort(key=lambda r: (r["status"] != "never_reported",
                             -(r["days_since_report"] or 0) if isinstance(r["days_since_report"], int) else 0))

    out = os.path.abspath(a.out)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print(f"\nWrote {len(rows)} non-reporting devices -> {out}")
    print(f"  never_reported: {never}   silent(>{a.days}d): {silent}")


if __name__ == "__main__":
    main()
