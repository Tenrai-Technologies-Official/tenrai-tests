"""Bytes RocksDB wrote and keeps per column family, from the event lines of a database's LOG files."""

import argparse
import datetime
import glob
import json
import os
import re
from collections import defaultdict

EVENT = re.compile(r"EVENT_LOG_v1 (\{.*\})\s*$")
STAMP = re.compile(r"^(\d{4}/\d{2}/\d{2}-\d{2}:\d{2}:\d{2})")
GIB = 2**30


def log_files(folder):
    old = glob.glob(os.path.join(folder, "LOG.old.*"))
    old.sort(key=lambda path: int(path.rsplit(".", 1)[1]))
    return old + [os.path.join(folder, "LOG")]


def events(folder, since, until):
    for path in log_files(folder):
        # Job numbers start again with every open of the database.
        jobs = {}
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                found = EVENT.search(line)
                if not found:
                    continue
                stamp = STAMP.match(line)
                stamp = stamp.group(1) if stamp else None
                if stamp and ((since and stamp < since) or (until and stamp > until)):
                    continue
                try:
                    event = json.loads(found.group(1))
                except ValueError:
                    continue
                kind = event.get("event")
                if kind == "flush_started":
                    jobs[event.get("job")] = "flush"
                elif kind == "compaction_started":
                    jobs[event.get("job")] = "compaction"
                yield stamp, kind, jobs.get(event.get("job"), "recovery"), event


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", help="the RocksDB folder, for a node <data dir>/traidag.rocks")
    parser.add_argument("--since", help="first event, in YYYY/MM/DD-HH:MM:SS format")
    parser.add_argument("--until", help="last event, same form")
    parser.add_argument("--hourly", help="comma separated families to follow hour by hour")
    args = parser.parse_args()

    written = defaultdict(lambda: defaultdict(float))
    raw = defaultdict(float)
    entries = defaultdict(int)
    tombstones = defaultdict(int)
    read = defaultdict(float)
    live = {}
    blobs = {}
    hours = {}
    first = last = None
    for stamp, kind, job, event in events(args.folder, args.since, args.until):
        if stamp:
            first = first or stamp
            last = stamp
        family = event.get("cf_name")
        if kind == "compaction_started":
            read[family] += event.get("input_data_size", 0)
        elif kind == "table_file_creation":
            written[family][job] += event.get("file_size", 0)
            if job == "flush":
                properties = event.get("table_properties", {})
                raw[family] += properties.get("raw_key_size", 0) + properties.get("raw_value_size", 0)
                entries[family] += properties.get("num_entries", 0)
                tombstones[family] += properties.get("num_deletions", 0)
            live[event.get("file_number")] = (family, event.get("file_size", 0))
        elif kind == "table_file_deletion":
            live.pop(event.get("file_number"), None)
        elif kind == "blob_file_creation":
            written[family]["blob " + job] += event.get("total_blob_bytes", 0)
            blobs[event.get("file_number")] = (family, event.get("total_blob_bytes", 0))
        elif kind == "blob_file_deletion":
            blobs.pop(event.get("file_number"), None)
        if args.hourly and stamp:
            hour = stamp[:13]
            if hour not in hours:
                hours[hour] = None
            hours[hour] = (dict(live), dict(blobs))

    if not first:
        print("no events")
        return
    fmt = "%Y/%m/%d-%H:%M:%S"
    span = (datetime.datetime.strptime(last, fmt) - datetime.datetime.strptime(first, fmt)).total_seconds()
    per_hour = 3600 / span if span else 0.0
    print(f"events {first} to {last} ({span / 3600:.2f} h)")

    sst = defaultdict(float)
    for family, size in live.values():
        sst[family] += size
    blob = defaultdict(float)
    for family, size in blobs.values():
        blob[family] += size

    rows = sorted(written, key=lambda family: -sum(written[family].values()))
    total = sum(sum(parts.values()) for parts in written.values()) or 1
    print(
        f"{'family':27} {'flush':>8} {'compact':>8} {'blob fl':>8} {'blob gc':>8} {'total':>8} {'GiB/h':>7} "
        f"{'share':>6} {'W-amp':>6} {'raw fl':>8} {'entries':>11} {'tombst':>10} {'sst':>7} {'blob':>7} {'read':>8}"
    )
    for family in rows:
        parts = written[family]
        flush = parts.get("flush", 0.0)
        blob_flush = parts.get("blob flush", 0.0)
        family_total = sum(parts.values())
        amplification = family_total / (flush + blob_flush) if flush + blob_flush else 0.0
        print(
            f"{family:27} {flush / GIB:8.3f} {parts.get('compaction', 0.0) / GIB:8.3f} {blob_flush / GIB:8.3f} "
            f"{parts.get('blob compaction', 0.0) / GIB:8.3f} {family_total / GIB:8.3f} {family_total / GIB * per_hour:7.3f} "
            f"{100 * family_total / total:6.1f} {amplification:6.1f} {raw[family] / GIB:8.3f} {entries[family]:11d} "
            f"{tombstones[family]:10d} {sst[family] / GIB:7.3f} {blob[family] / GIB:7.3f} {read[family] / GIB:8.3f}"
        )
    print(
        f"written {total / GIB:.2f} GiB ({total / GIB * per_hour:.2f} GiB/h, {total / 2**20 / span if span else 0:.2f} MiB/s), "
        f"kept {sum(sst.values()) / GIB:.2f} GiB in tables and {sum(blob.values()) / GIB:.2f} GiB in blob files, "
        f"compactions read {sum(read.values()) / GIB:.2f} GiB"
    )

    if args.hourly:
        watched = args.hourly.split(",")
        print("hour          " + " ".join(f"{name[:12]:>12}" for name in watched) + "   all GiB")
        for hour, (tables, blob_files) in hours.items():
            sizes = defaultdict(float)
            for family, size in tables.values():
                sizes[family] += size
            for family, size in blob_files.values():
                sizes[family] += size
            print(f"{hour} " + " ".join(f"{sizes.get(name, 0) / GIB:12.3f}" for name in watched) + f" {sum(sizes.values()) / GIB:9.2f}")


main()
