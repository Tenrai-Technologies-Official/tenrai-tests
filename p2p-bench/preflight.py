"""Preconditions of a benchmark run, checked before it starts: a run that cannot decide does not start.

Each check stands for a way a run once became invalid: binaries replaced by another build, node data on the
SATA disk, validators on the miners' CPUs, a disk that stalls, too little commit or room, a build, a test
binary or a test browser still running. `check` gathers the record and every reason against the run; bench.py
refuses the run on any reason and keeps the record with its results (preflight.json). Binaries come only from
folders that `freeze` wrote (freeze.py, build-variant.py), with the SHA-256 of every file in their manifest.

BENCH_COMPARE names the result folder of an earlier run: the run then starts only if it matches that run in
everything but the binaries BENCH_COMPARE_ALLOW names (default tenrai-miner.exe), so the two differ in one
thing only.
"""

import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

from bench_proc import FILETIME, kernel32

MANIFEST = "manifest.json"
BINARIES = ("tenraid.exe", "tenrai-release.exe", "tenrai-wallet.exe", "tenrai-miner.exe")
NVME = os.environ.get("BENCH_NVME")
# Processes that would share the machine with a run: other nodes and miners, the bench's tools, provers,
# builds, and the copied test binaries of the node and the circuits.
COMPETING = {"tenraid.exe", "tenrai-miner.exe", "bench-tx.exe", "wan-proxy.exe", "bench-private.exe",
             "tenrai-tsp2-prove.exe", "tenrai-stratum-bridge.exe", "tenrai-pool.exe", "cargo.exe", "rustc.exe",
             "link.exe", "cl.exe", "nvcc.exe"}
COMPETING_PREFIXES = ("node-", "qualification-")
BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe"}
# The browser rounds give each browser a profile of this name (browser-wallet/browser.mjs, firefox.mjs).
TEST_PROFILE = "tenrai-e2e"
COMMIT_GB = 8.0
LOG_ROOM_GB = 3.0
FLUSH_MEDIAN_MS = 25.0
FLUSH_SLOWEST_MS = 250.0
BUSY_PERCENT = 15.0
# The machine is busy for some seconds while the processes of the run before it end; a run waits this long for it to
# settle and is refused only when it stays busy.
BUSY_SETTLE_S = 120
# A run whose node waited longer than this for one flush measured its disk, not its code.
RUN_FLUSH_MS = 1000


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def freeze(files, folder):
    """Copies each source of `files` ({name: path}) into `folder`, read-only, with a manifest of their
    SHA-256. An earlier folder of that name is replaced whole, so no file of an older build stays."""
    folder = Path(folder)
    if folder.exists():
        for path in folder.iterdir():
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    hashes = {}
    for name, source in files.items():
        target = folder / name
        shutil.copy2(source, target)
        hashes[name] = sha256(target)
        os.chmod(target, stat.S_IREAD)
    manifest = {"frozen": time.strftime("%Y-%m-%d %H:%M:%S"),
                "sources": {name: str(source) for name, source in files.items()}, "files": hashes}
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    os.chmod(folder / MANIFEST, stat.S_IREAD)
    return manifest


def frozen(folder):
    """The files of a frozen folder by name and SHA-256, and what is wrong with it."""
    folder = Path(folder)
    if not (folder / MANIFEST).exists():
        return {}, [f"{folder} is not frozen (no {MANIFEST}): build it with build-variant.py or freeze it with freeze.py"]
    manifest = json.loads((folder / MANIFEST).read_text())
    files, reasons = manifest["files"], []
    for name in BINARIES:
        if name not in files:
            reasons.append(f"{folder} holds no {name}")
    # A program that wrote its default settings beside itself leaves a file no build made.
    for path in folder.iterdir():
        if path.name != MANIFEST and path.name not in files:
            reasons.append(f"{path} is not in the manifest")
    for name, expected in files.items():
        path = folder / name
        if not path.exists():
            reasons.append(f"{path} is missing")
        elif sha256(path) != expected:
            reasons.append(f"{path} no longer matches its manifest")
        elif os.access(path, os.W_OK):
            reasons.append(f"{path} is writable, so another build could replace it")
    return files, reasons


def tools(paths):
    """SHA-256 of each tool a run starts, and the tools that are missing."""
    found, reasons = {}, []
    for name, path in paths.items():
        if Path(path).exists():
            found[name] = sha256(path)
        else:
            reasons.append(f"{name} is missing: {path}")
    return found, reasons


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("length", wt.DWORD), ("load", wt.DWORD), ("total_physical", ctypes.c_ulonglong),
                ("free_physical", ctypes.c_ulonglong), ("commit_limit", ctypes.c_ulonglong),
                ("free_commit", ctypes.c_ulonglong), ("total_virtual", ctypes.c_ulonglong),
                ("free_virtual", ctypes.c_ulonglong), ("free_extended", ctypes.c_ulonglong)]


def memory():
    status = MEMORYSTATUSEX()
    status.length = ctypes.sizeof(status)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return {"free_commit_gb": round(status.free_commit / 2**30, 1),
            "free_physical_gb": round(status.free_physical / 2**30, 1)}


def flush_probe(folder, writes=24, size=64 * 1024):
    """Milliseconds one write of `size` bytes and its flush take on the disk of `folder`."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"preflight-{os.getpid()}.probe"
    block = os.urandom(size)
    times = []
    try:
        with open(path, "wb", buffering=0) as file:
            for _ in range(writes):
                start = time.perf_counter()
                file.write(block)
                os.fsync(file.fileno())
                times.append((time.perf_counter() - start) * 1000)
    finally:
        path.unlink(missing_ok=True)
    times.sort()
    return {"median_ms": round(times[len(times) // 2], 1), "slowest_ms": round(times[-1], 1)}


def busy_percent(seconds=5.0):
    """How busy the whole machine is, in percent of all its logical CPUs."""
    def sample():
        idle, kernel, user = FILETIME(), FILETIME(), FILETIME()
        kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        return idle.seconds(), kernel.seconds() + user.seconds()
    idle0, total0 = sample()
    time.sleep(seconds)
    idle1, total1 = sample()
    return round(100 * (1 - (idle1 - idle0) / max(total1 - total0, 1e-9)), 1)


def settled_busy_percent(limit=BUSY_PERCENT, settle_s=BUSY_SETTLE_S):
    """The machine's load, measured again every few seconds until it is at most `limit` or `settle_s` passed; every
    sample, the last one deciding."""
    samples = [busy_percent()]
    deadline = time.time() + settle_s
    while samples[-1] > limit and time.time() < deadline:
        time.sleep(5)
        samples.append(busy_percent())
    return samples


def processes():
    script = ("Get-CimInstance Win32_Process | Select-Object ProcessId,Name,ExecutablePath,CommandLine"
              " | ConvertTo-Json -Compress")
    output = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True,
                            creationflags=0x08000000).stdout
    found = json.loads(output or "[]")
    return found if isinstance(found, list) else [found]


def competing(own_folders):
    """Processes that would share the machine with the run. The bench stops its own leftovers under
    `own_folders` itself, so those do not count, unless another bench is still running them."""
    own = [str(folder).lower() for folder in own_folders]
    mine = {os.getpid(), os.getppid()}
    found = []
    for process in processes():
        name = (process.get("Name") or "").lower()
        path = (process.get("ExecutablePath") or "").lower()
        line = (process.get("CommandLine") or "").lower()
        if process["ProcessId"] in mine:
            continue
        if name in ("python.exe", "py.exe") and "bench.py" in line:
            found.append(f"another bench {process['ProcessId']}")
            continue
        if path and any(path.startswith(folder) for folder in own):
            continue
        heavy = name in COMPETING or (name.endswith(".exe") and name.startswith(COMPETING_PREFIXES))
        test_browser = name in BROWSERS and TEST_PROFILE in line
        if heavy or test_browser:
            found.append(f"{process['Name']} {process['ProcessId']}")
    return found


def size_gb(folder):
    """What a folder holds, for the room the bench frees when it deletes an earlier run's data."""
    total = 0
    for root, _, names in os.walk(folder):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total / 2**30


def node_folders(bench):
    return sorted({str(bench.data(index).parent) for index in bench.nodes})


def identity(bench, data, snapshots):
    """Everything that decides what a run measures; two runs that agree here measure the same thing. The
    build counts by the hashes of its binaries, not by its variant's name."""
    scenario = {key: value for key, value in bench.scenario.items() if key not in ("rtt", "loss", "bin")}
    return {
        "scenario": bench.name,
        "settings": json.loads(json.dumps(scenario, default=str)),
        "rtt_ms": bench.scenario.get("rtt"),
        "loss": bench.scenario.get("loss"),
        "bin_folder": str(bench.bin_source),
        "disks": {"logs": str(data), "logs_drive": Path(data).drive.upper(),
                  "nodes": node_folders(bench),
                  "nodes_drives": sorted({Path(folder).drive.upper() for folder in node_folders(bench)}),
                  "snapshots": str(snapshots)},
        "cpus": {"bench": hex(bench.cpus), "miners": hex(bench.miner_mask)},
        # What the nodes and miners log changes what they do between two blocks.
        "logging": {name: os.environ.get(name) or None
                    for name in ("RUST_LOG", "BENCH_RUST_LOG", "BENCH_MINER_DEBUG", "BENCH_MINER_MODE")},
    }


def snapshot_identity(bench):
    """The snapshot a run starts from: its genesis manifest and every pre-signed payment file."""
    folder = bench.snapshot()
    files = {"bench.traidag.json": sha256(folder / "bench.traidag.json")}
    for path in sorted((folder / "tx").glob("*.txt")):
        files[f"tx/{path.name}"] = sha256(path)
    return {"folder": str(folder), "files": files}


def check(bench, data, node_data, snapshots, tool_paths, own_folders, earlier_runs):
    """The record of a run and every reason it must not start. `earlier_runs` are the folders the bench
    deletes before it starts, whose room counts as free."""
    reasons = []
    record = identity(bench, data, snapshots)
    record["binaries"], found = frozen(bench.bin_source)
    reasons += found
    record["tools"], found = tools(tool_paths)
    reasons += found
    environment = os.environ.get("BENCH_CPUS")
    if environment and bench.scenario.get("cpus") and int(environment, 0) != bench.scenario["cpus"]:
        reasons.append(f"BENCH_CPUS={environment} differs from the scenario's {hex(bench.scenario['cpus'])}")
    if bench.cpus & bench.miner_mask:
        reasons.append(f"the validators' CPUs {hex(bench.cpus)} include the miners' {hex(bench.miner_mask)}")
    if NVME:
        drive = Path(NVME).drive.upper()
        folders = [node_data] if bench.scenario.get("disks") == "split" else node_folders(bench)
        for folder in folders:
            if Path(folder).resolve().drive.upper() != drive:
                reasons.append(f"node data {folder} is not on the configured BENCH_NVME volume {drive}")
    health = {"memory": memory(), "room_gb": {}, "flush": {}}
    need = bench.scenario.get("commit_gb", COMMIT_GB)
    if health["memory"]["free_commit_gb"] < need:
        reasons.append(f"{health['memory']['free_commit_gb']} GB of commit free, {need} GB needed")
    rooms = {str(data): LOG_ROOM_GB}
    for folder in node_folders(bench):
        rooms[folder] = bench.scenario.get("disk_gb", 18.0)
    for folder, needed in rooms.items():
        Path(folder).mkdir(parents=True, exist_ok=True)
        drive = Path(folder).drive.upper()
        freed = sum(size_gb(earlier) for earlier in earlier_runs
                    if Path(earlier).exists() and Path(earlier).drive.upper() == drive)
        free = round(shutil.disk_usage(folder).free / 2**30 + freed, 1)
        health["room_gb"][folder] = free
        if free < needed:
            reasons.append(f"{free} GB free on {folder} once the earlier run's data is gone, {needed} GB needed")
    for folder in node_folders(bench):
        probe = flush_probe(folder)
        health["flush"][folder] = probe
        if probe["median_ms"] > FLUSH_MEDIAN_MS or probe["slowest_ms"] > FLUSH_SLOWEST_MS:
            reasons.append(f"the disk of {folder} stalls now: a flush takes {probe['median_ms']} ms in the middle, "
                           f"{probe['slowest_ms']} ms at most")
    health["competing"] = competing(own_folders)
    if health["competing"]:
        reasons.append("other processes would share the machine: " + ", ".join(health["competing"]))
    health["busy_samples"] = settled_busy_percent()
    health["busy_percent"] = health["busy_samples"][-1]
    if health["busy_percent"] > BUSY_PERCENT:
        reasons.append(f"the machine is {health['busy_percent']} % busy before the run, still after "
                       f"{BUSY_SETTLE_S} s ({health['busy_samples']})")
    record["health"] = health
    record["checked"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return record, reasons


def differences(record, reference, allowed):
    """What differs between two runs' records, leaving out their health and the binaries allowed."""
    found = []
    for key in sorted(set(record) | set(reference)):
        if key in ("health", "checked", "compared_with", "affinity"):
            continue
        if key == "binaries":
            for name in sorted(set(record.get(key, {})) | set(reference.get(key, {}))):
                if name not in allowed and record[key].get(name) != reference[key].get(name):
                    found.append(f"binary {name}")
            continue
        if key == "bin_folder":
            continue
        if record.get(key) != reference.get(key):
            found.append(key)
    return found


def compare(record):
    """With BENCH_COMPARE set, the reasons this run does not match the run it is compared with."""
    folder = os.environ.get("BENCH_COMPARE")
    if not folder:
        return []
    path = Path(folder) / "preflight.json"
    if not path.exists():
        return [f"BENCH_COMPARE names {folder}, which holds no preflight.json"]
    reference = json.loads(path.read_text())
    allowed = set(filter(None, os.environ.get("BENCH_COMPARE_ALLOW", "tenrai-miner.exe").split(",")))
    record["compared_with"] = {"folder": str(folder), "allowed": sorted(allowed)}
    found = differences(record, reference, allowed)
    return [f"differs from {folder} in {', '.join(found)}"] if found else []


kernel32.GetProcessAffinityMask.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_size_t),
                                            ctypes.POINTER(ctypes.c_size_t)]


def affinity(pid):
    process = kernel32.OpenProcess(0x1000, False, pid)
    mask, system = ctypes.c_size_t(), ctypes.c_size_t()
    try:
        if not kernel32.GetProcessAffinityMask(process, ctypes.byref(mask), ctypes.byref(system)):
            raise OSError(ctypes.get_last_error(), "GetProcessAffinityMask")
    finally:
        kernel32.CloseHandle(process)
    return mask.value


def placed(processes_by_name, cpus, miners):
    """Every process of the run on the CPUs it belongs on; the names of those that are not."""
    wrong = []
    for name, process in processes_by_name.items():
        wanted = miners if name.startswith("miner-") else cpus
        if process.poll() is None and affinity(process.pid) != wanted:
            wrong.append(f"{name} on {hex(affinity(process.pid))} instead of {hex(wanted)}")
    return wrong


def validity(result):
    """Whether the run measured the code and not its environment."""
    reasons = []
    slowest = max((node.get("slow_write_max_ms") or 0) for node in result.get("nodes", {}).values())
    if slowest > RUN_FLUSH_MS:
        reasons.append(f"a flush took {slowest} ms on a node's disk")
    return {"valid": not reasons, "reasons": reasons}


def summary(record):
    health = record.get("health") or {}
    flush = "; ".join(f"{folder} {probe['median_ms']}/{probe['slowest_ms']} ms"
                      for folder, probe in (health.get("flush") or {}).items())
    binaries = ", ".join(f"{name} {digest[:16]}" for name, digest in record.get("binaries", {}).items())
    return (f"binaries {binaries}; nodes on {record['disks']['nodes']}, logs on {record['disks']['logs']}; "
            f"CPUs {record['cpus']['bench']} and miners {record['cpus']['miners']}; "
            f"commit free {(health.get('memory') or {}).get('free_commit_gb')} GB; flush {flush}; "
            f"machine {health.get('busy_percent')} % busy")
