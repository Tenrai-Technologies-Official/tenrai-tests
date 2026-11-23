"""Where blocks lose time on their way to the other validators, from a relay build's run:
py -3.14 relay_trace.py RESULT_FOLDER

Reads the BLK and PROF lines of relay_patches.py. Per validator: the steps of each block mined
elsewhere (heard, asked, received, missing transactions asked and received, in the pool), and per
availability proposal it received, how many of the named blocks it lacked and how long the last one
took. Then the relay requests by kind and answer time, how often a peer's relay slots were all
taken, and the bytes of each message class per second.
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from block_trace import spread

BLK = re.compile(r"^BLK (\d+) (\w+) ([0-9a-f]{16})$")
PROF = re.compile(r"^PROF (\d+) (\S+) (\S+) n=(\d+) wall_ms=\S+ max_ms=\S+ cpu_ms=\S+ bytes=(\d+)")
STEPS = [
    ("mined", "heard", "mined -> heard of it"),
    ("heard", "asked", "heard -> asked for it"),
    ("asked", "got", "asked -> compact form here"),
    ("got", "partial", "compact form -> lacks transactions"),
    ("partial", "txasked", "lacks -> transactions asked"),
    ("txasked", "txgot", "transactions asked -> here"),
    ("got", "accepted", "compact form -> in the pool"),
    ("mined", "accepted", "mined -> in the pool"),
]


def load(folder, window):
    """node -> block -> kind -> times; node -> site -> [count, bytes] of the reports inside the window
    and the seconds they cover."""
    blocks, counters, covered = {}, {}, {}
    for log in sorted(folder.glob("v*.log")):
        events = defaultdict(lambda: defaultdict(list))
        sums = defaultdict(lambda: [0, 0])
        reports = set()
        with open(log, errors="replace") as file:
            for line in file:
                if line.startswith("BLK "):
                    match = BLK.match(line.strip())
                    if match:
                        events[match.group(3)][match.group(2)].append(int(match.group(1)))
                elif line.startswith("PROF "):
                    match = PROF.match(line.strip())
                    # A report covers the 10 s before it.
                    if match and window[0] + 10_000 <= int(match.group(1)) <= window[1]:
                        reports.add(int(match.group(1)) // 1000)
                        name = match.group(2)
                        sums[name][0] += int(match.group(4))
                        sums[name][1] += int(match.group(5))
        blocks[log.stem], counters[log.stem] = events, sums
        covered[log.stem] = 10 * len(reports)
    return blocks, counters, covered


def first(kinds, kind):
    times = kinds.get(kind)
    return min(times) if times else None


def main():
    folder = Path(sys.argv[1])
    result = json.loads((folder / "result.json").read_text())
    window = result.get("window")
    window = (window[0] * 1000, window[1] * 1000) if window else (0, float("inf"))
    seconds = (window[1] - window[0]) / 1000
    blocks, counters, covered = load(folder, window)

    mined = {}
    for node, events in blocks.items():
        for block, kinds in events.items():
            at = first(kinds, "accepted")
            if at is not None and (block not in mined or at < mined[block][0]):
                mined[block] = (at, node)
    inside = {block: value for block, value in mined.items() if window[0] <= value[0] <= window[1] - 5000}
    print(f"== {folder.name}: {len(inside)} blocks mined in the window, {len(inside) / seconds:.1f}/s")

    for node, events in blocks.items():
        durations = defaultdict(list)
        others = partial = never = 0
        for block, (at, origin) in inside.items():
            if origin == node:
                continue
            others += 1
            kinds = events.get(block, {})
            times = {"mined": at}
            for kind in ("heard", "asked", "got", "partial", "txasked", "txgot", "accepted"):
                value = first(kinds, kind)
                if value is not None:
                    times[kind] = value
            partial += "partial" in times
            if "accepted" not in times:
                never += 1
            for start, end, _ in STEPS:
                if start in times and end in times:
                    durations[(start, end)].append(max(0, times[end] - times[start]))
        print(f"\n-- {node}: {others} blocks mined elsewhere, {partial} arrived without some transactions, "
              f"{never} never in its pool")
        for start, end, label in STEPS:
            print(f"   {label:36} {spread(durations[(start, end)])}")

        # Proposals: one BLK named group per arrival, all with the same millisecond.
        named = defaultdict(list)
        for block, kinds in events.items():
            for at in kinds.get("named", []):
                if window[0] <= at <= window[1]:
                    named[at].append(block)
        lacked, waits, ages, late_ages = [], [], [], []
        for at, ids in sorted(named.items()):
            missing = []
            for block in ids:
                held = first(events.get(block, {}), "accepted")
                if block in mined:
                    ages.append(at - mined[block][0])
                if held is None or held > at:
                    missing.append(held)
                    if block in mined:
                        late_ages.append(at - mined[block][0])
            lacked.append(len(missing))
            arrived = [held for held in missing if held is not None]
            if missing:
                waits.append(max(arrived) - at if len(arrived) == len(missing) else float("inf"))
        if named:
            some = sum(1 for count in lacked if count)
            print(f"   proposals received {len(named)}, {some} named blocks it lacked; lacked per proposal "
                  f"{spread(lacked).replace(' ms', '')}")
            finite = [wait for wait in waits if wait != float("inf")]
            print(f"   {'until the last lacking block was here':36} {spread(finite)}, never {len(waits) - len(finite)}")
            print(f"   {'age of every named block':36} {spread(ages)}")
            print(f"   {'age of the lacking ones':36} {spread(late_ages)}")
        wanted = [first(kinds, "wanted") for kinds in events.values() if first(kinds, "wanted")]
        print(f"   blocks asked for because a proposal waited: {len(wanted)}")

    print("\n== relay requests (answers in the window, per validator)")
    for node, sums in counters.items():
        seconds = max(covered[node], 1)
        print(f"-- {node}, {seconds} s of reports")
        kinds = sorted({name.split(".")[2] for name in sums if name.startswith("relay.rtt.")})
        for kind in kinds:
            prefix = f"relay.rtt.{kind}."
            total, ms = sums.get(prefix + "ms", [0, 0])
            buckets = {name[len(prefix):]: value[0] for name, value in sums.items()
                       if name.startswith(prefix) and not name.endswith(".ms")}
            order = ["<100ms", "<250ms", "<500ms", "<1s", "<2s", "<4s", ">=4s", "failed"]
            shown = " ".join(f"{bucket} {buckets[bucket]}" for bucket in order if buckets.get(bucket))
            print(f"   {kind:22} {total / seconds:6.1f}/s mean {ms / max(total, 1):6.0f} ms  {shown}")
        answers = {name[len("relay.answer."):]: value[0] for name, value in sums.items()
                   if name.startswith("relay.answer.")}
        full = {name[len("relay.full."):]: value[0] for name, value in sums.items() if name.startswith("relay.full.")}
        open_count, open_sum = sums.get("relay.open", [0, 0])
        waiting_count, waiting_sum = sums.get("relay.waiting_batches", [0, 0])
        print(f"   answers {answers}; all slots taken {full}; open relay requests per peer mean "
              f"{open_sum / max(open_count, 1):.2f}; transaction batches waiting for admission "
              f"{waiting_count} times, mean {waiting_sum / max(waiting_count, 1):.1f}")
        for direction in ("tx", "rx"):
            classes = sorted(((value[1], name[len(direction) + 1:]) for name, value in sums.items()
                              if name.startswith(direction + ".") and value[1]), reverse=True)
            shown = ", ".join(f"{name.replace('_', ' ')} {size / seconds / 1024:.0f}" for size, name in classes[:8])
            print(f"   {'sent' if direction == 'tx' else 'received'} KiB/s: {shown}")


if __name__ == "__main__":
    main()
