"""What the miners of a run found, and what became of it: py -3.14 miner_stats.py RESULT_FOLDER [...]

Adds up the miners' 5 s summaries ("N blocks in the last 5s: A accepted, S stale, R rejected")
over the load window and one summary more, as each covers the 5 s before it. Stale counts both
blocks the node called too late and blocks the miner dropped itself because their job's
transactions were already in a block it found.
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

SUMMARY = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\S*\s+INFO (?:\S+: )?(\d+) blocks in the last 5s: (\d+) accepted"
    r"(?: at heights [0-9-]+)?(?:, (\d+) stale)?(?:, (\d+) rejected)?")


def report(folder):
    window = json.loads((folder / "result.json").read_text()).get("window") or (0, float("inf"))
    totals = [0, 0, 0, 0]
    print(f"== {folder.name}")
    for log in sorted(folder.glob("miner-*.log")):
        counts = [0, 0, 0, 0]
        with open(log, errors="replace") as file:
            for line in file:
                match = SUMMARY.match(line)
                if not match:
                    continue
                when = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                if not window[0] <= when <= window[1] + 5:
                    continue
                for index, group in enumerate(match.groups()[1:]):
                    counts[index] += int(group or 0)
        totals = [total + count for total, count in zip(totals, counts)]
        found, accepted, stale, rejected = counts
        print(f"   {log.stem:14} found {found:5d}  accepted {accepted:5d}  stale {stale:5d} "
              f"({stale / max(found, 1):4.0%})  rejected {rejected}")
    found, accepted, stale, rejected = totals
    span = window[1] - window[0] + 5
    print(f"   all            found {found:5d}  accepted {accepted:5d}  stale {stale:5d} "
          f"({stale / max(found, 1):4.0%})  rejected {rejected};  {found / span:.1f} found/s, "
          f"{accepted / span:.1f} accepted/s")


def main():
    for folder in sys.argv[1:]:
        report(Path(folder))


if __name__ == "__main__":
    main()
