"""Two runs of the storage load test side by side: their own report lines and each column family."""

import re
import sys

MIB = 2**20


def report(path):
    values = {}
    with open(path, encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    patterns = {
        "run s": r"run ([\d.]+) s",
        "install p50 ms": r"install ms p50 (\d+)",
        "install p90 ms": r"install ms p50 \d+ p90 (\d+)",
        "build p50 ms": r"build ms p50 (\d+)",
        "cpu s": r"process cpu ([\d.]+) s",
        "read MiB": r"read ([\d.]+) MiB in",
        "read calls": r"read [\d.]+ MiB in (\d+) calls",
        "write MiB (run)": r", write ([\d.]+) MiB in",
        "write MiB (with flush)": r"write ([\d.]+) MiB in total",
        "WAL GB": r"Cumulative WAL: .*written: ([\d.]+) GB",
        "store MiB": r"\(([\d.]+) MiB on disk\)",
    }
    for name, pattern in patterns.items():
        found = re.search(pattern, text)
        if found:
            values[name] = float(found.group(1))
    return values


def families(path):
    rows = {}
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) == 15 and re.match(r"^[a-z0-9_]+$", parts[0]) and parts[0] != "family":
                rows[parts[0]] = {
                    "written": float(parts[5]),
                    "kept": float(parts[12]) + float(parts[13]),
                }
    return rows


def main():
    first, second = sys.argv[1], sys.argv[2]
    names = (sys.argv[3], sys.argv[4]) if len(sys.argv) > 4 else ("A", "B")
    a, b = report(first + ".out"), report(second + ".out")
    print(f"{'':28} {names[0]:>12} {names[1]:>12} {'change':>8}")
    for key in a:
        if key in b:
            change = f"{100 * (b[key] - a[key]) / a[key]:+.0f} %" if a[key] else ""
            print(f"{key:28} {a[key]:12.1f} {b[key]:12.1f} {change:>8}")
    fa, fb = families(first + ".events"), families(second + ".events")
    print()
    print(f"{'GiB written / kept':28} {names[0]:>12} {names[1]:>12} {'change':>8}   {names[0]:>9} {names[1]:>9}")
    total = [0.0, 0.0, 0.0, 0.0]
    for family in sorted(set(fa) | set(fb), key=lambda name: -fa.get(name, {}).get("written", 0.0)):
        wa = fa.get(family, {}).get("written", 0.0)
        wb = fb.get(family, {}).get("written", 0.0)
        ka = fa.get(family, {}).get("kept", 0.0)
        kb = fb.get(family, {}).get("kept", 0.0)
        total = [total[0] + wa, total[1] + wb, total[2] + ka, total[3] + kb]
        if max(wa, wb, ka, kb) < 0.0005:
            continue
        change = f"{100 * (wb - wa) / wa:+.0f} %" if wa else ""
        print(f"{family:28} {wa:12.3f} {wb:12.3f} {change:>8}   {ka:9.3f} {kb:9.3f}")
    change = f"{100 * (total[1] - total[0]) / total[0]:+.0f} %" if total[0] else ""
    print(f"{'all':28} {total[0]:12.3f} {total[1]:12.3f} {change:>8}   {total[2]:9.3f} {total[3]:9.3f}")


main()
