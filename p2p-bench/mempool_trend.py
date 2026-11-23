"""How a run's backlog moved: py -3.14 mempool_trend.py RESULT_FOLDER [SECONDS]

Prints, every SECONDS (default 10) of the run, what one validator's pool held at its cut installs:
transactions waiting for a block, transactions already in a block that has not run yet, and how many
blocks carried each of those. A backlog that keeps growing while the load stays the same means the
chain takes in less than it is offered, whatever was final once the load stopped.
"""

import re
import sys
from datetime import datetime
from pathlib import Path

INSTALL = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+) DEBUG \S+ cut installed height=(\d+) registered=(\d+) "
    r"executed=(\d+) transactions=(\d+) mempool=(\d+) carried=(\d+) copies=(\d+)")


def stamp(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f").timestamp()


def report(folder, step):
    log = Path(folder) / "v1.log"
    rows = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if match := INSTALL.match(line):
            fields = [int(value) for value in match.groups()[1:]]
            rows.append((stamp(match.group(1)), *fields))
    if not rows:
        print(f"{folder}: no cut installs in v1.log")
        return
    start = rows[0][0]
    slots = {}
    for time, height, _, _, transactions, mempool, carried, copies in rows:
        entry = slots.setdefault(int((time - start) // step), {"cuts": 0, "executed": 0})
        entry["cuts"] += 1
        entry["executed"] += transactions
        entry["last"] = (height, mempool, carried, copies)
    print(f"== {Path(folder).name}: v1, every {step:.0f} s")
    print("     s  height  cuts  executed tx  waiting  in blocks  blocks each")
    for slot in sorted(slots):
        entry = slots[slot]
        height, mempool, carried, copies = entry["last"]
        each = copies / carried if carried else 0.0
        print(f"  {slot * step:4.0f}  {height:6d}  {entry['cuts']:4d}  {entry['executed']:11d}  "
              f"{mempool - carried:7d}  {carried:9d}  {each:11.2f}")


for argument in sys.argv[1:2]:
    report(argument, float(sys.argv[2]) if len(sys.argv) > 2 else 10.0)
