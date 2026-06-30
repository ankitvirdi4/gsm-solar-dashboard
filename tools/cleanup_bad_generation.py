#!/usr/bin/env python3
"""
Clean up corrupted `generation` values (float-overflow garbage ~3.4e38 = 2**128,
and negatives) from the rollup collections — on your READ-WRITE PRIMARY cluster.

  ⚠️  The Atlas Online Archive endpoint used by the dashboard is READ-ONLY and
      CANNOT delete. Point this at your live/primary cluster instead.

SAFETY MODEL
  * DRY RUN by default — it only prints what it *would* change. Nothing is
    modified unless you pass --apply.
  * Refuses to run against an `online-archive` URI (wrong, read-only target).
  * Before any modification it writes a JSON backup of every affected document
    so the change is reversible.
  * Two strategies:
      --strategy zero    (default) set the bad generation value to 0, KEEPING the
                         record (preserves the "device reported that day" signal).
      --strategy delete  remove the whole record.
  * Scoped strictly to generation > --cap OR generation < 0. It never touches
    valid records, devices, plants, or users.

USAGE
  export MONGODB_RW_URI='mongodb+srv://USER:PASS@your-primary-cluster.mongodb.net'
  python tools/cleanup_bad_generation.py                       # dry run
  python tools/cleanup_bad_generation.py --apply               # zero out (default)
  python tools/cleanup_bad_generation.py --apply --strategy delete
  python tools/cleanup_bad_generation.py --collections dailygenerations,weeklyGenerations,monthlyGenerations
"""
import os
import sys
import json
import argparse
from datetime import datetime, timezone

from pymongo import MongoClient
from bson import json_util


def parse_args():
    ap = argparse.ArgumentParser(description="Remove corrupted generation values (dry run by default).")
    ap.add_argument("--apply", action="store_true",
                    help="actually modify the DB (omit for a dry run)")
    ap.add_argument("--strategy", choices=["zero", "delete"], default="zero",
                    help="'zero' sets bad generation to 0 (default, keeps record); 'delete' removes the record")
    ap.add_argument("--collections", default="dailygenerations",
                    help="comma-separated collections to clean (default: dailygenerations)")
    ap.add_argument("--cap", type=float, default=1_000_000.0,
                    help="max plausible kWh/day per device; above this (or <0) is junk")
    ap.add_argument("--db", default="test")
    ap.add_argument("--backup-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups"))
    return ap.parse_args()


def main():
    a = parse_args()
    uri = os.environ.get("MONGODB_RW_URI")
    if not uri:
        sys.exit("ERROR: set MONGODB_RW_URI to your READ-WRITE primary cluster connection string.")
    if "online-archive" in uri:
        sys.exit("ERROR: that is the read-only Online Archive endpoint. Use the live/primary cluster URI.")

    bad_filter = {"$or": [{"generation": {"$gt": a.cap}}, {"generation": {"$lt": 0}}]}
    client = MongoClient(uri, serverSelectionTimeoutMS=30000)
    db = client[a.db]
    colls = [c.strip() for c in a.collections.split(",") if c.strip()]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    mode = "APPLY" if a.apply else "DRY-RUN"
    print(f"[{mode}] strategy={a.strategy} cap={a.cap:g} db={a.db} collections={colls}\n")

    grand = 0
    for coll in colls:
        c = db[coll]
        n = c.count_documents(bad_filter)
        grand += n
        # distinct affected devices
        devs = c.distinct("slaveDeviceId", bad_filter) if n else []
        print(f"• {coll}: {n} corrupt records across {len(devs)} devices")
        if n == 0:
            continue
        # sample
        for d in c.find(bad_filter).limit(3):
            print(f"    e.g. _id={d['_id']} device=…{str(d.get('slaveDeviceId'))[-8:]} "
                  f"date={d.get('date')} generation={d.get('generation'):.3e}")

        if not a.apply:
            print("    (dry run — no changes)\n")
            continue

        # backup every affected doc BEFORE touching anything
        os.makedirs(a.backup_dir, exist_ok=True)
        bpath = os.path.join(a.backup_dir, f"{coll}.bad.{stamp}.json")
        with open(bpath, "w") as f:
            for d in c.find(bad_filter):
                f.write(json_util.dumps(d) + "\n")
        print(f"    backup written: {bpath}")

        if a.strategy == "zero":
            res = c.update_many(bad_filter, {"$set": {"generation": 0, "corrected": True}})
            print(f"    zeroed {res.modified_count} records (set generation=0, corrected=true)\n")
        else:
            res = c.delete_many(bad_filter)
            print(f"    deleted {res.deleted_count} records\n")

    print(f"TOTAL corrupt records {'modified' if a.apply else 'found'}: {grand}")
    if not a.apply:
        print("\nThis was a DRY RUN. Re-run with --apply to make changes "
              "(a JSON backup is written first; default strategy keeps the record and zeroes the value).")


if __name__ == "__main__":
    main()
