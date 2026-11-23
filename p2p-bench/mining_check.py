"""What the bench's miners can hash by themselves: py -3.14 mining_check.py [MINERS] [CPU_MASK] [SECONDS]

Starts MINERS copies of tenrai-miner on its synthetic benchmark job, one CPU thread each in TenQ
light mode as in bench.py, keeps them on the logical CPUs of CPU_MASK (0 for no pinning) and prints
each one's last reported hashrate and the sum. No node runs, so the result is the mining power the
pinning leaves, apart from templates, submissions and validators.
"""

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from bench import NO_WINDOW, TOOLS
from bench_proc import pin
from preflight import frozen

RATE = re.compile(r"INFO ([\d.]+)(k?)H/s, 15m")


def main():
    miners = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    mask = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0xC00
    seconds = int(sys.argv[3]) if len(sys.argv) > 3 else 25
    folder = TOOLS / "run-mining"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir()
    _, reasons = frozen(TOOLS / "bin-main")
    if reasons:
        raise SystemExit("; ".join(reasons))
    shutil.copyfile(TOOLS / "bin-main" / "tenrai-miner.exe", folder / "tenrai-miner.exe")
    processes = []
    for index in range(miners):
        log = open(folder / f"miner-{index}.log", "wb")
        process = subprocess.Popen(
            [str(folder / "tenrai-miner.exe"), "--benchmark", "development", "--cpu-only", "-t", "1", "--cpu-batch",
             "2", "--tenq-mode", "light", "--no-api", "--no-color", "--report-interval", "5"],
            stdout=log, stderr=subprocess.STDOUT, creationflags=NO_WINDOW, cwd=folder)
        if mask:
            pin(process.pid, mask)
        processes.append(process)
    time.sleep(seconds)
    for process in processes:
        process.kill()
    for process in processes:
        process.wait()
    rates = []
    for index in range(miners):
        found = RATE.findall((folder / f"miner-{index}.log").read_text(errors="replace"))
        rates.append(float(found[-1][0]) * (1000 if found[-1][1] else 1) if found else 0.0)
    where = f"logical CPUs {mask:#x}" if mask else "no pinning"
    print(f"{miners} miners, {where}, {seconds} s: " + " ".join(f"{rate:.0f}" for rate in rates)
          + f" H/s, sum {sum(rates):.0f} H/s")


if __name__ == "__main__":
    main()
