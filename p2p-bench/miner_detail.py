"""Per miner: CPU, hash rate, finds, repeat finds and how long new work took after a find:
py -3.14 miner_detail.py RESULT_FOLDER [...]

Needs a run with BENCH_MINER_DEBUG=1, whose miner logs name every accepted block, every block a miner
drops because its job's transactions were already in a block it found, and every new job. A job is
new work when it differs from the last one; the gap from an accepted block to the next new work shows
how long the miner kept hashing a job whose transactions it had already used.
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

TIME = r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+"
ACCEPTED = re.compile(TIME + r"DEBUG \S+ block \S+ accepted at height \d+")
REPEAT = re.compile(TIME + r"DEBUG \S+ another block of job \S+ would repeat its transactions")
WORK = re.compile(TIME + r"(?:DEBUG|INFO) \S+ (?:new work|work available) at height \d+")
RATE = re.compile(TIME + r"INFO \S+ ([0-9.]+)H/s, 15m")


def stamp(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f").timestamp()


def spread(values):
    if not values:
        return "     -/     -/     -"
    values = sorted(values)
    median = values[len(values) // 2]
    p90 = values[min(len(values) - 1, int(len(values) * 0.9))]
    return f"{median * 1000:6.0f}/{p90 * 1000:6.0f}/{values[-1] * 1000:6.0f}"


def events(log):
    accepted, repeats, work, rates = [], [], [], []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if match := ACCEPTED.match(line):
            accepted.append(stamp(match.group(1)))
        elif match := REPEAT.match(line):
            repeats.append(stamp(match.group(1)))
        elif match := WORK.match(line):
            work.append(stamp(match.group(1)))
        elif match := RATE.match(line):
            rates.append(float(match.group(2)))
    return accepted, repeats, work, rates


def next_after(times, moment):
    return next((time for time in times if time > moment), None)


def last_before(times, moment):
    earlier = [time for time in times if time < moment]
    return earlier[-1] if earlier else None


def report(folder):
    folder = Path(folder)
    resources = json.loads((folder / "result.json").read_text())["resources"]
    print(f"== {folder.name}")
    print("   miner          cores   H/s  accepted repeats  new work after a find ms   repeat after its find ms")
    print("                                                   median/   p90/   max      median/   p90/   max")
    every_switch, every_repeat = [], []
    for log in sorted(folder.glob("miner-v*.log")):
        accepted, repeats, work, rates = events(log)
        switches = [after - found for found in accepted if (after := next_after(work, found)) is not None]
        delays = [repeat - found for repeat in repeats if (found := last_before(accepted, repeat)) is not None]
        every_switch += switches
        every_repeat += delays
        cores = resources.get(log.stem, {}).get("cpu_cores_mean") or 0.0
        rate = sorted(rates)[len(rates) // 2] if rates else 0.0
        print(f"   {log.stem:14} {cores:5.2f} {rate:5.0f} {len(accepted):8d} {len(repeats):7d}  "
              f"{spread(switches)}    {spread(delays)}")
    print(f"   all                                               {spread(every_switch)}    {spread(every_repeat)}")


for argument in sys.argv[1:]:
    report(argument)
