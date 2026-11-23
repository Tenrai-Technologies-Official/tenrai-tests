"""Per run, the last block tally of every miner: accepted, rejected, stale, and the spread.

A block the node refuses because its pool already holds half its limit reaches the miner as a 400
answer and is counted as rejected, so the rejected column bounds those refusals from above.

    miner_outcomes.py <results folder> [<results folder>...]
"""
import pathlib
import re
import statistics
import sys

TALLY = re.compile(r"blocks (\d+) accepted, (\d+) rejected, (\d+) stale")
REASON = re.compile(r"rejected[^:]*: (.*)$")


def last_tally(path):
    tally = None
    reasons = {}
    with open(path, "rb") as handle:
        for raw in handle:
            line = raw.decode("utf-8", "replace").rstrip()
            found = TALLY.search(line)
            if found:
                tally = tuple(int(group) for group in found.groups())
            elif "rejected" in line and "accepted," not in line:
                reason = REASON.search(line)
                key = reason.group(1)[:80] if reason else line[20:100]
                reasons[key] = reasons.get(key, 0) + 1
    return tally, reasons


def spread(values):
    if not values:
        return "-"
    mean = statistics.mean(values)
    low, high = min(values), max(values)
    deviation = statistics.pstdev(values) / mean * 100 if mean else 0.0
    return f"min {low} max {high} mean {mean:.1f} cv {deviation:.1f} %"


def report(folder):
    folder = pathlib.Path(folder)
    miners = sorted(folder.glob("miner-*.log"))
    if not miners:
        return
    rows = []
    reasons = {}
    for path in miners:
        tally, found = last_tally(path)
        if tally is None:
            continue
        rows.append((path.stem[len("miner-"):], *tally))
        for key, count in found.items():
            reasons[key] = reasons.get(key, 0) + count
    if not rows:
        return
    accepted = [row[1] for row in rows]
    rejected = sum(row[2] for row in rows)
    stale = sum(row[3] for row in rows)
    found = sum(accepted) + rejected + stale
    nodes = {}
    for row in rows:
        node = row[0].split("-")[0]
        nodes[node] = nodes.get(node, 0) + row[1]
    print(f"{folder.name}: {len(rows)} miners, {found} found, {sum(accepted)} accepted, "
          f"{rejected} rejected ({rejected / found * 100 if found else 0:.2f} %), "
          f"{stale} stale ({stale / found * 100 if found else 0:.2f} %)")
    print(f"  accepted per miner: {spread(accepted)}")
    if len(nodes) > 1:
        print(f"  accepted per node: {', '.join(f'{k} {v}' for k, v in sorted(nodes.items()))}; "
              f"{spread(list(nodes.values()))}")
    for key, count in sorted(reasons.items(), key=lambda item: -item[1])[:4]:
        print(f"  rejected line x{count}: {key}")


def main():
    for folder in sys.argv[1:]:
        report(folder)


if __name__ == "__main__":
    main()
