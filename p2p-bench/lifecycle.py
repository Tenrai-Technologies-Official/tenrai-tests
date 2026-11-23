"""Where a payment's time to finality goes: py -3.14 lifecycle.py RESULT_FOLDER [RESULT_FOLDER ...]

Reads payments.json.gz (bench.py keeps it), the cut installs in the validator logs and, from a prof
build, the BLK lines. For every payment sent inside the load window that finalized it takes:

  sent        the bench sent it to one validator
  mined       the first validator took the block that carries it (the block its status names)
  registered  the validator it was sent to installed the cut that registered that block
  final       that validator installed the cut that ran the block; its API reports the payment
              final from that moment

and prints median, p90 and max of each step, and how many cuts a block waited between being
registered and being run. Payments whose status names no block or no registration cut are counted
apart. With the block events of the prof build it also follows every block: mined, admitted to
availability, registered and run, each the first validator's install of that cut.
"""

import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+\w+\s+\S+\s+cut installed height=(\d+)")
BLK = re.compile(r"^BLK (\d+) (accepted|admitted|registered|executed) ([0-9a-f]{16})$")
STAGES = ("accepted", "admitted", "registered", "executed")


def installs_and_blocks(folder):
    """When each validator installed each height, and when a block first reached each stage."""
    installs, blocks = defaultdict(dict), defaultdict(dict)
    for log in sorted(folder.glob("v*.log")):
        with open(log, errors="replace") as file:
            for line in file:
                match = BLK.match(line.strip())
                if match:
                    at = int(match.group(1)) / 1000
                    stages = blocks[match.group(3)]
                    stage = match.group(2)
                    if stage not in stages or at < stages[stage]:
                        stages[stage] = at
                    continue
                match = STAMP.match(line)
                if match:
                    when = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S.%f").timestamp()
                    installs[log.stem].setdefault(int(match.group(2)), when)
    mined = {block: stages["accepted"] for block, stages in blocks.items() if "accepted" in stages}
    return installs, mined, blocks


def spread(values):
    if not values:
        return "      -      -      -"
    ordered = sorted(values)
    pick = lambda share: ordered[min(len(ordered) - 1, int(len(ordered) * share))]
    return f"{pick(0.5):6.2f} {pick(0.9):6.2f} {ordered[-1]:6.2f}"


def report(folder):
    result = json.loads((folder / "result.json").read_text())
    window = result.get("window") or (0, float("inf"))
    with gzip.open(folder / "payments.json.gz", "rt") as file:
        saved = json.load(file)
    installs, mined, blocks = installs_and_blocks(folder)
    steps = defaultdict(list)
    waited = Counter()
    unexplained = Counter()
    for record in saved["records"]:
        if not record or record["status"] != 200 or not window[0] <= record["sent"] <= window[1]:
            continue
        status = saved["statuses"].get(record["txid"]) or {}
        final_cut = status.get("finalized_cut")
        if str(status.get("lifecycle", "")).lower() != "finalized" or not final_cut:
            unexplained["not finalized"] += 1
            continue
        node = installs.get(record["node"], {})
        final = node.get(final_cut["height"])
        registered_cut = status.get("registered_cut")
        block = (status.get("observed_work") or {}).get("block_id", "")[:16]
        if final is None:
            unexplained["final cut not in the log"] += 1
            continue
        steps["sent -> final"].append(final - record["sent"])
        if not registered_cut or block not in mined:
            unexplained["no block or registration cut"] += 1
            continue
        registered = node.get(registered_cut["height"])
        if registered is None:
            unexplained["registration cut not in the log"] += 1
            continue
        waited[final_cut["height"] - registered_cut["height"]] += 1
        steps["sent -> mined"].append(mined[block] - record["sent"])
        steps["mined -> registered"].append(registered - mined[block])
        steps["registered -> final"].append(final - registered)

    rate = result.get("settings", {}).get("rate")
    print(f"== {folder.name}: {rate} TPS offered, {len(steps['sent -> final'])} payments final")
    print(f"   {'seconds':24} median    p90    max")
    for name in ("sent -> mined", "mined -> registered", "registered -> final", "sent -> final"):
        print(f"   {name:24} {spread(steps[name])}")
    if waited:
        total = sum(waited.values())
        print("   cuts from registration to execution: "
              + ", ".join(f"{cuts}: {count / total:.0%}" for cuts, count in sorted(waited.items())))
    if unexplained:
        print("   left out: " + ", ".join(f"{reason} {count}" for reason, count in unexplained.items()))

    followed = [stages for stages in blocks.values()
                if all(stage in stages for stage in STAGES) and window[0] <= stages["accepted"] <= window[1]]
    if followed:
        print(f"   blocks mined in the window and run: {len(followed)}")
        for start, end in zip(STAGES, STAGES[1:]):
            print(f"   {start + ' -> ' + end:24} {spread([stages[end] - stages[start] for stages in followed])}")


def main():
    for folder in sys.argv[1:]:
        report(Path(folder))


if __name__ == "__main__":
    main()
