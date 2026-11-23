"""Reads a prof run: py -3.14 summarize_prof.py RESULT_FOLDER [RESULT_FOLDER ...]

First what the run reached: blocks the miners found and the nodes accepted per second, apart from
the blocks the cuts registered and executed, and finalized TPS; then the views, nil votes and
timeouts, and how each build and check got its cut prefix (held, awaited, ran) with the waits.
Then for each validator the 10 s PROF intervals inside the measuring window, per installed cut:
code paths by CPU and by wall time, disk writes and syncs by the code that asked for them, and the
RocksDB statistics (user, WAL, flush and compaction bytes, WAL syncs and how long they took).
"""

import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from bench_analyze import parse

PROF = re.compile(r"^PROF (\d+) (\S+) (\S+) (.*)$")
PROFDB = re.compile(r"^PROFDB (\d+) (\S+) (\S+)$")
PROFHIST = re.compile(r"^PROFHIST (\d+) (\S+) (.*)$")
VALUE = re.compile(r"(\w+)=(\S+)")
TICKERS = [
    "rocksdb.bytes.written", "rocksdb.wal.bytes", "rocksdb.wal.synced", "rocksdb.write.wal",
    "rocksdb.flush.write.bytes", "rocksdb.compact.write.bytes", "rocksdb.compact.read.bytes",
    "rocksdb.number.keys.written", "rocksdb.bytes.read", "rocksdb.block.cache.miss",
    "rocksdb.block.cache.hit", "rocksdb.stall.micros",
]
HISTOGRAMS = ["rocksdb.wal.file.sync.micros", "rocksdb.db.write.micros", "rocksdb.db.flush.micros",
              "rocksdb.compaction.times.micros", "rocksdb.sst.write.micros",
              "rocksdb.compaction.outfile.sync.micros", "rocksdb.manifest.file.sync.micros"]


def window_of(folder):
    result = json.loads((folder / "result.json").read_text())
    window = result.get("window")
    return (window[0] * 1000, window[1] * 1000) if window else (0, float("inf")), result


def read_node(path, window):
    sites = defaultdict(lambda: defaultdict(float))
    tickers = defaultdict(float)
    histograms = defaultdict(lambda: defaultdict(float))
    intervals = set()
    with open(path, errors="replace") as file:
        for line in file:
            if not line.startswith("PROF"):
                continue
            line = line.rstrip("\n")
            if match := PROF.match(line):
                stamp, name, site, rest = match.groups()
                if not window[0] <= int(stamp) <= window[1]:
                    continue
                intervals.add(stamp)
                key = name if site == "-" else f"{name} @ {site.split('src')[-1].lstrip('/').lstrip(chr(92))}"
                for field, value in VALUE.findall(rest):
                    if field == "max_ms":
                        sites[key][field] = max(sites[key][field], float(value))
                    else:
                        sites[key][field] += float(value)
            elif match := PROFDB.match(line):
                stamp, name, value = match.groups()
                if window[0] <= int(stamp) <= window[1]:
                    tickers[name] += float(value)
            elif match := PROFHIST.match(line):
                stamp, name, rest = match.groups()
                if not window[0] <= int(stamp) <= window[1]:
                    continue
                values = dict(VALUE.findall(rest))
                histograms[name]["n"] += float(values["n"])
                histograms[name]["sum"] += float(values["sum"])
                histograms[name]["max"] = max(histograms[name]["max"], float(values["max"]))
                histograms[name]["p99"] = max(histograms[name]["p99"], float(values["p99"]))
    return sites, tickers, histograms, len(intervals)


def installs(path, window):
    events, _ = parse(path)
    return sum(1 for when, name, _ in events if name == "install" and window[0] <= when * 1000 <= window[1])


def load(folder):
    folder = Path(folder)
    window, result = window_of(folder)
    nodes = {}
    for log in sorted(folder.glob("v*.log")):
        sites, tickers, histograms, intervals = read_node(log, window)
        nodes[log.stem] = {"sites": sites, "tickers": tickers, "histograms": histograms,
                           "intervals": intervals, "cuts": max(1, installs(log, window))}
    return result, nodes


def per_cut(node, value):
    return value / node["cuts"]


MINED = re.compile(r"(\d+) blocks in the last 5s: (\d+) accepted(?:.*?, (\d+) stale)?")


def mining(folder, window):
    """Blocks found and accepted per second over all miners, from their 5 s tallies."""
    found = accepted = stale = 0
    for log in folder.glob("miner-*.log"):
        for line in log.read_text(errors="replace").splitlines():
            match = MINED.search(line)
            if not match:
                continue
            when = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").timestamp()
            if window[0] / 1000 <= when <= window[1] / 1000:
                found += int(match.group(1))
                accepted += int(match.group(2))
                stale += int(match.group(3) or 0)
    return found, accepted, stale


DROPPED = [
    ("would repeat its transactions", "second find on a job whose transactions are already in a block"),
    ("came too late: ", None),
    ("was already known", "already known to the node"),
    ("trying again", "submission retried"),
    ("no template from the node", "no template from the node"),
]


def dropped(folder, window):
    """Why blocks were not accepted, from miners that ran with -d."""
    reasons = defaultdict(int)
    for log in folder.glob("miner-*.log"):
        for line in log.read_text(errors="replace").splitlines():
            for needle, label in DROPPED:
                if needle not in line:
                    continue
                try:
                    when = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").timestamp()
                except ValueError:
                    break
                if window[0] / 1000 <= when <= window[1] / 1000:
                    reasons[label or "node: " + line.split(needle, 1)[1].strip()] += 1
                break
    return reasons


def prefixes(log, window):
    """How builds and checks got their prefix, and how long the runs ahead took."""
    sources = defaultdict(list)
    ahead = []
    for when, name, fields in parse_prefix(log):
        if not window[0] <= when * 1000 <= window[1]:
            continue
        if name == "used":
            sources[fields["source"]].append(int(fields["millis"]))
        else:
            ahead.append(int(fields["millis"]))
    return sources, ahead


def parse_prefix(log):
    stamp = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)")
    field = re.compile(r"(\w+)=(\S+)")
    with open(log, errors="replace") as file:
        for line in file:
            if "cut prefix used" not in line and "cut prefix executed ahead" not in line:
                continue
            match = stamp.match(line)
            if not match:
                continue
            when = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S.%f").timestamp()
            fields = {key: value.strip('"') for key, value in field.findall(line)}
            yield when, "used" if "cut prefix used" in line else "ahead", fields


def spread(values):
    if not values:
        return "-"
    ordered = sorted(values)
    return (f"n {len(ordered)}, median {ordered[len(ordered) // 2]} ms, "
            f"p90 {ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]} ms, max {ordered[-1]} ms")


def report(folder):
    folder = Path(folder)
    result, nodes = load(folder)
    window, _ = window_of(folder)
    span = result.get("window_s") or 1
    print(f"== {folder}  ({result.get('scenario')}, {result.get('cuts')} cuts in {span:.0f} s, "
          f"{result.get('cuts_per_min', 0):.1f} cuts/min)")
    found, accepted, stale = mining(folder, window)
    print(f"   mining: {found / span:.1f} blocks/s found, {accepted / span:.1f} accepted, {stale / span:.1f} stale")
    for reason, count in sorted(dropped(folder, window).items(), key=lambda item: -item[1]):
        print(f"      {count / span:5.1f}/s {reason}")
    print(f"   finality: {result.get('bps_registered', 0):.1f} blocks/s registered, "
          f"{result.get('bps_executed', 0):.1f} executed, {result.get('tps_finalized', 0):.1f} TPS finalized "
          f"of {result.get('offered_tps', 0):.0f} offered, latency {result.get('finality_latency_s')}")
    print(f"   views: {result.get('views')}, heights above view 0: {result.get('views_above_zero')}")
    for name, stats in result.get("nodes", {}).items():
        print(f"   {name}: nil votes {stats.get('nil_votes')}, timeouts {stats.get('timeouts')}")
    for log in sorted(folder.glob("v*.log")):
        sources, ahead = prefixes(log, window)
        if not sources and not ahead:
            continue
        print(f"   {log.stem} prefix: ahead {spread(ahead)}")
        for source in ("held", "awaited", "ran"):
            print(f"      {source:8} {spread(sources.get(source, []))}")
    for name, node in nodes.items():
        sites = node["sites"]
        print(f"\n-- {name}: {node['cuts']} cuts installed, {node['intervals']} intervals of 10 s")
        print("   code paths by CPU per cut (ms):           cpu    wall     max    runs/cut  "
              "reads/cut  misses  in misses")
        ranked = sorted(((key, stat) for key, stat in sites.items() if "cpu_ms" in stat),
                        key=lambda item: -item[1]["cpu_ms"])
        for key, stat in ranked[:25]:
            print(f"   {key[:40]:40} {per_cut(node, stat['cpu_ms']):7.1f} {per_cut(node, stat['wall_ms']):7.1f} "
                  f"{stat['max_ms']:7.1f} {per_cut(node, stat['n']):9.1f} {per_cut(node, stat.get('reads', 0)):10.0f} "
                  f"{per_cut(node, stat.get('misses', 0)):7.0f} {per_cut(node, stat.get('miss_ms', 0)):8.1f}")
        print("   waits and disk by wall per cut (ms):       wall     max    runs/cut   KB/cut")
        waits = sorted(((key, stat) for key, stat in sites.items()
                        if key.startswith(("lock.", "db.", "cut.prefix.wait")) and "wall_ms" in stat),
                       key=lambda item: -item[1]["wall_ms"])
        for key, stat in waits[:25]:
            print(f"   {key[:40]:40} {per_cut(node, stat['wall_ms']):7.1f} {stat['max_ms']:7.1f} "
                  f"{per_cut(node, stat['n']):9.1f} {per_cut(node, stat.get('bytes', 0)) / 1024:8.1f}")
        seconds = max(1, node["intervals"] * 10)
        for key in sorted(key for key in sites if key.startswith(("mining.", "live."))):
            stat = sites[key]
            mean = f", mean {stat['wall_ms'] / stat['n']:.1f} ms, max {stat['max_ms']:.0f} ms" if stat.get(
                "wall_ms") else ""
            cpu = f", CPU {stat['cpu_ms'] / stat['n']:.1f} ms each" if stat.get("cpu_ms") else ""
            size = f", {stat['bytes'] / stat['n']:.1f} on average" if stat.get("bytes") else ""
            print(f"   {key}: {stat['n'] / seconds:.1f}/s{mean}{cpu}{size}")
        reads = sites.get("db.node_read", {})
        if reads:
            print(f"   state node reads per cut: {per_cut(node, reads.get('n', 0)):.0f}, "
                  f"misses {per_cut(node, reads.get('misses', 0)):.0f}, "
                  f"{per_cut(node, reads.get('read_ms', 0)):.1f} ms ({per_cut(node, reads.get('miss_ms', 0)):.1f} ms in misses)")
        tickers = node["tickers"]
        print("   rocksdb per cut: " + ", ".join(
            f"{name.replace('rocksdb.', '')} {per_cut(node, tickers.get(name, 0)) / (1024 if 'bytes' in name else 1):.1f}"
            f"{' KB' if 'bytes' in name else ''}" for name in TICKERS if name in tickers))
        for name in HISTOGRAMS:
            stat = node["histograms"].get(name)
            if stat and stat["n"]:
                print(f"   {name.replace('rocksdb.', '')}: {stat['n']:.0f} in the window, "
                      f"mean {stat['sum'] / stat['n'] / 1000:.1f} ms, p99 {stat['p99'] / 1000:.1f} ms, "
                      f"max {stat['max'] / 1000:.1f} ms")


if __name__ == "__main__":
    for folder in sys.argv[1:]:
        report(folder)
