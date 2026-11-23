"""Keeps the miners of a run at the block rate of the profile.

The start difficulty fits the miners only roughly, and the difficulty adjustment needs far longer than a run to
settle: the same miners found 53 to 55 blocks a second on a quiet machine, which is room a settled network does
not have. They pause for a part of every half second, and that part follows the blocks the cuts register, as the
adjustment would in the long run. It can only take hash rate away, so a machine too busy to reach the rate still
shows up as a lower one. A CPU cap on a job object did not do: it took 10 to 15 % away even at its full share.
"""

import ctypes
import ctypes.wintypes as wt
import re
import threading
import time

ntdll = ctypes.WinDLL("ntdll")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
ntdll.NtSuspendProcess.argtypes = [wt.HANDLE]
ntdll.NtResumeProcess.argtypes = [wt.HANDLE]
PROCESS_SUSPEND_RESUME = 0x0800

CUT_INSTALLED = "cut installed height="
REGISTERED = re.compile(r"registered=(\d+)")
STAMP = "%Y-%m-%d %H:%M:%S"
LOOK_BACK_S = 20
EVERY_S = 10
CYCLE_S = 0.5
FIRST_PAUSE = 0.05
MOST_PAUSE = 0.5


def installed_since(handle, leftover):
    """(when, registered) of the cuts in what the node log gained, and what is left of an unfinished line."""
    text = leftover + handle.read().decode(errors="replace")
    lines = text.split("\n")
    rows = []
    for line in lines[:-1]:
        if CUT_INSTALLED in line:
            found = REGISTERED.search(line)
            if found:
                rows.append((time.mktime(time.strptime(line[:19], STAMP)), int(found.group(1))))
    return rows, lines[-1]


class BlockRateHold:
    def __init__(self, miner_pids, node_log, target_bps, log):
        self.miners = [kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid) for pid in miner_pids]
        self.pause = FIRST_PAUSE
        self.node_log = node_log
        self.target = target_bps
        self.log = log
        self.stopped = threading.Event()
        self.threads = [threading.Thread(target=self.follow, daemon=True),
                        threading.Thread(target=self.pausing, daemon=True)]

    def start(self):
        for thread in self.threads:
            thread.start()

    def stop(self):
        self.stopped.set()
        for thread in self.threads:
            thread.join()

    def pausing(self):
        while not self.stopped.is_set():
            paused = self.pause * CYCLE_S
            if paused > 0.001:
                for miner in self.miners:
                    ntdll.NtSuspendProcess(miner)
                time.sleep(paused)
                for miner in self.miners:
                    ntdll.NtResumeProcess(miner)
            time.sleep(CYCLE_S - paused)

    def follow(self):
        recent, leftover = [], ""
        with open(self.node_log, "rb") as handle:
            handle.seek(0, 2)
            while not self.stopped.wait(EVERY_S):
                rows, leftover = installed_since(handle, leftover)
                recent += rows
                now = time.time()
                recent = [row for row in recent if row[0] >= now - LOOK_BACK_S]
                if len(recent) < 2:
                    continue
                span = recent[-1][0] - recent[0][0]
                if span < LOOK_BACK_S / 2:
                    continue
                measured = sum(blocks for _, blocks in recent[1:]) / span
                # Half of the error at a time: the windows overlap and the rate lags the pause.
                running = (1 - self.pause) * (self.target / max(measured, 1.0)) ** 0.5
                self.pause = min(MOST_PAUSE, max(0.0, 1 - running))
                self.log(f"blocks/s {measured:.1f}, miners pause {self.pause:.0%} of the time")
