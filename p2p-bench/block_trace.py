"""Follows every block of a prof run from where it was mined to the other validators' pools:
py -3.14 block_trace.py RESULT_FOLDER

Reads the BLK lines (prof_patches.py). The node that took a block first is where it was mined. For
each other validator it measures the steps on the way: mined to heard (the announcement), heard to
asked (the fetch), asked to got (the body arrived), got to the first try (waiting for the one
checking slot), first try to in the pool (the checks, and every retry after a busy engine lock).
It also says how many tries a block needed, how the retries were spaced, and how many blocks a
validator did not hold after 1 s and 2.5 s.
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

BLK = re.compile(r"^BLK (\d+) (\w+) ([0-9a-f]{16})$")
STEPS = [("mined", "heard"), ("heard", "asked"), ("asked", "got"), ("got", "try"), ("try", "accepted"),
         ("got", "accepted"), ("mined", "accepted")]
LABELS = {
    ("mined", "heard"): "mined -> announcement heard",
    ("heard", "asked"): "heard -> asked for",
    ("asked", "got"): "asked -> body received",
    ("got", "try"): "received -> first check starts",
    ("try", "accepted"): "first check -> in the pool",
    ("got", "accepted"): "received -> in the pool",
    ("mined", "accepted"): "mined -> in the pool here",
}


def spread(values):
    if not values:
        return "-"
    ordered = sorted(values)
    pick = lambda share: ordered[min(len(ordered) - 1, int(len(ordered) * share))]
    return (f"n {len(ordered):5d}  median {pick(0.5):6.0f}  p90 {pick(0.9):6.0f}  p99 {pick(0.99):6.0f}  "
            f"max {ordered[-1]:6.0f} ms")


def load(folder, window):
    """node -> block -> kind -> times, inside the window."""
    events = {}
    for log in sorted(folder.glob("v*.log")):
        blocks = defaultdict(lambda: defaultdict(list))
        with open(log, errors="replace") as file:
            for line in file:
                if not line.startswith("BLK "):
                    continue
                match = BLK.match(line.strip())
                if match and window[0] <= int(match.group(1)) <= window[1]:
                    blocks[match.group(3)][match.group(2)].append(int(match.group(1)))
        events[log.stem] = blocks
    return events


def main():
    folder = Path(sys.argv[1])
    result = json.loads((folder / "result.json").read_text())
    window = result.get("window")
    window = (window[0] * 1000, window[1] * 1000) if window else (0, float("inf"))
    events = load(folder, window)
    mined = {}
    for node, blocks in events.items():
        for block, kinds in blocks.items():
            if kinds.get("accepted"):
                first = min(kinds["accepted"])
                if block not in mined or first < mined[block][0]:
                    mined[block] = (first, node)
    span = (window[1] - window[0]) / 1000 if window[1] != float("inf") else result.get("window_s", 1)
    print(f"== {folder}: {len(mined)} blocks in a pool somewhere, {len(mined) / span:.1f}/s")
    for node, blocks in events.items():
        durations = defaultdict(list)
        tries, gaps, late = [], [], {1000: 0, 2500: 0}
        others = missing = 0
        for block, (at, origin) in mined.items():
            if origin == node or at > window[1] - 5000:
                continue
            others += 1
            kinds = blocks.get(block, {})
            times = {"mined": at}
            for kind in ("heard", "asked", "got", "try", "accepted"):
                if kinds.get(kind):
                    times[kind] = min(kinds[kind])
            if "accepted" not in times:
                missing += 1
                for limit in late:
                    late[limit] += 1
                continue
            for limit in late:
                late[limit] += times["accepted"] - at > limit
            for start, end in STEPS:
                if start in times and end in times:
                    durations[(start, end)].append(max(0, times[end] - times[start]))
            attempts = sorted(time for time in kinds.get("try", []) if time <= times["accepted"])
            if attempts:
                tries.append(len(attempts))
                gaps += [later - earlier for earlier, later in zip(attempts, attempts[1:])]
        print(f"\n-- {node}: {others} blocks mined elsewhere, {missing} never in its pool within the run")
        for step in STEPS:
            print(f"   {LABELS[step]:32} {spread(durations[step])}")
        if tries:
            print(f"   checks per block: mean {sum(tries) / len(tries):.2f}, "
                  f"{sum(1 for count in tries if count > 1) / len(tries):.0%} needed a retry, most {max(tries)}")
            print(f"   {'between two checks of a block':32} {spread(gaps)}")
        if others:
            print(f"   not in the pool 1 s after mining: {late[1000] / others:.1%}, after 2.5 s: {late[2500] / others:.1%}")


if __name__ == "__main__":
    main()
