"""Code paths of a prof run, summed over the measuring window: py -3.14 window_prof.py RESULT_FOLDER [names]

Reads the PROF lines (prof_patches.py) of every validator log and keeps those written while the
load ran. A name list keeps only the paths starting with one of the names, such as `cut.prefix`
or `install.`; the default covers cut work, transactions, work blocks, installs, votes and
availability. Each path gets its count and, per call, wall and CPU milliseconds and the state nodes
it read, missed in the node cache and the time spent in those misses. Paths that run many times a
cut (tx., exec., work.tenq, sig.) are shown per prefix run instead, that is per cut.
"""

import json
import sys
from pathlib import Path

DEFAULT = ["cut.", "exec.", "tx.", "work.", "install.", "lock.install", "vote.", "avail."]
PER_RUN = ("tx.", "exec.", "work.tenq", "sig.")


def fields(parts):
    return dict(part.split("=", 1) for part in parts if "=" in part)


def main():
    folder = Path(sys.argv[1])
    names = tuple(sys.argv[2:] or DEFAULT)
    window = json.loads((folder / "result.json").read_text()).get("window")
    for log in sorted(folder.glob("v*.log")):
        totals, runs = {}, 0
        with open(log, errors="replace") as file:
            for line in file:
                if not line.startswith("PROF "):
                    continue
                parts = line.split()
                # A report covers the 10 s before it, so the window is shifted by one report.
                if window and not window[0] + 10 <= int(parts[1]) / 1000 <= window[1] + 10:
                    continue
                name = parts[2] if parts[3] == "-" else f"{parts[2]} {parts[3]}"
                values = fields(parts[3:])
                if name == "cut.prefix.run":
                    runs += int(values["n"])
                if not name.startswith(names):
                    continue
                total = totals.setdefault(name, [0, 0.0, 0.0, 0, 0, 0.0])
                total[0] += int(values["n"])
                total[1] += float(values.get("wall_ms", values.get("read_ms", 0)))
                total[2] += float(values.get("cpu_ms", 0))
                total[3] += int(values.get("reads", 0))
                total[4] += int(values.get("misses", 0))
                total[5] += float(values.get("miss_ms", 0))
        runs = runs or 1
        print(f"== {log.stem}: {runs} prefix runs in the window")
        for name in sorted(totals):
            count, wall, cpu, reads, misses, miss_ms = totals[name]
            per_run = name.startswith(PER_RUN)
            divisor = runs if per_run else count
            print(f"   {name:44} n {count:7d} {'/run' if per_run else '/call'} wall {wall / divisor:7.1f} "
                  f"cpu {cpu / divisor:7.1f} reads {reads / divisor:8.0f} misses {misses / divisor:6.0f} "
                  f"miss_ms {miss_ms / divisor:6.1f}")


if __name__ == "__main__":
    main()
